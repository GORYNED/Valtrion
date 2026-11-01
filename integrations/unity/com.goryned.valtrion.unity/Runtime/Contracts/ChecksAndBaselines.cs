using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    public sealed class ChecksPlatforms
    {
        public List<string> Required { get; set; } = new List<string>();

        public List<string> Dev { get; set; }

        internal static ChecksPlatforms Read(ContractReader reader)
        {
            var platforms = new ChecksPlatforms
            {
                Required = reader.Choices("required", Enums.Platforms),
                Dev = reader.OptChoices("dev", Enums.Platforms),
            };
            reader.Done();
            return platforms;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("required", JsonBuild.Strings(Required));
            if (Dev != null) node.Set("dev", JsonBuild.Strings(Dev));
            return node;
        }
    }

    public sealed class ChecksArea
    {
        public string Glob { get; set; }

        public string Kind { get; set; }

        internal static ChecksArea Read(ContractReader reader)
        {
            var area = new ChecksArea { Glob = reader.Str("glob"), Kind = reader.Identifier("kind") };
            reader.Done();
            return area;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("glob", JsonValue.From(Glob)).Set("kind", JsonValue.From(Kind));
    }

    /// <summary>The checks a kind of change needs at each validation level (L0 to L4); a level that is absent has no checks of its own.</summary>
    public sealed class ChecksLevels
    {
        public List<string> L0 { get; set; }

        public List<string> L1 { get; set; }

        public List<string> L2 { get; set; }

        public List<string> L3 { get; set; }

        public List<string> L4 { get; set; }

        internal static ChecksLevels Read(ContractReader reader)
        {
            var levels = new ChecksLevels
            {
                L0 = reader.OptStrings("L0"),
                L1 = reader.OptStrings("L1"),
                L2 = reader.OptStrings("L2"),
                L3 = reader.OptStrings("L3"),
                L4 = reader.OptStrings("L4"),
            };
            reader.Done();
            return levels;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject();
            if (L0 != null) node.Set("L0", JsonBuild.Strings(L0));
            if (L1 != null) node.Set("L1", JsonBuild.Strings(L1));
            if (L2 != null) node.Set("L2", JsonBuild.Strings(L2));
            if (L3 != null) node.Set("L3", JsonBuild.Strings(L3));
            if (L4 != null) node.Set("L4", JsonBuild.Strings(L4));
            return node;
        }
    }

    /// <summary>A project's check map (<c>gdv.checks/1</c>): changed paths to kinds, kinds to checks. An unclassified path selects the widest set.</summary>
    public sealed class ChecksDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Checks;

        public ChecksPlatforms Platforms { get; set; }

        public int? UnityEditorSlots { get; set; }

        public List<ChecksArea> Areas { get; set; } = new List<ChecksArea>();

        public Dictionary<string, ChecksLevels> Kinds { get; set; } = new Dictionary<string, ChecksLevels>();

        public string Unclassified { get; set; }

        public static ChecksDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static ChecksDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Checks);
            var checks = new ChecksDocument
            {
                Platforms = ChecksPlatforms.Read(reader.Object("platforms")),
                UnityEditorSlots = reader.OptInt32("unityEditorSlots", 1),
                Areas = reader.Items("areas", ChecksArea.Read),
                Kinds = reader.Map("kinds", ChecksLevels.Read),
                Unclassified = reader.Choice("unclassified", Enums.Unclassified),
            };
            reader.Done();
            return checks;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("platforms", Platforms.Write()).SetIf("unityEditorSlots", UnityEditorSlots)
            .Set("areas", JsonBuild.Array(Areas, area => area.Write())).Set("kinds", JsonBuild.Map(Kinds, levels => levels.Write()))
            .Set("unclassified", JsonValue.From(Unclassified));

        public string ToJsonString() => ToJson().Write(true);
    }

    /// <summary>What two results must share to be compared: plan hash, device class, OS major, build profile and Unity version.
    /// Different keys give NOT_COMPARABLE, never PASS.</summary>
    public sealed class ComparabilityKey
    {
        public string PlanSha256 { get; set; }

        public string DeviceClass { get; set; }

        public int OsMajor { get; set; }

        public string BuildProfile { get; set; }

        public string UnityVersion { get; set; }

        public bool IsComparableWith(ComparabilityKey other) =>
            other != null && PlanSha256 == other.PlanSha256 && DeviceClass == other.DeviceClass && OsMajor == other.OsMajor
            && BuildProfile == other.BuildProfile && UnityVersion == other.UnityVersion;

        internal static ComparabilityKey Read(ContractReader reader)
        {
            var key = new ComparabilityKey
            {
                PlanSha256 = reader.Hex("planSha256", 64),
                DeviceClass = reader.Identifier("deviceClass"),
                OsMajor = reader.Int32("osMajor", 0),
                BuildProfile = reader.Identifier("buildProfile"),
                UnityVersion = reader.Str("unityVersion"),
            };
            reader.Done();
            return key;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("planSha256", JsonValue.From(PlanSha256)).Set("deviceClass", JsonValue.From(DeviceClass)).Set("osMajor", JsonValue.From(OsMajor))
            .Set("buildProfile", JsonValue.From(BuildProfile)).Set("unityVersion", JsonValue.From(UnityVersion));
    }

    public sealed class BaselineSource
    {
        public string RunId { get; set; }

        public string Repo { get; set; }

        public string Sha { get; set; }

        public string Evidence { get; set; }

        internal static BaselineSource Read(ContractReader reader)
        {
            var source = new BaselineSource
            {
                RunId = reader.Identifier("runId"),
                Repo = reader.Str("repo"),
                Sha = reader.Hex("sha", 40),
                Evidence = reader.Choice("evidence", EvidenceKind.Baseline),
            };
            reader.Done();
            return source;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("runId", JsonValue.From(RunId)).Set("repo", JsonValue.From(Repo)).Set("sha", JsonValue.From(Sha)).Set("evidence", JsonValue.From(Evidence));
    }

    public sealed class BaselineMetric
    {
        public string Unit { get; set; }

        public string Direction { get; set; }

        public MetricStats Stats { get; set; }

        internal static BaselineMetric Read(ContractReader reader)
        {
            var metric = new BaselineMetric
            {
                Unit = reader.Str("unit"),
                Direction = reader.Choice("direction", Enums.Directions),
                Stats = MetricStats.Read(reader.Object("stats")),
            };
            reader.Done();
            return metric;
        }

        internal JsonValue Write() => JsonValue.NewObject()
            .Set("unit", JsonValue.From(Unit)).Set("direction", JsonValue.From(Direction)).Set("stats", Stats.Write());
    }

    /// <summary>A baseline summary (<c>gdv.baseline/1</c>) kept in the project's git; raw samples stay outside git. Editor results cannot be a baseline.</summary>
    public sealed class BaselineDocument : IGdvDocument
    {
        public string Schema => SchemaIds.Baseline;

        public string PlanId { get; set; }

        public ComparabilityKey Key { get; set; }

        public string CreatedUtc { get; set; }

        public BaselineSource Source { get; set; }

        public Dictionary<string, BaselineMetric> Metrics { get; set; } = new Dictionary<string, BaselineMetric>();

        public static BaselineDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static BaselineDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Baseline);
            var baseline = new BaselineDocument
            {
                PlanId = reader.Identifier("planId"),
                Key = ComparabilityKey.Read(reader.Object("key")),
                CreatedUtc = reader.Utc("createdUtc"),
                Source = BaselineSource.Read(reader.Object("source")),
                Metrics = reader.Map("metrics", BaselineMetric.Read),
            };
            reader.Done();
            return baseline;
        }

        public JsonValue ToJson() => JsonValue.NewObject()
            .Set("schema", JsonValue.From(Schema)).Set("planId", JsonValue.From(PlanId)).Set("key", Key.Write())
            .Set("createdUtc", JsonValue.From(CreatedUtc)).Set("source", Source.Write())
            .Set("metrics", JsonBuild.Map(Metrics, metric => metric.Write()));

        public string ToJsonString() => ToJson().Write(true);
    }

    /// <summary>Reads any gdv document by its <c>schema</c> property.</summary>
    public static class ContractSerializer
    {
        public static IGdvDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static IGdvDocument FromJson(JsonValue value)
        {
            if (value == null || value.Kind != JsonKind.Object || !value.TryGet("schema", out var schema) || schema.Kind != JsonKind.String)
                throw new ContractException("$: missing string property 'schema'");
            switch (schema.AsString)
            {
                case SchemaIds.Plan: return PlanDocument.FromJson(value);
                case SchemaIds.Pipeline: return PipelineDocument.FromJson(value);
                case SchemaIds.Result: return ResultDocument.FromJson(value);
                case SchemaIds.Heartbeat: return HeartbeatDocument.FromJson(value);
                case SchemaIds.Job: return JobDocument.FromJson(value);
                case SchemaIds.Event: return EventDocument.FromJson(value);
                case SchemaIds.Hosts: return HostsDocument.FromJson(value);
                case SchemaIds.Registry: return RegistryDocument.FromJson(value);
                case SchemaIds.Lease: return LeaseDocument.FromJson(value);
                case SchemaIds.Checks: return ChecksDocument.FromJson(value);
                case SchemaIds.Baseline: return BaselineDocument.FromJson(value);
                default: throw new ContractException($"$: unknown schema '{schema.AsString}'");
            }
        }
    }
}
