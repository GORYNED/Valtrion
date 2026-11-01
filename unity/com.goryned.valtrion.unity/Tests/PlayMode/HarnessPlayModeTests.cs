using System;
using System.Collections;
using System.IO;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using Valtrion.Unity.Testing;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

namespace Valtrion.Unity.PlayModeTests
{
    public sealed class HarnessPlayModeTests
    {
        const string Plan = "{\"schema\":\"gdv.plan/1\",\"id\":\"playmode.smoke\",\"kind\":\"functional\",\"dataPolicy\":\"isolated\",\"budgetSeconds\":60,\"repeat\":2,"
            + "\"steps\":[{\"type\":\"idle\",\"timeoutSec\":10,\"frames\":3},"
            + "{\"type\":\"probe.start\",\"timeoutSec\":10,\"probes\":[\"frameTiming\",\"profiler\",\"timer:windowMs\"]},"
            + "{\"type\":\"wait\",\"timeoutSec\":10,\"seconds\":0.3},"
            + "{\"type\":\"probe.stop\",\"timeoutSec\":10},"
            + "{\"type\":\"gc\",\"timeoutSec\":30},"
            + "{\"type\":\"mark\",\"timeoutSec\":5,\"name\":\"done\"}]}";

        string _root;

        [SetUp]
        public void SetUp() => _root = Path.Combine(Path.GetTempPath(), "gdv-playmode-" + Guid.NewGuid().ToString("N").Substring(0, 8));

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(_root))
                Directory.Delete(_root, true);
        }

        RunOptions Options() => new RunOptions
        {
            PersistentDataPath = _root,
            RootDirectory = Path.Combine(_root, "gdv"),
            DiscoverSteps = false,
            RunId = "playmode-run",
        };

        [UnityTest]
        public IEnumerator RunsAPlanOnTheMainThreadWithoutBlockingFrames()
        {
            var runner = ValidationHarness.Start(PlanDocument.Parse(Plan), Plan, Options());
            var frames = 0;
            var start = Time.frameCount;

            while (!runner.Finished)
            {
                frames++;
                yield return null;
            }

            Assert.Greater(Time.frameCount - start, 10, "the run spans many frames");
            var result = ResultDocument.Parse(File.ReadAllText(runner.ResultPath));
            Assert.AreEqual("PASS", result.Status, result.Reason);
            Assert.AreEqual(Application.isEditor ? EvidenceKind.Editor : EvidenceKind.Physical, result.Target.Evidence);
            Assert.AreEqual(2, result.Metrics.Single(m => m.Name == "windowMs").Stats.N);
            Assert.IsTrue(result.Metrics.Any(m => m.Name == "frameTime.deltaMs"));
            Assert.IsTrue(result.Metrics.Any(m => m.Name == "memory.totalUsedMB") || result.Unverified.Any(text => text.Contains("memory.totalUsedMB unavailable")));
        }

        [UnityTest]
        public IEnumerator PlanTestBridgeRunsAPlanFileAndPassesOnPass()
        {
            Directory.CreateDirectory(_root);
            var path = Path.Combine(_root, "smoke.plan.json");
            File.WriteAllText(path, Plan);

            yield return PlanTest.Run(path, Options());

            Assert.AreEqual(Path.Combine(_root, "gdv", "out", "playmode-run"), PlanTest.LastRunDirectory);
            Assert.IsTrue(File.Exists(Path.Combine(PlanTest.LastRunDirectory, "result.json")));
        }

        [UnityTest]
        public IEnumerator InboxPlanStartsTheHarnessAndIsConsumed()
        {
            var inbox = Path.Combine(_root, "gdv", "inbox");
            Directory.CreateDirectory(inbox);
            File.WriteAllText(Path.Combine(inbox, "plan.json"), Plan.Replace("playmode.smoke", "playmode.inbox"));
            var environment = new FakeEnvironment { PersistentDataPath = _root };

            Assert.IsTrue(ValidationHarness.TryStartFromInbox(environment, Options()));
            var runner = ValidationHarness.Current;
            while (!runner.Finished)
                yield return null;

            Assert.AreEqual("playmode.inbox", runner.Result.Plan.Id);
            Assert.IsFalse(File.Exists(Path.Combine(inbox, "plan.json")));
            Assert.AreEqual(1, Directory.GetFiles(inbox, "plan.json.done-*").Length);
            Assert.IsFalse(ValidationHarness.TryStartFromInbox(environment, Options()), "a consumed plan is not read twice");
        }

        [UnityTest]
        public IEnumerator AutomaticStartRunsOutsideATestRunAndIsSuppressedInsideOne()
        {
            Directory.CreateDirectory(_root);
            var suppressedPlan = Path.Combine(_root, "suppressed.plan.json");
            var startedPlan = Path.Combine(_root, "started.plan.json");
            File.WriteAllText(suppressedPlan, Plan.Replace("playmode.smoke", "playmode.suppressed"));
            File.WriteAllText(startedPlan, Plan.Replace("playmode.smoke", "playmode.started"));
            var before = ValidationHarness.Current;

            Assert.IsFalse(ValidationHarness.TryAutoStart(new FakeEnvironment { PersistentDataPath = _root, Arguments = new[] { "app", "-gdv-plan", suppressedPlan } }, () => true, Options()));
            Assert.AreSame(before, ValidationHarness.Current);
            Assert.IsTrue(File.Exists(suppressedPlan), "a suppressed start does not consume the plan");

            Assert.IsTrue(ValidationHarness.TryAutoStart(new FakeEnvironment { PersistentDataPath = _root, Arguments = new[] { "app", "-gdv-plan", startedPlan } }, () => false, Options()), "positive control: outside a test run the same input starts");
            var runner = ValidationHarness.Current;
            while (!runner.Finished)
                yield return null;
            Assert.AreEqual("playmode.started", runner.Result.Plan.Id);
            Assert.IsFalse(File.Exists(startedPlan), "a started plan is consumed");
        }

        [Test]
        public void EverySignalAloneMarksATestRun()
        {
            Assert.IsTrue(ValidationHarness.IsTestRunSignal(true, "Main", () => false), "explicit flag");
            Assert.IsTrue(ValidationHarness.IsTestRunSignal(false, "InitTestScene123", () => false), "scene fallback");
            Assert.IsTrue(ValidationHarness.IsTestRunSignal(false, "Main", () => true), "runner object");
            Assert.IsFalse(ValidationHarness.IsTestRunSignal(false, "Main", () => false), "a normal session is not a test run");
        }

        // Runs first: when the session is started with GDV_PLAN set (the real bootstrap scenario), the bootstrap must not have started that plan.
        [UnityTest, NUnit.Framework.Order(-1)]
        public IEnumerator BootstrapStartedNothingEvenWhenThePlanVariableWasSetBeforeUnityStarted()
        {
            // The bootstrap ran at play-mode entry, before this test: it must have seen either no plan or a test run, never started one.
            var planVariable = Environment.GetEnvironmentVariable("GDV_PLAN");
            // A plan variable that names a consumed plan (a sibling .done-* exists) is ignored by design; any other value (a file, or a missing file that
            // is reported as an error) is a plan for the bootstrap to see.
            var consumed = !string.IsNullOrEmpty(planVariable) && !File.Exists(planVariable) && Directory.Exists(Path.GetDirectoryName(planVariable))
                && Directory.GetFiles(Path.GetDirectoryName(planVariable), Path.GetFileName(planVariable) + ".done-*").Length > 0;
            if (!string.IsNullOrEmpty(planVariable) && !consumed)
                Assert.AreEqual(AutoStartDecision.SuppressedByTestRun, ValidationHarness.BootstrapDecision, "a plan was given before Unity started: the real bootstrap must have seen the test run");
            else
                Assert.AreNotEqual(AutoStartDecision.Started, ValidationHarness.BootstrapDecision, "the real bootstrap never starts a plan inside a test run");
            Assert.AreNotEqual(AutoStartDecision.NotEvaluated, ValidationHarness.BootstrapDecision, "the bootstrap ran");
            Assert.AreNotEqual(AutoStartDecision.Failed, ValidationHarness.BootstrapDecision);
            yield break;
        }

        [UnityTest]
        public IEnumerator EnvironmentPlanDoesNotAutoStartInATestRunAndPlanTestRunsItExactlyOnce()
        {
            Directory.CreateDirectory(_root);
            var planPath = Path.Combine(_root, "env.plan.json");
            var outRoot = Path.Combine(_root, "env-out");
            File.WriteAllText(planPath, Plan.Replace("playmode.smoke", "playmode.env"));
            var previous = new[] { Environment.GetEnvironmentVariable("GDV_PLAN"), Environment.GetEnvironmentVariable("GDV_OUT_ROOT"), Environment.GetEnvironmentVariable("GDV_RUN_ID") };
            Environment.SetEnvironmentVariable("GDV_PLAN", planPath);
            Environment.SetEnvironmentVariable("GDV_OUT_ROOT", outRoot);
            Environment.SetEnvironmentVariable("GDV_RUN_ID", "env-run");
            try
            {
                Assert.IsTrue(ValidationHarness.TestRunnerObjectPresent(), "the Test Framework's controller object is found by type");
                Assert.IsTrue(ValidationHarness.IsTestRun, "the Test Framework owns this play session");
                var before = ValidationHarness.Current;
                Assert.IsFalse(ValidationHarness.TryAutoStart(), "the bootstrap stays out of a test run");
                Assert.AreSame(before, ValidationHarness.Current);

                yield return PlanTest.RunFromEnvironment(new RunOptions { PersistentDataPath = _root, RootDirectory = Path.Combine(_root, "gdv"), DiscoverSteps = false });

                Assert.AreEqual(1, Directory.GetDirectories(outRoot).Length, "exactly one run folder");
                Assert.IsTrue(File.Exists(Path.Combine(outRoot, "env-run", "result.json")));
                Assert.IsTrue(File.Exists(planPath), "an explicit plan file is not renamed by a test");
                Assert.AreEqual("playmode.env", ValidationHarness.Current.Result.Plan.Id);
            }
            finally
            {
                Environment.SetEnvironmentVariable("GDV_PLAN", previous[0]);
                Environment.SetEnvironmentVariable("GDV_OUT_ROOT", previous[1]);
                Environment.SetEnvironmentVariable("GDV_RUN_ID", previous[2]);
            }
        }

        sealed class FakeEnvironment : IPlanEnvironment
        {
            public string[] Arguments { get; set; } = new string[0];

            public string[] CommandLineArgs => Arguments;

            public string PersistentDataPath { get; set; }

            public DateTime UtcNow => DateTime.UtcNow;

            public string GetIntentExtra(string name) => null;

            public string GetEnvironmentVariable(string name) => null;
        }
    }
}
