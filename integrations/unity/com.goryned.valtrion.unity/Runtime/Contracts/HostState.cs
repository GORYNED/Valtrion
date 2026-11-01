using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    public sealed class RegistryHost
    {
        public string Id { get; set; }

        public string Os { get; set; }

        public List<string> Roles { get; set; } = new List<string>();

        internal static RegistryHost Read(ContractReader reader)
        {
            var host = new RegistryHost { Id = reader.Identifier("id"), Os = reader.Choice("os", Enums.HostOs), Roles = reader.Strings("roles") };
            reader.Done();
            return host;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("id", JsonValue.From(Id)).Set("os", JsonValue.From(Os)).Set("roles", JsonBuild.Strings(Roles));
    }

    public sealed class RegistryDevice
    {
        public string Id { get; set; }

        /// <summary>The host that owns the device; leases on it are taken on that host.</summary>
        public string HostId { get; set; }

        public string Alias { get; set; }

        public string Platform { get; set; }

        public string Class { get; set; }

        public string Model { get; set; }

        public string Os { get; set; }

        public string Evidence { get; set; }

        public List<string> Capabilities { get; set; } = new List<string>();

        public bool Exclusive { get; set; }

        public string Notes { get; set; }

        internal static RegistryDevice Read(ContractReader reader)
        {
            var device = new RegistryDevice
            {
                Id = reader.Identifier("id"),
                HostId = reader.Identifier("hostId"),
                Alias = reader.OptIdentifier("alias"),
                Platform = reader.Choice("platform", Enums.Platforms),
                Class = reader.Identifier("class"),
                Model = reader.OptStr("model"),
                Os = reader.OptStr("os"),
                Evidence = reader.OptChoice("evidence", EvidenceKind.All),
                Capabilities = reader.Strings("capabilities"),
                Exclusive = reader.Bool("exclusive"),
                Notes = reader.OptStr("notes"),
            };
            reader.Done();
            return device;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("id", JsonValue.From(Id)).Set("hostId", JsonValue.From(HostId)).SetIf("alias", Alias).Set("platform", JsonValue.From(Platform)).Set("class", JsonValue.From(Class))
            .SetIf("model", Model).SetIf("os", Os).SetIf("evidence", Evidence)
            .Set("capabilities", JsonBuild.Strings(Capabilities)).Set("exclusive", JsonValue.From(Exclusive)).SetIf("notes", Notes);
    }

    /// <summary>The host's device registry (<c>gdv.registry/1</c>, kept outside git). Plans require capabilities, never a device id.</summary>
    public sealed class RegistryDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Registry;

        public RegistryHost Host { get; set; }

        public List<RegistryDevice> Devices { get; set; } = new List<RegistryDevice>();

        public static RegistryDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static RegistryDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Registry);
            var registry = new RegistryDocument
            {
                Host = RegistryHost.Read(reader.Object("host")),
                Devices = reader.Items("devices", RegistryDevice.Read),
            };
            reader.Done();
            return registry;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("host", Host.Write()).Set("devices", JsonBuild.Array(Devices, device => device.Write()));

        public string ToJsonString() => ToJson().Write(true);
    }

    /// <summary>A lease on an exclusive resource (<c>gdv.lease/1</c>). A live lease is never taken over; it is stale only when the owner process is gone and the TTL has expired.</summary>
    public sealed class LeaseDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Lease;

        public string Resource { get; set; }

        public string Owner { get; set; }

        public string Session { get; set; }

        public string Worktree { get; set; }

        public int Pid { get; set; }

        public string Host { get; set; }

        public string AcquiredUtc { get; set; }

        public string RenewedUtc { get; set; }

        public int TtlSec { get; set; }

        public string Purpose { get; set; }

        public static LeaseDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static LeaseDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Lease);
            var lease = new LeaseDocument
            {
                Resource = reader.Identifier("resource"),
                Owner = reader.Str("owner"),
                Session = reader.Str("session"),
                Worktree = reader.OptStr("worktree"),
                Pid = reader.Int32("pid", 1),
                Host = reader.Identifier("host"),
                AcquiredUtc = reader.Utc("acquiredUtc"),
                RenewedUtc = reader.Utc("renewedUtc"),
                TtlSec = reader.Int32("ttlSec", 1),
                Purpose = reader.Str("purpose", 0),
            };
            reader.Done();
            return lease;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("resource", JsonValue.From(Resource)).Set("owner", JsonValue.From(Owner))
            .Set("session", JsonValue.From(Session)).SetIf("worktree", Worktree).Set("pid", JsonValue.From(Pid))
            .Set("host", JsonValue.From(Host)).Set("acquiredUtc", JsonValue.From(AcquiredUtc)).Set("renewedUtc", JsonValue.From(RenewedUtc))
            .Set("ttlSec", JsonValue.From(TtlSec)).Set("purpose", JsonValue.From(Purpose));

        public string ToJsonString() => ToJson().Write(true);
    }
}
