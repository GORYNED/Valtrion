using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
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
                case SchemaIds.Baseline: return BaselineDocument.FromJson(value);
                default: throw new ContractException($"$: unknown schema '{schema.AsString}'");
            }
        }
    }
}
