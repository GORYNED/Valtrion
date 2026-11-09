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
            CheckRepeat(plan);
            CheckExternalUi(plan);
            return plan;
        }

        static bool Whole(JsonValue value, long minimum) =>
            value != null && value.Kind == JsonKind.Number && value.AsNumber == System.Math.Floor(value.AsNumber) && value.AsNumber >= minimum && value.AsNumber <= int.MaxValue;

        /// <summary>The shape of a <c>repeat</c> step: a whole <c>count</c> of at least 1 and a non-empty list of inner steps, each with a <c>type</c>
        /// (not <c>repeat</c>) and a <c>timeoutSec</c>.</summary>
        static void CheckRepeat(PlanDocument plan)
        {
            if (plan.Setup != null)
            {
                for (var index = 0; index < plan.Setup.Count; index++)
                    CheckRepeatStep(plan.Setup[index], $"$.setup[{index}]");
            }
            for (var index = 0; index < plan.Steps.Count; index++)
                CheckRepeatStep(plan.Steps[index], $"$.steps[{index}]");
        }

        static void CheckRepeatStep(PlanStep step, string path)
        {
            if (step.Type != "repeat")
                return;
            step.Args.TryGet("count", out var count);
            if (!Whole(count, 1))
                throw new ContractException($"{path}: repeat needs a whole 'count' of at least 1");
            if (!step.Args.TryGet("steps", out var inner) || inner.Kind != JsonKind.Array || inner.Count == 0)
                throw new ContractException($"{path}: repeat needs a non-empty 'steps' list");
            var number = 0;
            foreach (var child in inner.Items)
            {
                var where = $"{path}.steps[{number++}]";
                if (child.Kind != JsonKind.Object)
                    throw new ContractException($"{where}: an inner step is an object with 'type' and 'timeoutSec'");
                if (!child.TryGet("type", out var type) || type.Kind != JsonKind.String || type.AsString.Length == 0)
                    throw new ContractException($"{where}: an inner step needs a 'type'");
                if (type.AsString == "repeat")
                    throw new ContractException($"{where}: repeat cannot be nested");
                child.TryGet("timeoutSec", out var timeout);
                if (!Whole(timeout, 1))
                    throw new ContractException($"{where}: an inner step needs a whole 'timeoutSec' of at least 1");
            }
        }

        /// <summary>External UI automation distorts a measurement, so a performance plan must not run an <c>external.ui</c> or <c>app.background</c> step
        /// between <c>probe.start</c> and <c>probe.stop</c>; the inner steps of a <c>repeat</c> count in the order they run.</summary>
        static void CheckExternalUi(PlanDocument plan)
        {
            if (plan.Kind != "performance")
                return;
            var measuring = false;
            for (var index = 0; index < plan.Steps.Count; index++)
            {
                var step = plan.Steps[index];
                var path = $"$.steps[{index}]";
                if (step.Type == "repeat" && step.Args.TryGet("steps", out var inner))
                {
                    // From the second round on, the inner steps start in the state the first round left: read the list twice when it repeats.
                    var passes = step.Args.TryGet("count", out var count) && count.Kind == JsonKind.Number && count.AsNumber > 1 ? 2 : 1;
                    for (var pass = 0; pass < passes; pass++)
                    {
                        var number = 0;
                        foreach (var child in inner.Items)
                            CheckWindow(child.TryGet("type", out var type) ? type.AsString : null, $"{path}.steps[{number++}]", ref measuring);
                    }
                }
                else
                {
                    CheckWindow(step.Type, path, ref measuring);
                }
            }
        }

        static void CheckWindow(string type, string path, ref bool measuring)
        {
            if (type == "probe.start")
                measuring = true;
            else if (type == "probe.stop")
                measuring = false;
            else if ((type == "external.ui" || type == "app.background") && measuring)
                throw new ContractException($"{path}: {type} is forbidden between probe.start and probe.stop in a performance plan");
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
