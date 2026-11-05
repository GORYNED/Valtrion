using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    public sealed class PipelineCandidate
    {
        public string Repo { get; set; }

        /// <summary>The full commit under test. Optional, or all zeros: the commit of the checkout is used and recorded.</summary>
        public string Sha { get; set; }

        public string Profile { get; set; }

        internal static PipelineCandidate Read(ContractReader reader)
        {
            var candidate = new PipelineCandidate
            {
                Repo = reader.Str("repo"),
                Sha = reader.Has("sha") ? reader.Hex("sha", 40) : null,
                Profile = reader.Identifier("profile"),
            };
            reader.Done();
            return candidate;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("repo", JsonValue.From(Repo)).SetIf("sha", Sha).Set("profile", JsonValue.From(Profile));
    }

    /// <summary>Which device a pipeline wants: a class, a concrete id, or both. Plans require capabilities, not UDIDs.</summary>
    public sealed class PipelineDevice
    {
        public string Class { get; set; }

        public string Id { get; set; }

        public string Platform { get; set; }

        internal static PipelineDevice Read(ContractReader reader)
        {
            var device = new PipelineDevice
            {
                Class = reader.OptIdentifier("class"),
                Id = reader.OptIdentifier("id"),
                Platform = reader.OptChoice("platform", Enums.Platforms),
            };
            reader.Done();
            return device;
        }

        internal JsonValue Write() => JsonValue.NewObject().SetIf("class", Class).SetIf("id", Id).SetIf("platform", Platform);
    }

    /// <summary>A file pushed into the application's data root before it starts (<c>prelaunchFiles</c> of a run stage).</summary>
    public sealed class PrelaunchFile
    {
        /// <summary>Absolute, or relative to the pipeline file.</summary>
        public string Local { get; set; }

        /// <summary>Relative to the application's data root.</summary>
        public string Remote { get; set; }

        internal static PrelaunchFile Read(ContractReader reader)
        {
            var file = new PrelaunchFile { Local = reader.Str("local"), Remote = reader.Str("remote") };
            reader.Done();
            return file;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("local", JsonValue.From(Local)).Set("remote", JsonValue.From(Remote));
    }

    public sealed class PipelineStage
    {
        /// <summary>build, install, run or collect.</summary>
        public string Stage { get; set; }

        public string Plan { get; set; }

        public string Variant { get; set; }

        public int? Repeat { get; set; }

        public bool? ColdLaunch { get; set; }

        public int? TimeoutSec { get; set; }

        public JsonValue Args { get; set; }

        /// <summary>Files pushed through the platform adapter before the application starts; null when the stage names none.</summary>
        public List<PrelaunchFile> PrelaunchFiles { get; set; }

        /// <summary>Run stages: output paths relative to the project folder that the host removes when the job ends; null when none.</summary>
        public List<string> Cleanup { get; set; }

        internal static PipelineStage Read(ContractReader reader)
        {
            var stage = new PipelineStage
            {
                Stage = reader.Choice("stage", Enums.Stages),
                Plan = reader.OptStr("plan", 1),
                Variant = reader.OptStr("variant", 1),
                Repeat = reader.OptInt32("repeat", 1),
                ColdLaunch = reader.OptBool("coldLaunch"),
                TimeoutSec = reader.OptInt32("timeoutSec", 1),
                Args = reader.OptTree("args"),
                PrelaunchFiles = reader.OptItems("prelaunchFiles", PrelaunchFile.Read),
                Cleanup = reader.OptStrings("cleanup"),
            };
            reader.Done();
            return stage;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("stage", JsonValue.From(Stage));
            node.SetIf("plan", Plan).SetIf("variant", Variant).SetIf("repeat", Repeat).SetIf("coldLaunch", ColdLaunch)
                .SetIf("timeoutSec", TimeoutSec).SetIf("args", Args);
            if (PrelaunchFiles != null)
                node.Set("prelaunchFiles", JsonBuild.Array(PrelaunchFiles, file => file.Write()));
            if (Cleanup != null)
                node.Set("cleanup", JsonBuild.Strings(Cleanup));
            return node;
        }
    }

    /// <summary>A pipeline (<c>gdv.pipeline/1</c>): build, install, a series of runs, collect and compare for one candidate.</summary>
    public sealed class PipelineDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Pipeline;

        public string Id { get; set; }

        public PipelineCandidate Candidate { get; set; }

        public PipelineDevice Device { get; set; }

        public List<PipelineStage> Stages { get; set; } = new List<PipelineStage>();

        public string Compare { get; set; }

        public static PipelineDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static PipelineDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Pipeline);
            var pipeline = new PipelineDocument
            {
                Id = reader.Identifier("id"),
                Candidate = PipelineCandidate.Read(reader.Object("candidate")),
                Device = PipelineDevice.Read(reader.Object("device")),
                Stages = reader.Items("stages", PipelineStage.Read, 1),
                Compare = reader.OptStr("compare", 1),
            };
            reader.Done();
            return pipeline;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("id", JsonValue.From(Id))
            .Set("candidate", Candidate.Write()).Set("device", Device.Write())
            .Set("stages", JsonBuild.Array(Stages, stage => stage.Write()))
            .SetIf("compare", Compare);

        public string ToJsonString() => ToJson().Write(true);
    }
}
