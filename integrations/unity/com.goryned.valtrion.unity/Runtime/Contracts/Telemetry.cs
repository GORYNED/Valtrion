using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    /// <summary>A request from the Runtime to the host (for example a system dialog only the host can answer), carried by a heartbeat.</summary>
    public sealed class HostRequest
    {
        public string Id { get; set; }

        public string Action { get; set; }

        public JsonValue Args { get; set; }

        internal static HostRequest Read(ContractReader reader)
        {
            var request = new HostRequest { Id = reader.Identifier("id"), Action = reader.Str("action"), Args = reader.OptTree("args") };
            reader.Done();
            return request;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("id", JsonValue.From(Id)).Set("action", JsonValue.From(Action)).SetIf("args", Args);
    }

    /// <summary>The heartbeat file (<c>gdv.heartbeat/1</c>) the Runtime rewrites atomically every few seconds; <c>seq</c> growing means progress.</summary>
    public sealed class HeartbeatDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Heartbeat;

        public string RunId { get; set; }

        public long Seq { get; set; }

        public string Utc { get; set; }

        public string State { get; set; }

        public int StepIndex { get; set; }

        public string StepType { get; set; }

        public long ElapsedMs { get; set; }

        public string Message { get; set; }

        public HostRequest Request { get; set; }

        public static HeartbeatDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static HeartbeatDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Heartbeat);
            var heartbeat = new HeartbeatDocument
            {
                RunId = reader.Identifier("runId"),
                Seq = reader.Int("seq", 0),
                Utc = reader.Utc("utc"),
                State = reader.Choice("state", Enums.HeartbeatStates),
                StepIndex = reader.Int32("stepIndex", -1),
                StepType = reader.Str("stepType", 0),
                ElapsedMs = reader.Int("elapsedMs", 0),
                Message = reader.OptStr("message"),
                Request = reader.OptObject("request") is ContractReader request ? HostRequest.Read(request) : null,
            };
            reader.Done();
            return heartbeat;
        }

        public JsonValue ToJson()
        {
            var node = JsonValue.NewObject().Set("schema", JsonValue.From(Schema)).Set("runId", JsonValue.From(RunId))
                .Set("seq", JsonValue.From(Seq)).Set("utc", JsonValue.From(Utc)).Set("state", JsonValue.From(State))
                .Set("stepIndex", JsonValue.From(StepIndex)).Set("stepType", JsonValue.From(StepType)).Set("elapsedMs", JsonValue.From(ElapsedMs))
                .SetIf("message", Message);
            if (Request != null) node.Set("request", Request.Write());
            return node;
        }

        public string ToJsonString() => ToJson().Write(true);
    }

    public sealed class JobWatchdog
    {
        public int StallSec { get; set; }

        public int TimeoutSec { get; set; }

        public string LastProgressUtc { get; set; }

        internal static JobWatchdog Read(ContractReader reader)
        {
            var watchdog = new JobWatchdog
            {
                StallSec = reader.Int32("stallSec", 1),
                TimeoutSec = reader.Int32("timeoutSec", 1),
                LastProgressUtc = reader.Utc("lastProgressUtc"),
            };
            reader.Done();
            return watchdog;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("stallSec", JsonValue.From(StallSec)).Set("timeoutSec", JsonValue.From(TimeoutSec)).Set("lastProgressUtc", JsonValue.From(LastProgressUtc));
    }

    /// <summary>The state of a detached host job (<c>gdv.job/1</c>, <c>job.json</c>); <c>status</c> is null until the job has finished.</summary>
    public sealed class JobDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Job;

        public string JobId { get; set; }

        public string Kind { get; set; }

        public string State { get; set; }

        public string Status { get; set; }

        public string Reason { get; set; }

        public string CreatedUtc { get; set; }

        public string UpdatedUtc { get; set; }

        public string Host { get; set; }

        public int? Pid { get; set; }

        public List<string> Command { get; set; } = new List<string>();

        public string ResultPath { get; set; }

        /// <summary>What a running pipeline job is doing (waiting-disk, waiting-build, building, waiting-device, on-device); null otherwise.</summary>
        public string Phase { get; set; }

        public JobWatchdog Watchdog { get; set; }

        public long EventsOffset { get; set; }

        public static JobDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static JobDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Job);
            var job = new JobDocument
            {
                JobId = reader.Identifier("jobId"),
                Kind = reader.Choice("kind", Enums.JobKinds),
                State = reader.Choice("state", Enums.JobStates),
                Status = reader.NullableChoice("status", ValidationStatus.All),
                Reason = reader.NullableStr("reason"),
                CreatedUtc = reader.Utc("createdUtc"),
                UpdatedUtc = reader.Utc("updatedUtc"),
                Host = reader.Identifier("host"),
                Pid = reader.OptInt32("pid", 1),
                Command = reader.Strings("command"),
                ResultPath = reader.OptStr("resultPath", 1),
                Phase = reader.OptChoice("phase", Enums.JobPhases),
                Watchdog =JobWatchdog.Read(reader.Object("watchdog")),
                EventsOffset = reader.Int("eventsOffset", 0),
            };
            reader.Done();
            return job;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("jobId", JsonValue.From(JobId)).Set("kind", JsonValue.From(Kind))
            .Set("state", JsonValue.From(State)).Set("status", JsonValue.From(Status)).Set("reason", JsonValue.From(Reason))
            .Set("createdUtc", JsonValue.From(CreatedUtc)).Set("updatedUtc", JsonValue.From(UpdatedUtc)).Set("host", JsonValue.From(Host))
            .SetIf("pid", Pid).Set("command", JsonBuild.Strings(Command)).SetIf("resultPath", ResultPath)
            .SetIf("phase", Phase).Set("watchdog", Watchdog.Write()).Set("eventsOffset", JsonValue.From(EventsOffset));

        public string ToJsonString() => ToJson().Write(true);
    }

    /// <summary>One line of <c>events.jsonl</c> (<c>gdv.event/1</c>), the append-only log a watcher follows.</summary>
    public sealed class EventDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Event;

        public long Seq { get; set; }

        public string Utc { get; set; }

        public string JobId { get; set; }

        public string Type { get; set; }

        public string Step { get; set; }

        public string Status { get; set; }

        public string Message { get; set; }

        public JsonValue Data { get; set; }

        public static EventDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static EventDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Event);
            var evt = new EventDocument
            {
                Seq = reader.Int("seq", 0),
                Utc = reader.Utc("utc"),
                JobId = reader.Identifier("jobId"),
                Type = reader.Choice("type", Enums.EventTypes),
                Step = reader.OptStr("step"),
                Status = reader.OptChoice("status", ValidationStatus.All),
                Message = reader.OptStr("message"),
                Data = reader.OptTree("data"),
            };
            reader.Done();
            return evt;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("seq", JsonValue.From(Seq)).Set("utc", JsonValue.From(Utc))
            .Set("jobId", JsonValue.From(JobId)).Set("type", JsonValue.From(Type))
            .SetIf("step", Step).SetIf("status", Status).SetIf("message", Message).SetIf("data", Data);

        /// <summary>The single-line form appended to events.jsonl.</summary>
        public string ToJsonLine() => ToJson().Write(false);

        public string ToJsonString() => ToJson().Write(true);
    }
}
