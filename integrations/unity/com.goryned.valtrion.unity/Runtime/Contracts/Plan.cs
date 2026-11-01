using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    /// <summary>One step of a plan. <see cref="Args"/> holds every other property: step arguments are free-form and owned by the step type.</summary>
    public sealed class PlanStep
    {
        public string Type { get; set; }

        public int TimeoutSec { get; set; }

        public JsonValue Args { get; set; } = JsonValue.NewObject();

        internal static PlanStep Read(ContractReader reader)
        {
            var step = new PlanStep { Type = reader.Identifier("type"), TimeoutSec = reader.Int32("timeoutSec", 1) };
            step.Args = reader.Rest();
            reader.Done();
            return step;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("type", JsonValue.From(Type)).Set("timeoutSec", JsonValue.From(TimeoutSec));
            foreach (var member in Args.Members)
                node.Set(member.Key, member.Value);
            return node;
        }
    }

    public sealed class MetricSpec
    {
        public string Unit { get; set; }

        public string Direction { get; set; }

        public double? Budget { get; set; }

        public double? RegressionPct { get; set; }

        public int? MinSamples { get; set; }

        internal static MetricSpec Read(ContractReader reader)
        {
            var spec = new MetricSpec
            {
                Unit = reader.Str("unit"),
                Direction = reader.Choice("direction", Enums.Directions),
                Budget = reader.OptNum("budget"),
                RegressionPct = reader.OptNum("regressionPct", 0),
                MinSamples = reader.OptInt32("minSamples", 1),
            };
            reader.Done();
            return spec;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("unit", JsonValue.From(Unit)).Set("direction", JsonValue.From(Direction))
            .SetIf("budget", Budget).SetIf("regressionPct", RegressionPct).SetIf("minSamples", MinSamples);
    }

    public sealed class PlanRequirements
    {
        public List<string> Platforms { get; set; }

        public List<string> Capabilities { get; set; }

        public string Profile { get; set; }

        public List<string> Evidence { get; set; }

        internal static PlanRequirements Read(ContractReader reader)
        {
            var requirements = new PlanRequirements
            {
                Platforms = reader.OptChoices("platforms", Enums.Platforms, 1),
                Capabilities = reader.OptStrings("capabilities"),
                Profile = reader.OptIdentifier("profile"),
                Evidence = reader.OptChoices("evidence", EvidenceKind.All, 1),
            };
            reader.Done();
            return requirements;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject();
            if (Platforms != null) node.Set("platforms", JsonBuild.Strings(Platforms));
            if (Capabilities != null) node.Set("capabilities", JsonBuild.Strings(Capabilities));
            node.SetIf("profile", Profile);
            if (Evidence != null) node.Set("evidence", JsonBuild.Strings(Evidence));
            return node;
        }
    }

    /// <summary>A validation plan (<c>gdv.plan/1</c>): ordered steps, each with a mandatory timeout, plus the metrics they report.</summary>
    public sealed class PlanDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Plan;

        public string Id { get; set; }

        public string Kind { get; set; }

        public PlanRequirements Requires { get; set; }

        public string DataPolicy { get; set; }

        public int BudgetSeconds { get; set; }

        public int? Repeat { get; set; }

        public int? Warmup { get; set; }

        public bool? ContinueOnFail { get; set; }

        public List<PlanStep> Setup { get; set; }

        public List<PlanStep> Steps { get; set; } = new List<PlanStep>();

        public Dictionary<string, MetricSpec> Metrics { get; set; }

        public string Cleanup { get; set; }

        public static PlanDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static PlanDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Plan);
            var plan = new PlanDocument
            {
                Id = reader.Identifier("id"),
                Kind = reader.Choice("kind", Enums.PlanKinds),
                Requires = reader.OptObject("requires") is ContractReader requires ? PlanRequirements.Read(requires) : null,
                DataPolicy = reader.Choice("dataPolicy", Enums.DataPolicies),
                BudgetSeconds = reader.Int32("budgetSeconds", 1),
                Repeat = reader.OptInt32("repeat", 1),
                Warmup = reader.OptInt32("warmup", 0),
                ContinueOnFail = reader.OptBool("continueOnFail"),
                Setup = reader.OptItems("setup", PlanStep.Read),
                Steps = reader.Items("steps", PlanStep.Read, 1),
                Metrics = reader.OptMap("metrics", MetricSpec.Read),
                Cleanup = reader.OptChoice("cleanup", Enums.Cleanups),
            };
            reader.Done();
            CheckExternalUi(plan);
            return plan;
        }

        /// <summary>External UI automation distorts a measurement, so a performance plan must not run an <c>external.ui</c> step
        /// between <c>probe.start</c> and <c>probe.stop</c>.</summary>
        static void CheckExternalUi(PlanDocument plan)
        {
            if (plan.Kind != "performance")
                return;
            var measuring = false;
            for (var index = 0; index < plan.Steps.Count; index++)
            {
                var type = plan.Steps[index].Type;
                if (type == "probe.start")
                    measuring = true;
                else if (type == "probe.stop")
                    measuring = false;
                else if (type == "external.ui" && measuring)
                    throw new ContractException($"$.steps[{index}]: external.ui is forbidden between probe.start and probe.stop in a performance plan");
            }
        }

        public JsonValue ToJson()
        {
            var node = JsonValue.NewObject().Set("schema", JsonValue.From(Schema)).Set("id", JsonValue.From(Id)).Set("kind", JsonValue.From(Kind));
            if (Requires != null) node.Set("requires", Requires.Write());
            node.Set("dataPolicy", JsonValue.From(DataPolicy)).Set("budgetSeconds", JsonValue.From(BudgetSeconds))
                .SetIf("repeat", Repeat).SetIf("warmup", Warmup).SetIf("continueOnFail", ContinueOnFail);
            if (Setup != null) node.Set("setup", JsonBuild.Array(Setup, step => step.Write()));
            node.Set("steps", JsonBuild.Array(Steps, step => step.Write()));
            if (Metrics != null) node.Set("metrics", JsonBuild.Map(Metrics, metric => metric.Write()));
            return node.SetIf("cleanup", Cleanup);
        }

        public string ToJsonString() => ToJson().Write(true);
    }
}
