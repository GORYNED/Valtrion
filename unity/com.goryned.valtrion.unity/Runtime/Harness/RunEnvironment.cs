using System;
using System.Collections.Generic;
using System.IO;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>The candidate, target and host a run reports. What the app cannot know itself (commit, host) comes from an optional
    /// <c>candidate.json</c> the host pushes into the inbox; everything else is read from the engine.</summary>
    public sealed class RunEnvironment
    {
        public ResultCandidate Candidate { get; set; }

        public ResultTarget Target { get; set; }

        public ResultHost Host { get; set; }

        /// <summary>Claims that could not be backed, added to the result's <c>unverified</c> list.</summary>
        public List<string> Unverified { get; } = new List<string>();

        public const string CandidateFile = "candidate.json";

        /// <summary>Detects the environment of this process. <paramref name="inboxDirectory"/> may hold <c>candidate.json</c>, which is consumed.</summary>
        public static RunEnvironment Detect(string inboxDirectory)
        {
            var info = new RunEnvironment();
            JsonValue overrides = null;
            var file = inboxDirectory == null ? null : Path.Combine(inboxDirectory, CandidateFile);
            if (file != null && File.Exists(file))
            {
                try
                {
                    overrides = JsonValue.Parse(File.ReadAllText(file));
                    File.Move(file, file + ".done-" + UtcFormat.Compact(DateTime.UtcNow));
                }
                catch (Exception ex) when (ex is JsonException || ex is IOException || ex is UnauthorizedAccessException)
                {
                    info.Unverified.Add($"{CandidateFile} was ignored: {ex.Message}");
                }
            }

            var editor = Application.isEditor;
            var platform = PlatformName(out var knownPlatform);
            info.Candidate = new ResultCandidate
            {
                Repo = Text(overrides, "repo", "unknown"),
                Sha = Hex(overrides, "sha"),
                Profile = Identifiers.Sanitize(Text(overrides, "profile", editor ? "Editor" : Debug.isDebugBuild ? "Development" : "Release"), "unknown"),
                Unity = Application.unityVersion,
                Dirty = Bool(overrides, "dirty", true),
            };
            if (info.Candidate.Sha == null)
            {
                info.Candidate.Sha = new string('0', 40);
                info.Unverified.Add("candidate commit unknown: no candidate.json with a sha was pushed into the inbox");
            }

            var evidence = Text(overrides, "evidence", editor ? EvidenceKind.Editor : !knownPlatform ? EvidenceKind.Host : LooksLikeEmulator() ? EvidenceKind.Emulator : EvidenceKind.Physical);
            if (Array.IndexOf(EvidenceKind.All, evidence) < 0)
            {
                info.Unverified.Add($"candidate.json evidence '{evidence}' is not a known kind and was ignored");
                evidence = editor ? EvidenceKind.Editor : EvidenceKind.Host;
            }
            info.Target = new ResultTarget
            {
                Platform = platform,
                DeviceId = Text(overrides, "deviceId", null) is string deviceId ? Identifiers.Sanitize(deviceId, "device") : null,
                DeviceClass = Identifiers.Sanitize(Text(overrides, "deviceClass", SystemInfo.deviceModel), "unknown"),
                Model = SystemInfo.deviceModel,
                Os = SystemInfo.operatingSystem,
                OsMajor = FirstNumber(SystemInfo.operatingSystem),
                Evidence = evidence,
            };
            info.Host = new ResultHost
            {
                Id = Identifiers.Sanitize(Text(overrides, "hostId", "device"), "device"),
                Transport = "local",
                SelectedBy = "local",
            };
            info.Unverified.Add("host fields describe the device itself; gdv rewrites them when it collects the result");
            return info;
        }

        static string PlatformName(out bool known)
        {
            known = true;
            switch (Application.platform)
            {
                case RuntimePlatform.WindowsPlayer: return "windows";
                case RuntimePlatform.OSXPlayer: return "macos";
                case RuntimePlatform.IPhonePlayer: return "ios";
                case RuntimePlatform.Android: return "android";
                case RuntimePlatform.WindowsEditor:
                case RuntimePlatform.OSXEditor:
                case RuntimePlatform.LinuxEditor: return "editor";
                default:
                    known = false;
                    return "editor";
            }
        }

        static bool LooksLikeEmulator()
        {
            var model = SystemInfo.deviceModel ?? string.Empty;
            return Application.platform == RuntimePlatform.Android
                && (model.IndexOf("sdk_gphone", StringComparison.OrdinalIgnoreCase) >= 0 || model.IndexOf("Emulator", StringComparison.OrdinalIgnoreCase) >= 0);
        }

        static int FirstNumber(string text)
        {
            var value = 0;
            var started = false;
            foreach (var character in text ?? string.Empty)
            {
                if (character >= '0' && character <= '9')
                {
                    started = true;
                    value = value * 10 + (character - '0');
                    if (value > 100000)
                        break;
                }
                else if (started)
                {
                    break;
                }
            }
            return value;
        }

        static string Text(JsonValue document, string name, string fallback) =>
            document != null && document.Kind == JsonKind.Object && document.TryGet(name, out var value) && value.Kind == JsonKind.String && value.AsString.Length > 0 ? value.AsString : fallback;

        static bool Bool(JsonValue document, string name, bool fallback) =>
            document != null && document.Kind == JsonKind.Object && document.TryGet(name, out var value) && value.Kind == JsonKind.Bool ? value.AsBool : fallback;

        static string Hex(JsonValue document, string name)
        {
            var text = Text(document, name, null);
            if (text == null || text.Length != 40)
                return null;
            foreach (var character in text)
            {
                if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f')))
                    return null;
            }
            return text;
        }
    }
}
