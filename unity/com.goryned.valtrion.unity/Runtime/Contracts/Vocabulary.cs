using System;

namespace Valtrion.Unity.Contracts
{
    /// <summary>Terminal status of a run, job or step. The CLI exit codes are 0 pass, 1 fail, 2 blocked or needs permission, 3 stall or timeout.</summary>
    public static class ValidationStatus
    {
        public const string Pass = "PASS";
        public const string Fail = "FAIL";
        public const string Blocked = "BLOCKED";
        public const string NeedsPermission = "NEEDS_PERMISSION";
        public const string Stall = "STALL";
        public const string Timeout = "TIMEOUT";

        public static readonly string[] All = { Pass, Fail, Blocked, NeedsPermission, Stall, Timeout };

        public static int ExitCode(string status)
        {
            switch (status)
            {
                case Pass: return 0;
                case Fail: return 1;
                case Blocked:
                case NeedsPermission: return 2;
                case Stall:
                case Timeout: return 3;
                default: throw new ArgumentException($"unknown status '{status}'", nameof(status));
            }
        }
    }

    /// <summary>What kind of target produced a result. Only physical, emulator and host evidence can become a baseline.</summary>
    public static class EvidenceKind
    {
        public const string Physical = "physical";
        public const string Emulator = "emulator";
        public const string Editor = "editor";
        public const string Host = "host";

        public static readonly string[] All = { Physical, Emulator, Editor, Host };
        public static readonly string[] Baseline = { Physical, Emulator, Host };
    }

    /// <summary>Verdicts of comparing a result with a baseline.</summary>
    public static class ComparisonVerdict
    {
        public static readonly string[] All = { "PASS", "REGRESSION", "IMPROVED", "NOISY", "NOT_COMPARABLE" };
    }

    /// <summary>Schema ids of the contracts; the value of the <c>schema</c> property of every document.</summary>
    public static class SchemaIds
    {
        public const string Plan = "gdv.plan/1";
        public const string Pipeline = "gdv.pipeline/1";
        public const string Result = "gdv.result/1";
        public const string Heartbeat = "gdv.heartbeat/1";
        public const string Job = "gdv.job/1";
        public const string Event = "gdv.event/1";
        public const string Hosts = "gdv.hosts/1";
        public const string Registry = "gdv.registry/1";
        public const string Lease = "gdv.lease/1";
        public const string Checks = "gdv.checks/1";
        public const string Baseline = "gdv.baseline/1";

        public static readonly string[] All = { Plan, Pipeline, Result, Heartbeat, Job, Event, Hosts, Registry, Lease, Checks, Baseline };
    }

    static class Enums
    {
        public static readonly string[] Platforms = { "ios", "android", "windows", "macos", "editor" };
        public static readonly string[] PlanKinds = { "functional", "performance", "soak", "smoke" };
        public static readonly string[] DataPolicies = { "isolated", "devdata", "clean-install" };
        public static readonly string[] Directions = { "lower", "higher" };
        public static readonly string[] Cleanups = { "ledger", "none" };
        public static readonly string[] Stages = { "build", "install", "run", "collect" };
        public static readonly string[] HeartbeatStates = { "starting", "running", "waiting-host", "finishing", "done" };
        public static readonly string[] JobKinds = { "run", "pipeline", "build", "install", "collect" };
        public static readonly string[] JobStates = { "queued", "running", "finished", "cancelled" };
        public static readonly string[] EventTypes =
        {
            "job.started", "job.finished", "step.started", "step.finished", "progress",
            "stall.detected", "timeout.detected", "lease.acquired", "lease.released", "log",
        };
        public static readonly string[] HostOs = { "windows", "macos", "linux" };
        public static readonly string[] TransportKinds = { "local", "ssh" };
        public static readonly string[] HostSelections = { "local", "capability", "pinned" };
        public static readonly string[] WorktreePolicies = { "per-task", "shared" };
        public static readonly string[] Unclassified = { "widest", "fail" };
    }

    /// <summary>A gdv document: it knows its schema id and writes itself as a JSON value tree.</summary>
    public interface IGdvDocument
    {
        string Schema { get; }

        Json.JsonValue ToJson();
    }
}
