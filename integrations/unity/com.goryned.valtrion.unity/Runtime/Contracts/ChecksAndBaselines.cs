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

        /// <summary>Optional globs of paths that do not belong to this area although <see cref="Glob"/> matches them.</summary>
        public List<string> Exclude { get; set; }

        public string Kind { get; set; }

        /// <summary>true: for a path this area matches, the built-in preset's areas (<c>extends</c>) are ignored instead of adding their kinds.</summary>
        public bool? OverridesPreset { get; set; }

        internal static ChecksArea Read(ContractReader reader)
        {
            var area = new ChecksArea { Glob = reader.Str("glob"), Exclude = reader.OptStrings("exclude"), Kind = reader.Identifier("kind"), OverridesPreset = reader.OptBool("overridesPreset") };
            reader.Done();
            return area;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("glob", JsonValue.From(Glob));
            if (Exclude != null) node.Set("exclude", JsonBuild.Strings(Exclude));
            node.Set("kind", JsonValue.From(Kind));
            node.SetIf("overridesPreset", OverridesPreset);
            return node;
        }
    }

    /// <summary>For child keys whose name matches <see cref="Match"/> (a glob), a changed key counts as <see cref="Kind"/>.</summary>
    public sealed class ChecksJsonKey
    {
        public string Match { get; set; }

        /// <summary>One kind (the short form); null when <see cref="Kinds"/> is used.</summary>
        public string Kind { get; set; }

        /// <summary>A list of kinds; null when <see cref="Kind"/> is used.</summary>
        public List<string> Kinds { get; set; }

        /// <summary>true on a fallback key (<c>*</c>) whose kinds hold player or device checks: <c>check-config</c> then does not warn that every unknown entry selects hardware.</summary>
        public bool? AllowHeavyFallback { get; set; }

        internal static ChecksJsonKey Read(ContractReader reader)
        {
            var key = new ChecksJsonKey { Match = reader.Str("match"), Kind = reader.OptIdentifier("kind"), Kinds = reader.OptStrings("kinds"), AllowHeavyFallback = reader.OptBool("allowHeavyFallback") };
            if (key.Kind == null && key.Kinds == null)
                throw new ContractException($"{reader.Path}: give kind or kinds");
            if (key.Kinds != null && key.Kinds.Count == 0)
                throw new ContractException($"{reader.Path}.kinds: expected at least one kind");
            reader.Done();
            return key;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("match", JsonValue.From(Match));
            if (Kind != null) node.Set("kind", JsonValue.From(Kind));
            if (Kinds != null) node.Set("kinds", JsonBuild.Strings(Kinds));
            node.SetIf("allowHeavyFallback", AllowHeavyFallback);
            return node;
        }
    }

    /// <summary>For a changed JSON file matching <see cref="Glob"/>, the kinds of the child keys of <see cref="Root"/> that changed.</summary>
    public sealed class ChecksJsonRule
    {
        public string Glob { get; set; }

        public string Root { get; set; }

        public List<ChecksJsonKey> Keys { get; set; } = new List<ChecksJsonKey>();

        internal static ChecksJsonRule Read(ContractReader reader)
        {
            var rule = new ChecksJsonRule { Glob = reader.Str("glob"), Root = reader.Str("root"), Keys = reader.Items("keys", ChecksJsonKey.Read, 1) };
            reader.Done();
            return rule;
        }

        internal JsonValue Write() =>
            JsonValue.NewObject().Set("glob", JsonValue.From(Glob)).Set("root", JsonValue.From(Root)).Set("keys", JsonBuild.Array(Keys, key => key.Write()));
    }

    /// <summary>The markers of a block comment that starts and ends on one line.</summary>
    public sealed class ChecksBlockComment
    {
        public string Start { get; set; }

        public string End { get; set; }

        internal static ChecksBlockComment Read(ContractReader reader)
        {
            var block = new ChecksBlockComment { Start = reader.Str("start"), End = reader.Str("end") };
            reader.Done();
            return block;
        }

        internal JsonValue Write() => JsonValue.NewObject().Set("start", JsonValue.From(Start)).Set("end", JsonValue.From(End));
    }

    /// <summary>For paths that match <see cref="Glob"/>, a change that only touches comments, blank lines and layout counts as <see cref="Kind"/>.</summary>
    public sealed class ChecksContentRule
    {
        public string Glob { get; set; }

        public string Kind { get; set; }

        public List<string> LineComment { get; set; } = new List<string>();

        public ChecksBlockComment BlockComment { get; set; }

        /// <summary>When true, re-indentation of code lines also counts as cosmetic (default false, which is the safe choice for languages where indentation is code).</summary>
        public bool? Reindent { get; set; }

        /// <summary>Line prefixes (for example <c>#!</c> or <c>#if</c>) that are code even though a comment marker starts them.</summary>
        public List<string> CodePrefixes { get; set; }

        internal static ChecksContentRule Read(ContractReader reader)
        {
            var rule = new ChecksContentRule
            {
                Glob = reader.Str("glob"),
                Kind = reader.Identifier("kind"),
                LineComment = reader.Strings("lineComment"),
                BlockComment = reader.OptObject("blockComment") is ContractReader block ? ChecksBlockComment.Read(block) : null,
                Reindent = reader.OptBool("reindent"),
                CodePrefixes = reader.OptStrings("codePrefixes"),
            };
            if (rule.LineComment.Count == 0)
                throw new ContractException($"{reader.Path}.lineComment: expected at least one marker");
            reader.Done();
            return rule;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("glob", JsonValue.From(Glob)).Set("kind", JsonValue.From(Kind)).Set("lineComment", JsonBuild.Strings(LineComment));
            if (BlockComment != null) node.Set("blockComment", BlockComment.Write());
            node.SetIf("reindent", Reindent);
            if (CodePrefixes != null) node.Set("codePrefixes", JsonBuild.Strings(CodePrefixes));
            return node;
        }
    }

    /// <summary>For paths that match <see cref="Glob"/>, a project command that proves a change keeps every value: exit code 0 gives the path <see cref="Kind"/>.</summary>
    public sealed class ChecksEquivalenceRule
    {
        public string Glob { get; set; }

        public string Kind { get; set; }

        /// <summary>The program and its arguments; the base commit and the changed path are appended when it runs.</summary>
        public List<string> Command { get; set; } = new List<string>();

        /// <summary>Seconds the command may run (1 to 3600, default 60 in Core).</summary>
        public int? TimeoutSec { get; set; }

        internal static ChecksEquivalenceRule Read(ContractReader reader)
        {
            var rule = new ChecksEquivalenceRule
            {
                Glob = reader.Str("glob"),
                Kind = reader.Identifier("kind"),
                Command = reader.Strings("command"),
                TimeoutSec = reader.OptInt32("timeoutSec", 1),
            };
            if (rule.Command.Count == 0)
                throw new ContractException($"{reader.Path}.command: expected at least one item");
            if (rule.TimeoutSec > 3600)
                throw new ContractException($"{reader.Path}.timeoutSec: {rule.TimeoutSec} is above 3600");
            reader.Done();
            return rule;
        }

        internal JsonValue Write()
        {
            var node = JsonValue.NewObject().Set("glob", JsonValue.From(Glob)).Set("kind", JsonValue.From(Kind)).Set("command", JsonBuild.Strings(Command));
            node.SetIf("timeoutSec", TimeoutSec);
            return node;
        }
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

        /// <summary>Optional named runner command templates (name to command); an Editor suite entry <c>editmode:A@name</c> uses one.</summary>
        public Dictionary<string, string> Runners { get; set; }

        /// <summary>Optional built-in preset (<c>valtrion:unity/1</c>) whose path rules and classes are merged under this map; versioned, so a Core upgrade never changes a selection.</summary>
        public string Extends { get; set; }

        /// <summary>With <see cref="Extends"/>: the project's concrete checks for each slot of the preset (<c>editor</c>, <c>player</c>, <c>device</c>); an empty list says there are none.</summary>
        public Dictionary<string, List<string>> Bindings { get; set; }

        /// <summary>Null only when the map has <see cref="Extends"/> and no areas of its own.</summary>
        public List<ChecksArea> Areas { get; set; } = new List<ChecksArea>();

        /// <summary>Optional rules that map a comment-only or layout-only change of matching paths to a cheaper kind.</summary>
        public List<ChecksContentRule> ContentRules { get; set; }

        /// <summary>Optional rules that classify a changed JSON file by the keys that changed.</summary>
        public List<ChecksJsonRule> JsonRules { get; set; }

        /// <summary>Optional rules whose project command can prove a change value-equivalent, which gives the path a cheaper kind.</summary>
        public List<ChecksEquivalenceRule> EquivalenceRules { get; set; }

        /// <summary>Null only when the map has <see cref="Extends"/> and no kinds of its own.</summary>
        public Dictionary<string, ChecksLevels> Kinds { get; set; } = new Dictionary<string, ChecksLevels>();

        public string Unclassified { get; set; }

        public static ChecksDocument Parse(string json) => FromJson(JsonValue.Parse(json));

        public static ChecksDocument FromJson(JsonValue value)
        {
            var reader = new ContractReader(value, "$");
            reader.Const("schema", SchemaIds.Checks);
            var runners = reader.StringMap("runners");
            if (runners != null)
                foreach (var runner in runners)
                    if (runner.Value.Length == 0)
                        throw new ContractException($"{reader.Path}.runners.{runner.Key}: expected a non-empty string");
            var extends = reader.OptStr("extends", 1);
            if (extends != null && !System.Text.RegularExpressions.Regex.IsMatch(extends, "^valtrion:[a-z0-9-]+/[0-9]+$"))
                throw new ContractException($"{reader.Path}.extends: expected a preset id such as valtrion:unity/1");
            var checks = new ChecksDocument
            {
                Platforms = ChecksPlatforms.Read(reader.Object("platforms")),
                UnityEditorSlots = reader.OptInt32("unityEditorSlots", 1),
                Runners = runners,
                Extends = extends,
                Bindings = reader.StringListMap("bindings"),
                // A map that extends a preset may leave out areas and kinds; a map without `extends` needs both.
                Areas = extends != null ? reader.OptItems("areas", ChecksArea.Read) : reader.Items("areas", ChecksArea.Read),
                ContentRules = reader.OptItems("contentRules", ChecksContentRule.Read),
                JsonRules = reader.OptItems("jsonRules", ChecksJsonRule.Read),
                EquivalenceRules = reader.OptItems("equivalenceRules", ChecksEquivalenceRule.Read),
                Kinds = extends != null ? reader.OptMap("kinds", ChecksLevels.Read) : reader.Map("kinds", ChecksLevels.Read),
                Unclassified = reader.Choice("unclassified", Enums.Unclassified),
            };
            reader.Done();
            return checks;
        }

        public JsonValue ToJson()
        {
            var node = JsonValue.NewObject()
                .Set("schema", JsonValue.From(Schema)).Set("platforms", Platforms.Write()).SetIf("unityEditorSlots", UnityEditorSlots);
            if (Runners != null) node.Set("runners", JsonBuild.StringMap(Runners));
            if (Extends != null) node.Set("extends", JsonValue.From(Extends));
            if (Bindings != null) node.Set("bindings", JsonBuild.Map(Bindings, list => JsonBuild.Strings(list)));
            if (Areas != null) node.Set("areas", JsonBuild.Array(Areas, area => area.Write()));
            if (JsonRules != null) node.Set("jsonRules", JsonBuild.Array(JsonRules, rule => rule.Write()));
            if (ContentRules != null) node.Set("contentRules", JsonBuild.Array(ContentRules, rule => rule.Write()));
            if (EquivalenceRules != null) node.Set("equivalenceRules", JsonBuild.Array(EquivalenceRules, rule => rule.Write()));
            if (Kinds != null) node.Set("kinds", JsonBuild.Map(Kinds, levels => levels.Write()));
            return node.Set("unclassified", JsonValue.From(Unclassified));
        }

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
