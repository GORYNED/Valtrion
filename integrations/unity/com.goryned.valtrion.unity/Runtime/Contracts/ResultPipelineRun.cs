using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    /// <summary>What became of one stage of a pipeline job (<c>pipelineRun.stages[]</c> of a result).</summary>
    public sealed class ResultStageRecord
    {
        /// <summary>ran, carried, reused or not-run.</summary>
        public static readonly string[] Dispositions = { "ran", "carried", "reused", "not-run" };

        /// <summary>1-based position of the stage in the pipeline.</summary>
        public int Index { get; set; }

        public string Stage { get; set; }

        /// <summary><c>ran</c> in this job, <c>carried</c> over from an earlier run, <c>reused</c> from the result cache, or <c>not-run</c>.</summary>
        public string Disposition { get; set; }

        /// <summary>The status the stage ended with (here or in the run it was carried from); null when it did not run.</summary>
        public string Status { get; set; }

        public string Reason { get; set; }

        public string Plan { get; set; }

        public string PlanSha256 { get; set; }

        public string StageSha256 { get; set; }

        public string RunId { get; set; }

        public string DeviceId { get; set; }

        /// <summary>A carried stage: the run that ran it; a reused stage: the run whose result was reused.</summary>
        public string FromRun { get; set; }

        internal static ResultStageRecord Read(ContractReader reader)
        {
            var record = new ResultStageRecord
            {
                Index = reader.Int32("index", 1),
                Stage = reader.Choice("stage", Enums.Stages),
                Disposition = reader.Choice("disposition", Dispositions),
                Status = reader.OptChoice("status", ValidationStatus.All),
                Reason = reader.OptStr("reason", 1),
                Plan = reader.OptIdentifier("plan"),
                PlanSha256 = reader.Has("planSha256") ? reader.Hex("planSha256", 64) : null,
                StageSha256 = reader.Has("stageSha256") ? reader.Hex("stageSha256", 64) : null,
                RunId = reader.OptIdentifier("runId"),
                DeviceId = reader.OptIdentifier("deviceId"),
                FromRun = reader.OptIdentifier("fromRun"),
            };
            reader.Done();
            return record;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("index", JsonValue.From(Index)).Set("stage", JsonValue.From(Stage)).Set("disposition", JsonValue.From(Disposition))
            .SetIf("status", Status).SetIf("reason", Reason).SetIf("plan", Plan).SetIf("planSha256", PlanSha256)
            .SetIf("stageSha256", StageSha256).SetIf("runId", RunId).SetIf("deviceId", DeviceId).SetIf("fromRun", FromRun);
    }

    /// <summary>The stage record of a pipeline job (<c>pipelineRun</c> of its result).</summary>
    public sealed class ResultPipelineRun
    {
        public string PipelineId { get; set; }

        /// <summary>The earlier run (job id) the stages that were not selected were carried from; null for an ordinary run.</summary>
        public string ResumedFrom { get; set; }

        /// <summary>True when the job ran on after a failed run stage (<c>--keep-going</c>); null when not recorded.</summary>
        public bool? KeepGoing { get; set; }

        public List<ResultStageRecord> Stages { get; set; } = new List<ResultStageRecord>();

        internal static ResultPipelineRun Read(ContractReader reader)
        {
            var run = new ResultPipelineRun
            {
                PipelineId = reader.Identifier("pipelineId"),
                ResumedFrom = reader.OptIdentifier("resumedFrom"),
                KeepGoing = reader.OptBool("keepGoing"),
                Stages = reader.Items("stages", ResultStageRecord.Read),
            };
            reader.Done();
            return run;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("pipelineId", JsonValue.From(PipelineId)).SetIf("resumedFrom", ResumedFrom).SetIf("keepGoing", KeepGoing)
            .Set("stages", JsonBuild.Array(Stages, stage => stage.Write()));
    }

    /// <summary>Where a run stage result answered from the result cache came from (<c>reusedFrom</c> of a result).</summary>
    public sealed class ResultReusedFrom
    {
        public string RunId { get; set; }

        public string JobId { get; set; }

        /// <summary>SHA-256 of the result cache key.</summary>
        public string Key { get; set; }

        public string StoredUtc { get; set; }

        internal static ResultReusedFrom Read(ContractReader reader)
        {
            var reused = new ResultReusedFrom
            {
                RunId = reader.Identifier("runId"),
                JobId = reader.OptIdentifier("jobId"),
                Key = reader.Hex("key", 64),
                StoredUtc = reader.OptUtc("storedUtc"),
            };
            reader.Done();
            return reused;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("runId", JsonValue.From(RunId)).SetIf("jobId", JobId).Set("key", JsonValue.From(Key)).SetIf("storedUtc", StoredUtc);
    }
}
