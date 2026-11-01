using System;
using System.Collections.Generic;
using System.IO;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;
using NUnit.Framework;
using UnityEditor.PackageManager;

namespace Valtrion.Unity.Tests
{
    /// <summary>Round-trips the golden fixtures shared with the Python tests: every valid fixture parses and writes back to an
    /// equal value tree, every invalid fixture is rejected.</summary>
    public sealed class ContractFixtureTests
    {
        const string NoFixtures = "no-contracts-fixtures";

        // The fixtures live in the repository checkout (contracts/fixtures), above the package folder. A consumer project
        // that embeds the package from a git URL or the registry has no checkout: the tests are then ignored, not failed.
        static string FindFixtureRoot()
        {
            var package = PackageInfo.FindForAssembly(typeof(ContractSerializer).Assembly);
            if (package == null)
                return null;
            for (var directory = new DirectoryInfo(package.resolvedPath); directory != null; directory = directory.Parent)
            {
                var candidate = Path.Combine(directory.FullName, "contracts", "fixtures");
                if (Directory.Exists(candidate))
                    return candidate;
            }
            return null;
        }

        static string FixtureRoot
        {
            get
            {
                var root = FindFixtureRoot();
                if (root == null)
                    Assert.Ignore("contracts/fixtures was not found above the package folder; these tests need a Valtrion repository checkout");
                return root;
            }
        }

        static IEnumerable<string> Files(string folder, string extension)
        {
            var root = FindFixtureRoot();
            // Test case sources run at discovery: without a checkout hand out one placeholder, the test then ignores itself.
            return root == null ? new[] { NoFixtures } : Directory.GetFiles(Path.Combine(root, folder), extension);
        }

        static string[] ValidJson() => ToNames(Files("valid", "*.json"));

        static string[] InvalidJson() => ToNames(Files("invalid", "*.json"));

        static string[] ToNames(IEnumerable<string> paths)
        {
            var names = new List<string>();
            foreach (var path in paths)
                names.Add(Path.GetFileName(path));
            names.Sort(StringComparer.Ordinal);
            return names.ToArray();
        }

        [TestCaseSource(nameof(ValidJson))]
        public void ValidFixtureRoundTrips(string name)
        {
            var original = JsonValue.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", name)));

            var document = ContractSerializer.FromJson(original);
            var written = document.ToJson();

            Assert.IsTrue(original.DeepEquals(written), $"{name} changed in the round trip:\n{written.Write(true)}");
            Assert.IsTrue(original.DeepEquals(JsonValue.Parse(written.Write(false))), "compact text is not equivalent");
            Assert.IsTrue(original.DeepEquals(JsonValue.Parse(written.Write(true))), "indented text is not equivalent");
        }

        [TestCaseSource(nameof(InvalidJson))]
        public void InvalidFixtureIsRejected(string name)
        {
            var value = JsonValue.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "invalid", name)));

            Assert.Throws<ContractException>(() => ContractSerializer.FromJson(value), name);
        }

        [Test]
        public void EverySchemaHasValidAndInvalidFixtures()
        {
            Assert.IsNotNull(FixtureRoot);
            var valid = new HashSet<string>();
            foreach (var name in ValidJson())
                valid.Add(ContractSerializer.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", name))).Schema);
            CollectionAssert.AreEquivalent(SchemaIds.All, valid);

            var invalidPrefixes = new HashSet<string>();
            foreach (var name in InvalidJson())
                invalidPrefixes.Add(name.Substring(0, name.IndexOf('-')));
            foreach (var schema in SchemaIds.All)
                Assert.IsTrue(invalidPrefixes.Contains(schema.Substring(4, schema.IndexOf('/') - 4)), schema);
        }

        [Test]
        public void EventsLogLinesAreEvents()
        {
            var lines = File.ReadAllLines(Path.Combine(FixtureRoot, "valid", "events.jsonl"));
            Assert.AreEqual(3, lines.Length);
            for (var index = 0; index < lines.Length; index++)
            {
                var evt = EventDocument.Parse(lines[index]);
                Assert.AreEqual(index, evt.Seq);
                Assert.IsTrue(JsonValue.Parse(lines[index]).DeepEquals(JsonValue.Parse(evt.ToJsonLine())));
            }
        }

        [Test]
        public void StepArgumentsAreKeptVerbatim()
        {
            var plan = PlanDocument.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", "plan.json")));
            var step = plan.Steps[2];

            Assert.AreEqual("scenario.record", step.Type);
            Assert.AreEqual(10, step.Args.Members[0].Value.AsNumber);
            Assert.IsTrue(step.Args.TryGet("options", out var options));
            Assert.AreEqual(2, options.Count);
            Assert.AreEqual(3, plan.Metrics["stopToReadyMs"].MinSamples);
        }

        [Test]
        public void APipelineWithPrelaunchFilesAndNoShaIsReadAndWrittenBack()
        {
            var text = File.ReadAllText(Path.Combine(FixtureRoot, "valid", "pipeline-prelaunch.json"));
            var pipeline = PipelineDocument.Parse(text);

            Assert.IsNull(pipeline.Candidate.Sha, "the commit of the checkout is used when the file names none");
            Assert.IsNull(pipeline.Stages[0].PrelaunchFiles);
            var files = pipeline.Stages[2].PrelaunchFiles;
            Assert.AreEqual(2, files.Count);
            Assert.AreEqual("settings/camera.json", files[0].Local);
            Assert.AreEqual("diagnostics.txt", files[1].Remote);
            Assert.IsTrue(JsonValue.Parse(text).DeepEquals(JsonValue.Parse(pipeline.ToJsonString())));
            Assert.IsFalse(pipeline.ToJsonString().Contains("\"sha\""), "an absent sha stays absent");

            var withSha = PipelineDocument.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", "pipeline.json")));
            Assert.AreEqual(40, withSha.Candidate.Sha.Length);
        }

        [Test]
        public void ExitCodesFollowTheStatus()
        {
            Assert.AreEqual(0, ValidationStatus.ExitCode(ValidationStatus.Pass));
            Assert.AreEqual(1, ValidationStatus.ExitCode(ValidationStatus.Fail));
            Assert.AreEqual(2, ValidationStatus.ExitCode(ValidationStatus.Blocked));
            Assert.AreEqual(2, ValidationStatus.ExitCode(ValidationStatus.NeedsPermission));
            Assert.AreEqual(3, ValidationStatus.ExitCode(ValidationStatus.Stall));
            Assert.AreEqual(3, ValidationStatus.ExitCode(ValidationStatus.Timeout));
            Assert.Throws<ArgumentException>(() => ValidationStatus.ExitCode("OK"));
        }

        [Test]
        public void BaselinesCompareOnlyOnEqualKeys()
        {
            var baseline = BaselineDocument.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", "baseline.json")));
            var other = BaselineDocument.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", "baseline.json")));

            Assert.IsTrue(baseline.Key.IsComparableWith(other.Key));
            other.Key.OsMajor += 1;
            Assert.IsFalse(baseline.Key.IsComparableWith(other.Key));
            other.Key.OsMajor = baseline.Key.OsMajor;
            other.Key.UnityVersion = "other";
            Assert.IsFalse(baseline.Key.IsComparableWith(other.Key));
        }

        [Test]
        public void MalformedJsonIsRejected()
        {
            foreach (var text in new[] { "", "{", "{\"a\":1,}", "[1,]", "{\"a\":1,\"a\":2}", "01", "\"x", "{} {}", "tru", "1e999", "-1e999", "NaN", "\"\\u41\0\0\"", "\"\\u00g1\"", "\"\\u+041\"" })
                Assert.Throws<JsonException>(() => JsonValue.Parse(text), text);
        }

        [Test]
        public void JsonRoundTripsEscapesAndNumbers()
        {
            const string text = "{\"s\":\"a\\\"b\\\\c\\n\\u0001\\u00e9\",\"n\":[0,-1,2.5,1e3,12345678901],\"b\":[true,false,null],\"o\":{}}";
            var value = JsonValue.Parse(text);

            Assert.IsTrue(value.DeepEquals(JsonValue.Parse(value.Write(false))));
            Assert.AreEqual("a\"b\\c\n\u0001é", value.TryGet("s", out var s) ? s.AsString : null);
            Assert.AreEqual(1000, value.TryGet("n", out var n) ? n.Items[3].AsNumber : 0);
        }

        [Test]
        public void LimitsMatchThePythonValidator()
        {
            var plan = File.ReadAllText(Path.Combine(FixtureRoot, "valid", "plan.json"));
            var hosts = File.ReadAllText(Path.Combine(FixtureRoot, "valid", "hosts.json"));

            Assert.Throws<ContractException>(() => PlanDocument.Parse(plan.Replace("\"budgetSeconds\": 900", "\"budgetSeconds\": 4294967296")));
            Assert.Throws<ContractException>(() => PlanDocument.Parse(plan.Replace("\"timeoutSec\": 180", "\"timeoutSec\": 4294967297")));
            Assert.Throws<ContractException>(() => HostsDocument.Parse(hosts.Replace("\"builder@mac-build.example\"", "\"a\\nb\"")));
            Assert.Throws<ContractException>(() => HostsDocument.Parse(hosts.Replace("\"builder@mac-build.example\"", "\"-oProxyCommand=x\"")));

            var deep = new string('[', 65) + new string(']', 65);
            Assert.Throws<JsonException>(() => JsonValue.Parse(deep));
            Assert.DoesNotThrow(() => JsonValue.Parse(new string('[', 64) + new string(']', 64)));
        }

        [Test]
        public void EventLinesNeverContainLineSeparators()
        {
            var evt = EventDocument.Parse(File.ReadAllText(Path.Combine(FixtureRoot, "valid", "event.json")));
            evt.Message = "a\u2028b\u0085c\u2029d\ne";

            var line = evt.ToJsonLine();

            Assert.AreEqual(-1, line.IndexOfAny(new[] { '\n', '\r', '\u2028', '\u2029', '\u0085' }));
            Assert.AreEqual(evt.Message, EventDocument.Parse(line).Message);
        }

        [Test]
        public void DocumentsAreRejectedWhenTheSchemaIsMissingOrUnknown()
        {
            Assert.Throws<ContractException>(() => ContractSerializer.Parse("{}"));
            Assert.Throws<ContractException>(() => ContractSerializer.Parse("{\"schema\":\"gdv.nothing/1\"}"));
            Assert.Throws<ContractException>(() => ContractSerializer.Parse("[]"));
        }
    }
}
