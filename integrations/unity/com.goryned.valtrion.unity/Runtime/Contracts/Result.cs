using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    public sealed class MetricStats
    {
        public long N { get; set; }

        public double Median { get; set; }

        public double P90 { get; set; }

        public double Max { get; set; }

        public double? Stdev { get; set; }

        internal static MetricStats Read(ContractReader reader)
        {
            var stats = new MetricStats
            {
                N = reader.Int("n", 0),
                Median = reader.Num("median"),
                P90 = reader.Num("p90"),
                Max = reader.Num("max"),
                Stdev = reader.OptNum("stdev", 0),
            };
            reader.Done();
            return stats;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("n", JsonValue.From(N)).Set("median", JsonValue.From(Median)).Set("p90", JsonValue.From(P90)).Set("max", JsonValue.From(Max))
            .SetIf("stdev", Stdev);
    }

    public sealed class ResultCandidate
    {
        public string Repo { get; set; }

        public string Sha { get; set; }

        public string Profile { get; set; }

        public string Unity { get; set; }

        public bool Dirty { get; set; }

        internal static ResultCandidate Read(ContractReader reader)
        {
            var candidate = new ResultCandidate
            {
                Repo = reader.Str("repo"),
                Sha = reader.Hex("sha", 40),
                Profile = reader.Identifier("profile"),
                Unity = reader.Str("unity"),
                Dirty = reader.Bool("dirty"),
            };
            reader.Done();
            return candidate;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("repo", JsonValue.From(Repo)).Set("sha", JsonValue.From(Sha)).Set("profile", JsonValue.From(Profile))
            .Set("unity", JsonValue.From(Unity)).Set("dirty", JsonValue.From(Dirty));
    }

    public sealed class ResultTarget
    {
        public string Platform { get; set; }

        public string DeviceId { get; set; }

        public string DeviceClass { get; set; }

        public string Model { get; set; }

        public string Os { get; set; }

        public int OsMajor { get; set; }

        public string Evidence { get; set; }

        internal static ResultTarget Read(ContractReader reader)
        {
            var target = new ResultTarget
            {
                Platform = reader.Choice("platform", Enums.Platforms),
                DeviceId = reader.OptIdentifier("deviceId"),
                DeviceClass = reader.Identifier("deviceClass"),
                Model = reader.OptStr("model"),
                Os = reader.OptStr("os"),
                OsMajor = reader.Int32("osMajor", 0),
                Evidence = reader.Choice("evidence", EvidenceKind.All),
            };
            reader.Done();
            return target;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("platform", JsonValue.From(Platform)).SetIf("deviceId", DeviceId).Set("deviceClass", JsonValue.From(DeviceClass))
            .SetIf("model", Model).SetIf("os", Os).Set("osMajor", JsonValue.From(OsMajor)).Set("evidence", JsonValue.From(Evidence));
    }

    /// <summary>The host that executed the run. The choice is deterministic and recorded: <see cref="Transport"/> is local or ssh,
    /// <see cref="SelectedBy"/> is local (the caller's machine satisfied the request), capability (a remote host did) or pinned.</summary>
    public sealed class ResultHost
    {
        public string Id { get; set; }

        public string Transport { get; set; }

        public string SelectedBy { get; set; }

        public Dictionary<string, string> Tools { get; set; }

        internal static ResultHost Read(ContractReader reader)
        {
            var host = new ResultHost
            {
                Id = reader.Identifier("id"),
                Transport = reader.Choice("transport", Enums.TransportKinds),
                SelectedBy = reader.Choice("selectedBy", Enums.HostSelections),
                Tools = reader.StringMap("tools"),
            };
            reader.Done();
            return host;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("id", JsonValue.From(Id)).Set("transport", JsonValue.From(Transport))
                .Set("selectedBy", JsonValue.From(SelectedBy));
            if (Tools != null) node.Set("tools", JsonBuild.StringMap(Tools));
            return node;
        }
    }

    public sealed class ResultPlan
    {
        public string Id { get; set; }

        public string Sha256 { get; set; }

        internal static ResultPlan Read(ContractReader reader)
        {
            var plan = new ResultPlan { Id = reader.Identifier("id"), Sha256 = reader.Hex("sha256", 64) };
            reader.Done();
            return plan;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("id", JsonValue.From(Id)).Set("sha256", JsonValue.From(Sha256));
    }

    /// <summary>Per-layer outcome; null means the layer did not run. A layer is never inferred from its neighbour: compile, export, native build, sign, install, launch and scenario are reported separately.</summary>
    public sealed class ResultLayers
    {
        public string Compile { get; set; }

        public string Export { get; set; }

        public string NativeBuild { get; set; }

        public string Sign { get; set; }

        public string Install { get; set; }

        public string Launch { get; set; }

        public string Scenario { get; set; }

        internal static ResultLayers Read(ContractReader reader)
        {
            var layers = new ResultLayers
            {
                Compile = reader.OptChoice("compile", ValidationStatus.All),
                Export = reader.OptChoice("export", ValidationStatus.All),
                NativeBuild = reader.OptChoice("nativeBuild", ValidationStatus.All),
                Sign = reader.OptChoice("sign", ValidationStatus.All),
                Install = reader.OptChoice("install", ValidationStatus.All),
                Launch = reader.OptChoice("launch", ValidationStatus.All),
                Scenario = reader.OptChoice("scenario", ValidationStatus.All),
            };
            reader.Done();
            return layers;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .SetIf("compile", Compile).SetIf("export", Export).SetIf("nativeBuild", NativeBuild)
            .SetIf("sign", Sign).SetIf("install", Install).SetIf("launch", Launch).SetIf("scenario", Scenario);
    }

    public sealed class ResultStep
    {
        public string Type { get; set; }

        public string Status { get; set; }

        public long StartMs { get; set; }

        public long DurationMs { get; set; }

        public Dictionary<string, double> Marks { get; set; }

        public string Message { get; set; }

        internal static ResultStep Read(ContractReader reader)
        {
            var step = new ResultStep
            {
                Type = reader.Identifier("type"),
                Status = reader.Choice("status", ValidationStatus.All),
                StartMs = reader.Int("startMs", 0),
                DurationMs = reader.Int("durationMs", 0),
                Marks = reader.NumberMap("marks"),
                Message = reader.OptStr("message"),
            };
            reader.Done();
            return step;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("type", JsonValue.From(Type)).Set("status", JsonValue.From(Status))
                .Set("startMs", JsonValue.From(StartMs)).Set("durationMs", JsonValue.From(DurationMs));
            if (Marks != null) node.Set("marks", JsonBuild.NumberMap(Marks));
            return node.SetIf("message", Message);
        }
    }

    public sealed class ResultMetric
    {
        public string Name { get; set; }

        public string Unit { get; set; }

        public List<double> Samples { get; set; } = new List<double>();

        public MetricStats Stats { get; set; }

        internal static ResultMetric Read(ContractReader reader)
        {
            var metric = new ResultMetric
            {
                Name = reader.Identifier("name"),
                Unit = reader.Str("unit"),
                Samples = reader.Numbers("samples"),
                Stats = MetricStats.Read(reader.Object("stats")),
            };
            reader.Done();
            return metric;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("name", JsonValue.From(Name)).Set("unit", JsonValue.From(Unit))
            .Set("samples", JsonBuild.Numbers(Samples)).Set("stats", Stats.Write());
    }

    public sealed class ResultComparison
    {
        public string Baseline { get; set; }

        public string Verdict { get; set; }

        public List<string> Details { get; set; } = new List<string>();

        internal static ResultComparison Read(ContractReader reader)
        {
            var comparison = new ResultComparison
            {
                Baseline = reader.Str("baseline"),
                Verdict = reader.Choice("verdict", ComparisonVerdict.All),
                Details = reader.Strings("details"),
            };
            reader.Done();
            return comparison;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("baseline", JsonValue.From(Baseline)).Set("verdict", JsonValue.From(Verdict)).Set("details", JsonBuild.Strings(Details));
    }

    public sealed class ResultArtifact
    {
        public string Kind { get; set; }

        public string Path { get; set; }

        public string Sha256 { get; set; }

        internal static ResultArtifact Read(ContractReader reader)
        {
            var artifact = new ResultArtifact { Kind = reader.Str("kind"), Path = reader.Str("path"), Sha256 = reader.Hex("sha256", 64) };
            reader.Done();
            return artifact;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("kind", JsonValue.From(Kind)).Set("path", JsonValue.From(Path)).Set("sha256", JsonValue.From(Sha256));
    }

    /// <summary>Which commit the project folder was at when the job's build stage settled it (<c>--sha</c>): the Unity project folder,
    /// the commit, and for a managed checkout its id, whether it was reset from another commit, created, and whether the commit was
    /// fetched from the remote first. An unmanaged folder is only verified (<see cref="Switched"/> false).</summary>
    public sealed class ResultCheckout
    {
        public string Folder { get; set; }

        public string Sha { get; set; }

        public bool Switched { get; set; }

        public bool Fetched { get; set; }

        /// <summary>The managed checkout (host.json projects.&lt;name&gt;.checkouts[].id); null for an unmanaged folder.</summary>
        public string Id { get; set; }

        /// <summary>True when the build stage had to create the managed checkout; null when not recorded.</summary>
        public bool? Created { get; set; }

        /// <summary>The commit before the switch; null when not recorded.</summary>
        public string PreviousSha { get; set; }

        internal static ResultCheckout Read(ContractReader reader)
        {
            var checkout = new ResultCheckout
            {
                Folder = reader.Str("folder"),
                Sha = reader.Hex("sha", 40),
                Switched = reader.Bool("switched"),
                Fetched = reader.Bool("fetched"),
                Id = reader.OptStr("id", 1),
                Created = reader.OptBool("created"),
                PreviousSha = reader.Has("previousSha") ? reader.Hex("previousSha", 40) : null,
            };
            reader.Done();
            return checkout;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("folder", JsonValue.From(Folder)).Set("sha", JsonValue.From(Sha))
            .Set("switched", JsonValue.From(Switched)).Set("fetched", JsonValue.From(Fetched))
            .SetIf("id", Id).SetIf("created", Created).SetIf("previousSha", PreviousSha);
    }

    /// <summary>A declared output path and why it was kept or refused.</summary>
    public sealed class CleanupPathReason
    {
        public string Path { get; set; }

        public string Reason { get; set; }

        internal static CleanupPathReason Read(ContractReader reader)
        {
            var item = new CleanupPathReason { Path = reader.Str("path"), Reason = reader.Str("reason") };
            reader.Done();
            return item;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("path", JsonValue.From(Path)).Set("reason", JsonValue.From(Reason));
    }

    /// <summary>What became of the output paths a job declared for removal (<c>cleanup</c> of <c>gdv.job/1</c> and <c>gdv.result/1</c>):
    /// paths are relative to <see cref="Base"/>, and each declared path is in exactly one list.</summary>
    public sealed class OutputCleanup
    {
        public string Base { get; set; }

        public List<string> Removed { get; set; } = new List<string>();

        public List<CleanupPathReason> Kept { get; set; } = new List<CleanupPathReason>();

        public List<CleanupPathReason> Refused { get; set; } = new List<CleanupPathReason>();

        internal static OutputCleanup Read(ContractReader reader)
        {
            var cleanup = new OutputCleanup
            {
                Base = reader.Str("base"),
                Removed = reader.Strings("removed"),
                Kept = reader.Items("kept", CleanupPathReason.Read),
                Refused = reader.Items("refused", CleanupPathReason.Read),
            };
            reader.Done();
            return cleanup;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("base", JsonValue.From(Base)).Set("removed", JsonBuild.Strings(Removed))
            .Set("kept", JsonBuild.Array(Kept, item => item.Write())).Set("refused", JsonBuild.Array(Refused, item => item.Write()));
    }

    /// <summary>The build artifact a run was made with, published outside the job's work folder: built by the job, reused from the build
    /// cache or installed by key. Absent for a pipeline without a build.</summary>
    public sealed class ResultBuild
    {
        public string Key { get; set; }

        public string Path { get; set; }

        /// <summary>file or directory; null when not recorded.</summary>
        public string Kind { get; set; }

        public string Sha256 { get; set; }

        public long SizeBytes { get; set; }

        public string CandidateSha { get; set; }

        public bool Dirty { get; set; }

        public string Profile { get; set; }

        public string Platform { get; set; }

        public Dictionary<string, string> Toolchain { get; set; }

        /// <summary>True when the job did not build it (cache hit, or install by key).</summary>
        public bool Reused { get; set; }

        public string BuiltByJob { get; set; }

        internal static ResultBuild Read(ContractReader reader)
        {
            var build = new ResultBuild
            {
                Key = reader.OptStr("key", 1),
                Path = reader.Str("path"),
                Kind = reader.OptChoice("kind", Enums.BuildKinds),
                Sha256 = reader.Hex("sha256", 64),
                SizeBytes = reader.Int("sizeBytes", 0),
                CandidateSha = reader.Hex("candidateSha", 40),
                Dirty = reader.Bool("dirty"),
                Profile = reader.Identifier("profile"),
                Platform = reader.OptStr("platform", 1),
                Toolchain = reader.StringMap("toolchain"),
                Reused = reader.Bool("reused"),
                BuiltByJob = reader.OptStr("builtByJob", 1),
            };
            reader.Done();
            return build;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().SetIf("key", Key).Set("path", JsonValue.From(Path)).SetIf("kind", Kind)
                .Set("sha256", JsonValue.From(Sha256)).Set("sizeBytes", JsonValue.From(SizeBytes))
                .Set("candidateSha", JsonValue.From(CandidateSha)).Set("dirty", JsonValue.From(Dirty))
                .Set("profile", JsonValue.From(Profile)).SetIf("platform", Platform);
            if (Toolchain != null) node.Set("toolchain", JsonBuild.StringMap(Toolchain));
            node.Set("reused", JsonValue.From(Reused)).SetIf("builtByJob", BuiltByJob);
            return node;
        }
    }

    /// <summary>The result of one run (<c>gdv.result/1</c>), written by the Runtime or the host; the same envelope for every platform.</summary>
    public sealed class ResultDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Result;

        public string RunId { get; set; }

        public string Status { get; set; }

        /// <summary>Why the status is not PASS; null for PASS.</summary>
        public string Reason { get; set; }

        public ResultCandidate Candidate { get; set; }

        public ResultTarget Target { get; set; }

        public ResultHost Host { get; set; }

        public ResultPlan Plan { get; set; }

        public string StartedUtc { get; set; }

        public string FinishedUtc { get; set; }

        public ResultLayers Layers { get; set; }

        public List<ResultStep> Steps { get; set; } = new List<ResultStep>();

        public List<ResultMetric> Metrics { get; set; } = new List<ResultMetric>();

        public ResultComparison Comparison { get; set; }

        /// <summary>The commit the build stage settled the project folder at; null when the job's build stage did not settle one.</summary>
        public ResultCheckout Checkout { get; set; }

        /// <summary>The build the run was made with; null for a pipeline without a build.</summary>
        public ResultBuild Build { get; set; }

        /// <summary>What became of the outputs the run stages declared for removal; null when they declared none.</summary>
        public OutputCleanup Cleanup { get; set; }

        /// <summary>What became of every stage of the pipeline job; null for a result that is not a pipeline job's.</summary>
        public ResultPipelineRun PipelineRun { get; set; }

        /// <summary>The run a result answered from the result cache came from; null for a result this job produced.</summary>
        public ResultReusedFrom ReusedFrom { get; set; }

        public List<ResultArtifact> Artifacts { get; set; } = new List<ResultArtifact>();

        /// <summary>What remains unverified; required, and an empty list claims that nothing does.</summary>
        public List<string> Unverified { get; set; } = new List<string>();

        public int ExitCode => ValidationStatus.ExitCode(Status);

        public static ResultDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static ResultDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Result);
            var result = new ResultDocument
            {
                RunId = reader.Identifier("runId"),
                Status = reader.Choice("status", ValidationStatus.All),
                Reason = reader.NullableStr("reason"),
                Candidate = ResultCandidate.Read(reader.Object("candidate")),
                Target = ResultTarget.Read(reader.Object("target")),
                Host = ResultHost.Read(reader.Object("host")),
                Plan = ResultPlan.Read(reader.Object("plan")),
                StartedUtc = reader.Utc("startedUtc"),
                FinishedUtc = reader.Utc("finishedUtc"),
                Layers = ResultLayers.Read(reader.Object("layers")),
                Steps = reader.Items("steps", ResultStep.Read),
                Metrics = reader.Items("metrics", ResultMetric.Read),
                Comparison = reader.OptObject("comparison") is ContractReader comparison ? ResultComparison.Read(comparison) : null,
                Checkout = reader.OptObject("checkout") is ContractReader checkout ? ResultCheckout.Read(checkout) : null,
                Build = reader.OptObject("build") is ContractReader build ? ResultBuild.Read(build) : null,
                Cleanup = reader.OptObject("cleanup") is ContractReader cleanup ? OutputCleanup.Read(cleanup) : null,
                PipelineRun = reader.OptObject("pipelineRun") is ContractReader run ? ResultPipelineRun.Read(run) : null,
                ReusedFrom = reader.OptObject("reusedFrom") is ContractReader reused ? ResultReusedFrom.Read(reused) : null,
                Artifacts = reader.Items("artifacts", ResultArtifact.Read),
                Unverified = reader.Strings("unverified"),
            };
            reader.Done();
            CheckConsistency(result);
            return result;
        }

        /// <summary>A PASS result has no reason and no layer that is not PASS; any other status says why.</summary>
        static void CheckConsistency(ResultDocument result)
        {
            if (result.Status == ValidationStatus.Pass && result.Reason != null)
                throw new ContractException("$.reason: a PASS result has no reason");
            if (result.Status != ValidationStatus.Pass && string.IsNullOrEmpty(result.Reason))
                throw new ContractException($"$.reason: a {result.Status} result needs a reason");
            if (result.Status != ValidationStatus.Pass)
                return;
            var layers = result.Layers;
            foreach (var layer in new[] { layers.Compile, layers.Export, layers.NativeBuild, layers.Sign, layers.Install, layers.Launch, layers.Scenario })
            {
                if (layer != null && layer != ValidationStatus.Pass)
                    throw new ContractException($"$.layers: a PASS result cannot have a {layer} layer");
            }
        }

        public JsonValue ToJson()
        {
            var node = JsonValue.NewObject().Set("schema", JsonValue.From(Schema)).Set("runId", JsonValue.From(RunId))
                .Set("status", JsonValue.From(Status)).Set("reason", JsonValue.From(Reason))
                .Set("candidate", Candidate.Write()).Set("target", Target.Write());
            node.Set("host", Host.Write()).Set("plan", Plan.Write()).Set("startedUtc", JsonValue.From(StartedUtc)).Set("finishedUtc", JsonValue.From(FinishedUtc))
                .Set("layers", Layers.Write()).Set("steps", JsonBuild.Array(Steps, step => step.Write()))
                .Set("metrics", JsonBuild.Array(Metrics, metric => metric.Write()));
            if (Comparison != null) node.Set("comparison", Comparison.Write());
            if (Checkout != null) node.Set("checkout", Checkout.Write());
            if (Build != null) node.Set("build", Build.Write());
            if (Cleanup != null) node.Set("cleanup", Cleanup.Write());
            if (PipelineRun != null) node.Set("pipelineRun", PipelineRun.Write());
            if (ReusedFrom != null) node.Set("reusedFrom", ReusedFrom.Write());
            // Both lists are always written: an empty list is a claim ("nothing was left unverified"), omission is not.
            node.Set("artifacts", JsonBuild.Array(Artifacts, artifact => artifact.Write()));
            node.Set("unverified", JsonBuild.Strings(Unverified));
            return node;
        }

        public string ToJsonString() => ToJson().Write(true);
    }
}
