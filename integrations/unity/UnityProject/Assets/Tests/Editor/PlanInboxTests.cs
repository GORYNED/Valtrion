using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;
using UnityEditor.PackageManager;

namespace Valtrion.Unity.Tests
{
    public sealed class PlanInboxTests
    {
        sealed class FakeEnvironment : IPlanEnvironment
        {
            public string[] CommandLineArgs { get; set; } = new string[0];

            public string PersistentDataPath { get; set; }

            public DateTime UtcNow => new DateTime(2026, 10, 29, 15, 30, 0, DateTimeKind.Utc);

            public string Intent { get; set; }

            public System.Collections.Generic.Dictionary<string, string> Extras { get; } = new System.Collections.Generic.Dictionary<string, string>();

            public System.Collections.Generic.Dictionary<string, string> Variables { get; } = new System.Collections.Generic.Dictionary<string, string>();

            public string GetIntentExtra(string name) => name == PlanInbox.PlanExtra ? Intent : Extras.TryGetValue(name, out var value) ? value : null;

            public string GetEnvironmentVariable(string name) => Variables.TryGetValue(name, out var value) ? value : null;
        }

        sealed class AutorecConverter : ILegacyPlanConverter
        {
            public IReadOnlyList<string> FileNames { get; } = new[] { "autorec.json" };

            public bool CanConvert(string fileName, string text) => text.Contains("autorec");

            public PlanDocument Convert(string fileName, string text) => HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1}");
        }

        string _root;
        FakeEnvironment _environment;

        [SetUp]
        public void SetUp()
        {
            _root = Path.Combine(Path.GetTempPath(), "gdv-inbox-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(Path.Combine(_root, "gdv", "inbox"));
            _environment = new FakeEnvironment { PersistentDataPath = _root };
        }

        [TearDown]
        public void TearDown() => Directory.Delete(_root, true);

        string PlanText(string id) => HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1}").ToJsonString().Replace("test.plan", id);

        string Write(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, text);
            return path;
        }

        [Test]
        public void NothingFoundMeansNoPlan()
        {
            Assert.IsNull(new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find());
        }

        [Test]
        public void InboxPlanIsFoundThenRenamedAndNotReadTwice()
        {
            var path = Write(Path.Combine(_root, "gdv", "inbox", "plan.json"), PlanText("from.inbox"));
            var inbox = new PlanInbox(_environment, new ILegacyPlanConverter[0]);

            var source = inbox.Find();
            Assert.AreEqual("inbox", source.Origin);
            Assert.AreEqual("from.inbox", source.Plan.Id);
            Assert.IsTrue(inbox.MarkConsumed(source));

            Assert.IsFalse(File.Exists(path));
            Assert.IsTrue(File.Exists(path + ".done-20261029T153000Z"));
            Assert.IsNull(inbox.Find());
        }

        [Test]
        public void LaunchArgumentBeatsIntentBeatsInbox()
        {
            Write(Path.Combine(_root, "gdv", "inbox", "plan.json"), PlanText("from.inbox"));
            var intentPlan = Write(Path.Combine(_root, "intent.json"), PlanText("from.intent"));
            var argumentPlan = Write(Path.Combine(_root, "argument.json"), PlanText("from.argument"));
            _environment.Intent = intentPlan;
            _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, argumentPlan, PlanInbox.RunIdArgument, "run-7" };
            var inbox = new PlanInbox(_environment, new ILegacyPlanConverter[0]);

            var first = inbox.Find();
            Assert.AreEqual("from.argument", first.Plan.Id);
            Assert.AreEqual("argument", first.Origin);
            Assert.AreEqual("run-7", first.RunId);
            inbox.MarkConsumed(first);
            _environment.CommandLineArgs = new string[0];

            var second = inbox.Find();
            Assert.AreEqual("intent", second.Origin);
            inbox.MarkConsumed(second);

            Assert.AreEqual("inbox", inbox.Find().Origin);
        }

        [Test]
        public void EnvironmentVariablesSitBetweenArgumentsAndIntentExtras()
        {
            var inboxPlan = Write(Path.Combine(_root, "gdv", "inbox", "plan.json"), PlanText("from.inbox"));
            var intentPlan = Write(Path.Combine(_root, "intent.json"), PlanText("from.intent"));
            var variablePlan = Write(Path.Combine(_root, "variable.json"), PlanText("from.variable"));
            var argumentPlan = Write(Path.Combine(_root, "argument.json"), PlanText("from.argument"));
            _environment.Intent = intentPlan;
            _environment.Extras[PlanInbox.RunIdExtra] = "intent-run";
            _environment.Extras[PlanInbox.OutExtra] = "intent-out";
            _environment.Variables[PlanInbox.PlanVariable] = variablePlan;
            _environment.Variables[PlanInbox.RunIdVariable] = "variable-run";
            _environment.Variables[PlanInbox.OutVariable] = "variable-out";
            var inbox = new PlanInbox(_environment, new ILegacyPlanConverter[0]);

            var fromVariable = inbox.Find();
            Assert.AreEqual("environment", fromVariable.Origin);
            Assert.AreEqual("from.variable", fromVariable.Plan.Id);
            Assert.AreEqual("variable-run", fromVariable.RunId);
            Assert.AreEqual("variable-out", fromVariable.OutRoot);

            _environment.CommandLineArgs = new[] { "app", PlanInbox.PlanArgument, argumentPlan, PlanInbox.RunIdArgument, "argument-run", PlanInbox.OutArgument, "argument-out" };
            var fromArgument = inbox.Find();
            Assert.AreEqual("from.argument", fromArgument.Plan.Id);
            Assert.AreEqual("argument-run", fromArgument.RunId);
            Assert.AreEqual("argument-out", fromArgument.OutRoot);

            _environment.CommandLineArgs = new string[0];
            _environment.Variables.Clear();
            var fromIntent = inbox.Find();
            Assert.AreEqual("intent", fromIntent.Origin);
            Assert.AreEqual("intent-run", fromIntent.RunId);
            Assert.AreEqual("intent-out", fromIntent.OutRoot);

            _environment.Intent = null;
            var fromInbox = inbox.Find();
            Assert.AreEqual(inboxPlan, fromInbox.Path);
            Assert.AreEqual("intent-out", fromInbox.OutRoot, "the output folder applies to every plan source");
        }

        [Test]
        public void OnlyTheTestFrameworkSceneCountsAsATestRun()
        {
            Assert.IsTrue(ValidationHarness.IsTestScene("InitTestScene638000000000000000"));
            Assert.IsFalse(ValidationHarness.IsTestScene("Main"));
            Assert.IsFalse(ValidationHarness.IsTestScene(null));
        }

        [Test]
        public void ConsumedEnvironmentPlanIsIgnoredOnARelaunch()
        {
            var variablePlan = Write(Path.Combine(_root, "variable.json"), PlanText("from.variable"));
            _environment.Variables[PlanInbox.PlanVariable] = variablePlan;
            var inbox = new PlanInbox(_environment, new ILegacyPlanConverter[0]);

            inbox.MarkConsumed(inbox.Find());

            Assert.IsNull(inbox.Find(), "the inherited variable names a consumed plan: nothing to run");
        }

        [Test]
        public void RunTestsOnTheCommandLineMarksATestRun()
        {
            Assert.IsTrue(ValidationHarness.CommandLineRunsTests(new[] { "Unity.exe", "-batchmode", "-runTests", "-testPlatform", "PlayMode" }));
            Assert.IsFalse(ValidationHarness.CommandLineRunsTests(new[] { "Unity.exe", "-batchmode", "-executeMethod", "X" }));
            Assert.IsFalse(ValidationHarness.CommandLineRunsTests(null));
        }

        [Test]
        public void MissingEnvironmentPlanIsAnErrorToo()
        {
            _environment.Variables[PlanInbox.PlanVariable] = Path.Combine(_root, "missing.json");

            var source = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();

            StringAssert.Contains("GDV_PLAN", source.Error);
        }

        [Test]
        public void MissingArgumentPlanIsAnErrorNotAFallbackToTheInbox()
        {
            Write(Path.Combine(_root, "gdv", "inbox", "plan.json"), PlanText("from.inbox"));
            _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, Path.Combine(_root, "missing.json") };

            var source = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();

            Assert.IsNull(source.Plan);
            StringAssert.Contains("does not exist", source.Error);
            StringAssert.DoesNotContain("consumed", source.Error);
        }

        [Test]
        public void ArgumentPlanConsumedByAnEarlierLaunchIsNamedAsConsumed()
        {
            // #80: a Mono player run renamed the plan, and an IL2CPP player started later with the same -gdv-plan path reported only "does not exist".
            var path = Write(Path.Combine(_root, "validation", "smoke.plan.json"), PlanText("from.argument"));
            _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, path };
            var inbox = new PlanInbox(_environment, new ILegacyPlanConverter[0]);
            Assert.IsTrue(inbox.MarkConsumed(inbox.Find()));

            var relaunch = inbox.Find();

            Assert.AreEqual("argument", relaunch.Origin);
            Assert.IsNull(relaunch.Plan, "an explicit plan that was consumed is still an error, never a silent no-op");
            StringAssert.Contains("does not exist", relaunch.Error);
            StringAssert.Contains("already consumed", relaunch.Error);
            StringAssert.Contains("smoke.plan.json.done-20261029T153000Z", relaunch.Error);
        }

        [Test]
        public void ConsumedHintAlsoForABareRelativeFileName()
        {
            var directory = Path.Combine(_root, "cwd");
            Write(Path.Combine(directory, "smoke.plan.json.done-20261029T153000Z"), PlanText("old"));
            var previous = Directory.GetCurrentDirectory();
            try
            {
                Directory.SetCurrentDirectory(directory);
                _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, "smoke.plan.json" };

                var source = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();

                StringAssert.Contains("already consumed", source.Error);
                StringAssert.Contains("smoke.plan.json.done-20261029T153000Z", source.Error);
            }
            finally
            {
                Directory.SetCurrentDirectory(previous);
            }
        }

        [Test]
        public void ConsumedCopiesOfOtherPlansGiveNoHint()
        {
            var directory = Path.Combine(_root, "validation");
            Write(Path.Combine(directory, "other.plan.json.done-20261029T153000Z"), PlanText("other"));
            Write(Path.Combine(directory, "xsmoke.plan.json.done-20261029T153000Z"), PlanText("other"));
            _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, Path.Combine(directory, "smoke.plan.json") };

            var source = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();

            StringAssert.Contains("does not exist", source.Error);
            StringAssert.DoesNotContain("consumed", source.Error);
        }

        [Test]
        public void WildcardCharactersInThePlanNameAreNotASearchPattern()
        {
            if (UnityEngine.Application.platform == UnityEngine.RuntimePlatform.WindowsEditor)
                Assert.Ignore("'*' and '?' are not valid in Windows file names");
            var directory = Path.Combine(_root, "validation");
            // Matched as a pattern, "s*.json.done-*" would also find this unrelated plan's consumed copy.
            Write(Path.Combine(directory, "smoke.json.done-20261029T153000Z"), PlanText("other"));
            _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, Path.Combine(directory, "s*.json") };

            var missing = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();
            StringAssert.DoesNotContain("consumed", missing.Error);

            Write(Path.Combine(directory, "s*.json.done-20261029T153000Z"), PlanText("old"));
            var consumed = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();
            StringAssert.Contains("s*.json.done-20261029T153000Z", consumed.Error);
        }

        [TestCase("plan with spaces.json")]
        [TestCase("План-Ωβ.json")]
        public void ArgumentPlanIsReadFromPathsWithSpacesAndNonAsciiCharacters(string fileName)
        {
            var path = Write(Path.Combine(_root, "folder with spaces", fileName), PlanText("from.argument"));
            foreach (var form in new[] { path, path.Replace('\\', '/') })
            {
                _environment.CommandLineArgs = new[] { "app.exe", PlanInbox.PlanArgument, form };

                var source = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();

                Assert.IsNull(source.Error, form);
                Assert.AreEqual("from.argument", source.Plan.Id, form);
            }
        }

        [Test]
        public void TheHarnessAssemblyIsAlwaysLinked()
        {
            // #80: a link.xml inside a package is not read by UnityLinker, so this attribute is what keeps the unreferenced harness in IL2CPP players.
            var assembly = typeof(ValidationHarness).Assembly;
            Assert.IsTrue(Attribute.IsDefined(assembly, typeof(UnityEngine.Scripting.AlwaysLinkAssemblyAttribute)), assembly.FullName);
        }

        [Test]
        public void RejectedPlanIsReportedWithItsReason()
        {
            Write(Path.Combine(_root, "gdv", "inbox", "plan.json"), "{\"schema\":\"gdv.plan/1\"}");

            var source = new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find();

            Assert.IsNull(source.Plan);
            StringAssert.Contains("was rejected", source.Error);
        }

        [Test]
        public void LegacyFileIsConvertedOnlyThroughARegisteredConverter()
        {
            Write(Path.Combine(_root, "gdv", "inbox", "autorec.json"), "{\"autorec\":true}");

            Assert.IsNull(new PlanInbox(_environment, new ILegacyPlanConverter[0]).Find(), "without a converter the file is ignored");

            var source = new PlanInbox(_environment, new ILegacyPlanConverter[] { new AutorecConverter() }).Find();
            Assert.AreEqual("legacy:autorec.json", source.Origin);
            Assert.AreEqual("test.plan", source.Plan.Id);
        }

        sealed class OneStep
        {
            public static int Runs;

            [ValidationStep("test.attribute")]
            public static IEnumerator Handler(StepContext context)
            {
                Runs++;
                yield break;
            }

            [ValidationStep("test.badsignature")]
            public static void Wrong()
            {
            }
        }

        [Test]
        public void AttributeDiscoveryRegistersSteps_AndReportsProblemsInsteadOfThrowing()
        {
            var registry = new StepRegistry();

            var found = registry.Discover(typeof(OneStep).Assembly);

            Assert.GreaterOrEqual(found, 1);
            Assert.IsTrue(registry.TryGet("test.attribute", out _));
            Assert.IsFalse(registry.TryGet("test.badsignature", out _));
            Assert.IsTrue(registry.DiscoveryProblems.Any(problem => problem.Contains("test.badsignature")));
            Assert.Throws<InvalidOperationException>(() => registry.Register("test.attribute", OneStep.Handler));
        }

        [Test]
        public void BuiltInStepsAreRegisteredWithoutDiscovery()
        {
            var registry = StepRegistry.CreateDefault(false);

            foreach (var type in new[] { "wait", "waitUntil", "mark", "probe.start", "probe.stop", "gc", "idle", "screenshot", "log.expect", "file.put", "app.quit", "external.ui", "session.offsets" })
                Assert.IsTrue(registry.TryGet(type, out _), type);
        }

        [Test]
        public void ReleaseBuildsContainNoHarnessAssembly()
        {
            var package = PackageInfo.FindForAssembly(typeof(PlanRunner).Assembly);
            Assert.IsNotNull(package);
            var gate = "\"DEVELOPMENT_BUILD || UNITY_EDITOR\"";

            foreach (var asmdef in Directory.GetFiles(Path.Combine(package.resolvedPath, "Runtime"), "*.asmdef", SearchOption.AllDirectories))
                StringAssert.Contains(gate, File.ReadAllText(asmdef), asmdef);

            var runtime = Directory.GetFiles(Path.Combine(package.resolvedPath, "Runtime"), "*.cs", SearchOption.AllDirectories);
            Assert.IsTrue(runtime.Any(path => File.ReadAllText(path).Contains("RuntimeInitializeOnLoadMethod")), "the bootstrap lives in the gated Runtime assembly");
            var runtimeAsmdef = File.ReadAllText(Path.Combine(package.resolvedPath, "Runtime", "Valtrion.Unity.asmdef"));
            StringAssert.Contains("\"noEngineReferences\": false", runtimeAsmdef);
            // Auto-referenced so a project's Assembly-CSharp can hold steps (under #if DEVELOPMENT_BUILD || UNITY_EDITOR); the define constraint keeps Release players free of it.
            StringAssert.Contains("\"autoReferenced\": true", runtimeAsmdef);
            StringAssert.Contains(gate, runtimeAsmdef);

            // The plan test helper is gated the same way and is never auto-referenced (a project test assembly names it).
            var testing = Directory.GetFiles(Path.Combine(package.resolvedPath, "Testing"), "*.asmdef", SearchOption.AllDirectories);
            Assert.IsNotEmpty(testing, "Testing/ holds the plan test helper assembly");
            foreach (var asmdef in testing)
            {
                var text = File.ReadAllText(asmdef);
                StringAssert.Contains(gate, text, asmdef);
                StringAssert.Contains("\"autoReferenced\": false", text, asmdef);
            }
        }

        static string[] FilterPlayerAssemblies(UnityEditor.BuildOptions options, bool playModeTestRunnerEnabled, string[] assemblies)
        {
            var filter = Type.GetType("Valtrion.Unity.Editor.PlanTestBuildAssemblyFilter, Valtrion.Unity.Editor", true);
            Assert.IsTrue(typeof(UnityEditor.Build.IFilterBuildAssemblies).IsAssignableFrom(filter));
            var method = filter.GetMethod("Filter", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
            return (string[])method.Invoke(null, new object[] { options, playModeTestRunnerEnabled, assemblies });
        }

        [Test]
        public void AnOrdinaryPlayerBuildLeavesOutOnlyThePlanTestHelper()
        {
            var assemblies = new[] { "Library/PlayerScriptAssemblies/Valtrion.Unity.dll", "Library/PlayerScriptAssemblies/Valtrion.Unity.PlayModeTests.dll",
                "Library/PlayerScriptAssemblies/Valtrion.Unity.PlayModeTests.Extra.dll", "Library/PlayerScriptAssemblies/Assembly-CSharp.dll" };

            CollectionAssert.AreEqual(new[] { assemblies[0], assemblies[2], assemblies[3] },
                FilterPlayerAssemblies(UnityEditor.BuildOptions.Development, false, assemblies));
            CollectionAssert.AreEqual(assemblies,
                FilterPlayerAssemblies(UnityEditor.BuildOptions.Development | UnityEditor.BuildOptions.IncludeTestAssemblies, false, assemblies));
            CollectionAssert.AreEqual(assemblies, FilterPlayerAssemblies(UnityEditor.BuildOptions.Development, true, assemblies));
        }
    }
}
