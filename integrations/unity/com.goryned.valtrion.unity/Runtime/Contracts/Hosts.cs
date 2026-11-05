using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    public sealed class HostTransport
    {
        /// <summary>local (the machine gdv runs on; no transport involved) or ssh.</summary>
        public string Kind { get; set; }

        public string SshTarget { get; set; }

        public int? ConnectTimeoutSec { get; set; }

        internal static HostTransport Read(ContractReader reader)
        {
            var transport = new HostTransport
            {
                Kind = reader.Choice("kind", Enums.TransportKinds),
                SshTarget = reader.OptStr("sshTarget", 1),
                ConnectTimeoutSec = reader.OptInt32("connectTimeoutSec", 1),
            };
            if (transport.SshTarget != null && transport.SshTarget[0] == '-')
                throw new ContractException($"{reader.Path}.sshTarget: must not start with '-' (it would be read as an ssh option)");
            if (transport.SshTarget != null)
            {
                foreach (var character in transport.SshTarget)
                {
                    if (character < 0x20)
                        throw new ContractException($"{reader.Path}.sshTarget: must not contain control characters");
                }
            }
            if (transport.Kind == "ssh" && transport.SshTarget == null)
                throw new ContractException($"{reader.Path}: an ssh host needs sshTarget");
            if (transport.Kind == "local" && transport.SshTarget != null)
                throw new ContractException($"{reader.Path}: a local host has no sshTarget");
            reader.Done();
            return transport;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("kind", JsonValue.From(Kind)).SetIf("sshTarget", SshTarget).SetIf("connectTimeoutSec", ConnectTimeoutSec);
    }

    public sealed class HostEditor
    {
        public string Version { get; set; }

        public List<string> Modules { get; set; } = new List<string>();

        internal static HostEditor Read(ContractReader reader)
        {
            var editor = new HostEditor { Version = reader.Str("version"), Modules = reader.Strings("modules") };
            reader.Done();
            return editor;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("version", JsonValue.From(Version)).Set("modules", JsonBuild.Strings(Modules));
    }

    /// <summary>What a host declares; <c>gdv doctor</c> verifies it. Attached devices are discovered, not declared.</summary>
    public sealed class HostCapabilitiesInfo
    {
        public List<HostEditor> UnityEditors { get; set; }

        public List<string> BuildPlatforms { get; set; }

        public string Xcode { get; set; }

        public bool? IosSigning { get; set; }

        public bool? Adb { get; set; }

        internal static HostCapabilitiesInfo Read(ContractReader reader)
        {
            var capabilities = new HostCapabilitiesInfo
            {
                UnityEditors = reader.OptItems("unityEditors", HostEditor.Read),
                BuildPlatforms = reader.OptChoices("buildPlatforms", Enums.Platforms),
                Xcode = reader.OptStr("xcode", 1),
                IosSigning = reader.OptBool("iosSigning"),
                Adb = reader.OptBool("adb"),
            };
            reader.Done();
            return capabilities;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject();
            if (UnityEditors != null) node.Set("unityEditors", JsonBuild.Array(UnityEditors, editor => editor.Write()));
            if (BuildPlatforms != null) node.Set("buildPlatforms", JsonBuild.Strings(BuildPlatforms));
            return node.SetIf("xcode", Xcode).SetIf("iosSigning", IosSigning).SetIf("adb", Adb);
        }
    }

    public sealed class HostEntry
    {
        public string Id { get; set; }

        public HostTransport Transport { get; set; }

        public string Os { get; set; }

        public List<string> Roles { get; set; }

        public HostCapabilitiesInfo Capabilities { get; set; }

        public string ProjectsRoot { get; set; }

        public string WorktreePolicy { get; set; }

        public string ArtifactsRoot { get; set; }

        /// <summary>Absolute path of the Python (3.10 or newer) that <c>valtrion hosts sync</c> uses on an SSH host; null when not recorded.</summary>
        public string Python { get; set; }

        /// <summary>Absolute path of the valtrion command on an SSH host, recorded by <c>valtrion hosts sync</c> and used instead of a
        /// PATH lookup; null when not recorded.</summary>
        public string ValtrionCommand { get; set; }

        internal static HostEntry Read(ContractReader reader)
        {
            var host = new HostEntry
            {
                Id = reader.Identifier("id"),
                Transport = HostTransport.Read(reader.Object("transport")),
                Os = reader.Choice("os", Enums.HostOs),
                Roles = reader.OptStrings("roles"),
                Capabilities = reader.OptObject("capabilities") is ContractReader capabilities ? HostCapabilitiesInfo.Read(capabilities) : null,
                ProjectsRoot = reader.OptStr("projectsRoot", 1),
                WorktreePolicy = reader.OptChoice("worktreePolicy", Enums.WorktreePolicies),
                ArtifactsRoot = reader.Str("artifactsRoot"),
                Python = reader.OptStr("python", 1),
                ValtrionCommand = reader.OptStr("valtrionCommand", 1),
            };
            reader.Done();
            return host;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("id", JsonValue.From(Id)).Set("transport", Transport.Write()).Set("os", JsonValue.From(Os));
            if (Roles != null) node.Set("roles", JsonBuild.Strings(Roles));
            if (Capabilities != null) node.Set("capabilities", Capabilities.Write());
            return node.SetIf("projectsRoot", ProjectsRoot).SetIf("worktreePolicy", WorktreePolicy).Set("artifactsRoot", JsonValue.From(ArtifactsRoot))
                .SetIf("python", Python).SetIf("valtrionCommand", ValtrionCommand);
        }
    }

    /// <summary>The host inventory of one machine (<c>gdv.hosts/1</c>, outside git). gdv probes the local machine first and uses a remote
    /// host only when the local one cannot serve the request; local and remote runs share one supervisor, leases and result format.</summary>
    public sealed class HostsDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Hosts;

        public List<HostEntry> Hosts { get; set; } = new List<HostEntry>();

        public static HostsDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static HostsDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Hosts);
            var document = new HostsDocument { Hosts = reader.Items("hosts", HostEntry.Read, 1) };
            reader.Done();
            var seen = new HashSet<string>();
            var locals = 0;
            foreach (var host in document.Hosts)
            {
                if (!seen.Add(host.Id))
                    throw new ContractException($"$.hosts: duplicate host id '{host.Id}'");
                if (host.Transport.Kind == "local")
                    locals++;
            }
            if (locals > 1)
                throw new ContractException("$.hosts: at most one local host");
            return document;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("hosts", JsonBuild.Array(Hosts, host => host.Write()));

        public string ToJsonString() => ToJson().Write(true);
    }
}
