using System.IO;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class PlanRunnerTests
    {
        HarnessFixture _f;

        [SetUp]
        public void SetUp()
        {
            _f = new HarnessFixture();
            HarnessFixture.CounterValue = 0;
        }

        [TearDown]
        public void TearDown() => _f.Dispose();

        [Test]
        public void PassingPlanWritesResultHeartbeatAndOneTerminalLine()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.5},{\"type\":\"mark\",\"timeoutSec\":5,\"name\":\"m\"}");

            var runner = _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(0, result.ExitCode);
            Assert.AreEqual(2, result.Steps.Count);
            Assert.IsTrue(result.Steps[1].Marks.ContainsKey("m"));
            Assert.IsTrue(runner.Finished);
            Assert.AreEqual(1, _f.Log.Count(line => line.StartsWith("GDV-RESULT")));
            Assert.AreEqual("GDV-RESULT PASS run=test-run", _f.Log.Single(line => line.StartsWith("GDV-RESULT")));
            var heartbeat = HeartbeatDocument.Parse(File.ReadAllText(Path.Combine(_f.RunDirectory, "heartbeat.json")));
            Assert.AreEqual("done", heartbeat.State);
            Assert.IsNull(_f.QuitCode, "the app stays open after the result");
            Assert.IsEmpty(Directory.GetFiles(_f.RunDirectory, "*.tmp-*"), "the atomic write leaves no temporary file");
        }

        [Test]
        public void OutRootReplacesTheDefaultOutputFolder()
        {
            var outRoot = Path.Combine(_f.Root, "custom-out");
            _f.Options.OutRoot = outRoot;

            _f.Run(HarnessFixture.Plan("{\"type\":\"mark\",\"timeoutSec\":5,\"name\":\"m\"}"));

            Assert.IsTrue(File.Exists(Path.Combine(outRoot, "test-run", "result.json")));
            Assert.IsTrue(File.Exists(Path.Combine(outRoot, "test-run", "heartbeat.json")));
            Assert.IsTrue(File.Exists(Path.Combine(outRoot, "current.json")));
            Assert.IsFalse(Directory.Exists(Path.Combine(_f.Root, "gdv", "out")));
        }

        [Test]
        public void FirstFailureAbortsTheRun()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"test.fail\",\"timeoutSec\":5},{\"type\":\"test.counter\",\"timeoutSec\":5}");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("boom", result.Reason);
            Assert.AreEqual(1, result.Steps.Count);
            Assert.AreEqual(0, HarnessFixture.CounterValue);
        }

        [Test]
        public void ContinueOnFailRunsTheRestAndKeepsTheFirstFailure()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"test.fail\",\"timeoutSec\":5},{\"type\":\"test.counter\",\"timeoutSec\":5}", ",\"continueOnFail\":true");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            Assert.AreEqual(2, result.Steps.Count);
            Assert.AreEqual(1, HarnessFixture.CounterValue);
        }

        [Test]
        public void StepTimeoutEndsTheRunAsTimeout()
        {
            _f.Options.Conditions.Register("never", () => false);
            var plan = HarnessFixture.Plan("{\"type\":\"waitUntil\",\"timeoutSec\":1,\"condition\":\"never\"}");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            Assert.AreEqual(3, result.ExitCode);
            StringAssert.Contains("never", result.Steps[0].Message);
        }

        [Test]
        public void AStepThatNeverReturnsToTheRunnerIsEndedByTheHostsHeartbeatAtItsTimeout()
        {
            _f.Registry.Register("test.hang", HangAtEndOfFrame);
            var plan = HarnessFixture.Plan("{\"type\":\"test.hang\",\"timeoutSec\":2}");
            var runner = new PlanRunner(plan, plan.ToJsonString(), _f.Options);
            var routine = runner.Execute();

            Assert.IsTrue(routine.MoveNext(), "the step is waiting for the end of a frame the host never delivers");
            _f.Clock.Advance(2500);
            runner.Heartbeat();
            Assert.IsFalse(runner.Finished, "inside the timeout and its grace the runner waits");

            _f.Clock.Advance(1000);
            runner.Heartbeat();

            Assert.IsTrue(runner.Finished);
            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            Assert.AreEqual(3, result.ExitCode);
            StringAssert.Contains("did not return to the runner", result.Steps[0].Message);
            Assert.AreEqual(1, _f.Log.Count(line => line.StartsWith("GDV-RESULT")));
            Assert.IsFalse(routine.MoveNext(), "a host that resumes the coroutine later finds the run finished");
        }

        [Test]
        public void AHungStepAfterAnEarlierFailureReportsTheHangAndThePlanBudgetStillWins()
        {
            _f.Registry.Register("test.hang", HangAtEndOfFrame);
            var plan = HarnessFixture.Plan("{\"type\":\"test.fail\",\"timeoutSec\":5},{\"type\":\"test.hang\",\"timeoutSec\":600}", ",\"continueOnFail\":true", "functional", 60);
            var runner = new PlanRunner(plan, plan.ToJsonString(), _f.Options);
            var routine = runner.Execute();
            for (var count = 0; !runner.Finished && count < 20; count++)
            {
                routine.MoveNext();
                _f.Clock.Advance(16);
            }

            _f.Clock.Advance(61000 + 1500);
            runner.Heartbeat();

            Assert.IsTrue(runner.Finished, "the budget of 60 s ends the hung step although its own timeout is 600 s");
            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("plan budget of 60 s exceeded", result.Reason);
            StringAssert.Contains("did not return to the runner", result.Reason);
            Assert.AreEqual(2, result.Steps.Count);
        }

        [Test]
        public void AHungStepAfterAnEarlierFailureIsReportedAsTheCauseNotTheEarlierFailure()
        {
            _f.Registry.Register("test.hang", HangAtEndOfFrame);
            var plan = HarnessFixture.Plan("{\"type\":\"test.fail\",\"timeoutSec\":5},{\"type\":\"test.hang\",\"timeoutSec\":2}", ",\"continueOnFail\":true");
            var runner = new PlanRunner(plan, plan.ToJsonString(), _f.Options);
            var routine = runner.Execute();
            for (var count = 0; !runner.Finished && count < 20; count++)
            {
                routine.MoveNext();
                _f.Clock.Advance(16);
            }

            _f.Clock.Advance(3500);
            runner.Heartbeat();

            Assert.IsTrue(runner.Finished);
            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("did not return to the runner", result.Reason);
            StringAssert.DoesNotContain("boom", result.Reason);
        }

        [Test]
        public void TheScreenshotStepIsBlockedAtOnceWhereNoFramesAreRendered()
        {
            var original = BuiltInSteps.ScreenCaptureAvailable;
            BuiltInSteps.ScreenCaptureAvailable = () => false;
            try
            {
                _f.Run(HarnessFixture.Plan("{\"type\":\"screenshot\",\"timeoutSec\":60,\"name\":\"menu\"}"));
            }
            finally
            {
                BuiltInSteps.ScreenCaptureAvailable = original;
            }

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            Assert.AreEqual(2, result.ExitCode);
            StringAssert.Contains("screen capture is unavailable", result.Steps[0].Message);
            Assert.Less(_f.Clock.NowMs, 1000.0, "it did not wait for its 60 s timeout");
        }

        [Test]
        public void AThrowingStepReportsTheExceptionTypeTheFirstFrameAndLogsTheFullException()
        {
            _f.Registry.Register("test.throw", ThrowStep);

            _f.Run(HarnessFixture.Plan("{\"type\":\"test.throw\",\"timeoutSec\":5}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("NullReferenceException: Object reference not set", result.Steps[0].Message);
            StringAssert.Contains(" (at ", result.Steps[0].Message);
            StringAssert.Contains("ThrowStep", result.Steps[0].Message, "the frame names the place that threw");
            StringAssert.Contains("ThrowStep", result.Reason);
            Assert.IsTrue(_f.Log.Any(line => line.StartsWith("GDV: step") && line.Contains("NullReferenceException") && line.Contains("ThrowStep")),
                "the full exception with its stack is logged");
        }

        [Test]
        public void AnArgumentErrorKeepsItsOwnWordingWithoutAStack()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5}"));

            var result = _f.ReadResult();
            StringAssert.Contains("invalid arguments: ", result.Steps[0].Message);
            StringAssert.DoesNotContain(" (at ", result.Steps[0].Message);
            Assert.IsFalse(_f.Log.Any(line => line.StartsWith("GDV: step") && line.Contains("threw")));
        }

        sealed class TracedException : System.Exception
        {
            readonly string _trace;

            public TracedException(string message, string trace, System.Exception inner = null)
                : base(message, inner)
            {
                _trace = trace;
            }

            public override string StackTrace => _trace;
        }

        sealed class ExplodingMessageException : System.Exception
        {
            public override string Message => throw new System.InvalidOperationException("the message cannot be read");
        }

        [Test]
        public void TheFirstFrameIsTheFirstNonEmptyLineWithoutItsAtPrefixAndIsCapped()
        {
            Assert.AreEqual(" (at A.B()  [0x1] in Foo.cs:3)", Faults.FirstFrame(new TracedException("x", "\n   at A.B()  [0x1] in Foo.cs:3\n   at C.D()")));
            Assert.AreEqual(" (at Plain.Frame())", Faults.FirstFrame(new TracedException("x", "Plain.Frame()\nOther.Frame()")));
            Assert.AreEqual(string.Empty, Faults.FirstFrame(new TracedException("x", " \n ")));
            var capped = Faults.FirstFrame(new TracedException("x", "at " + new string('a', 500)));
            Assert.AreEqual(" (at ".Length + 300 + "...".Length + ")".Length, capped.Length);
        }

        [Test]
        public void AWrapperExceptionIsDescribedByItsCauseAndTheStepStillFailsWhenTheMessageCannotBeRead()
        {
            var inner = new TracedException("deep", "at Real.Cause()");
            var wrapped = new System.Reflection.TargetInvocationException(inner);
            Assert.AreEqual("TargetInvocationException -> TracedException: deep (at Real.Cause())", Faults.Describe(wrapped));

            _f.Registry.Register("test.explode", context => Explode());
            _f.Run(HarnessFixture.Plan("{\"type\":\"test.explode\",\"timeoutSec\":5}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status, "the run still ends with a result");
            StringAssert.Contains("ExplodingMessageException (details unavailable)", result.Steps[0].Message);
        }

        static System.Collections.IEnumerator Explode()
        {
            throw new ExplodingMessageException();
#pragma warning disable CS0162
            yield break;
#pragma warning restore CS0162
        }

        [Test]
        public void FaultsDescribeAnExceptionWithoutAStackTraceByItsTypeAndMessageOnly()
        {
            Assert.AreEqual("InvalidOperationException: nothing", Faults.Describe(new System.InvalidOperationException("nothing")));
            Assert.AreEqual(string.Empty, Faults.FirstFrame(new System.InvalidOperationException("nothing")));
        }

        static System.Collections.IEnumerator ThrowStep(StepContext context)
        {
            string text = null;
            context.Note(text.Length.ToString());
            yield break;
        }

        static System.Collections.IEnumerator HangAtEndOfFrame(StepContext context)
        {
            while (true)
                yield return new UnityEngine.WaitForEndOfFrame();
        }

        [Test]
        public void PlanBudgetEndsTheRunAsTimeoutEvenWithContinueOnFail()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":100,\"seconds\":50},{\"type\":\"test.counter\",\"timeoutSec\":5}", ",\"continueOnFail\":true", "functional", 2);

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("budget", result.Reason);
            Assert.AreEqual(0, HarnessFixture.CounterValue);
        }

        [Test]
        public void UnknownStepTypeIsBlocked()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"no.such.step\",\"timeoutSec\":5}"));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            Assert.AreEqual(2, result.ExitCode);
        }

        [Test]
        public void ThrowingStepFailsWithTheException()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("'seconds' is required", result.Reason);
        }

        [Test]
        public void WarmupIterationsRunButAreNotMeasured()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"test.counter\",\"timeoutSec\":5},{\"type\":\"test.sample\",\"timeoutSec\":5,\"value\":2}", ",\"repeat\":3,\"warmup\":1");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(4, HarnessFixture.CounterValue);
            Assert.AreEqual(6, result.Steps.Count, "warm-up steps that pass are not recorded");
            var metric = result.Metrics.Single(m => m.Name == "test.value");
            Assert.AreEqual(3, metric.Stats.N);
            Assert.AreEqual(2, metric.Stats.Median);
        }

        [Test]
        public void ExternalUiIsRefusedInsideAnOpenProbeWindow()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[\"timer:t\"]},{\"type\":\"external.ui\",\"timeoutSec\":5,\"action\":\"x\"}");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("probe window", result.Reason);
        }

        [Test]
        public void ExternalUiPutsTheRequestInTheHeartbeatAndWaitsForTheReply()
        {
            _f.Options.HeartbeatIntervalMs = 0;
            var plan = HarnessFixture.Plan("{\"type\":\"external.ui\",\"timeoutSec\":30,\"action\":\"allow-notifications\",\"args\":{\"x\":1}}");
            string requestId = null;

            var runner = new PlanRunner(plan, plan.ToJsonString(), _f.Options);
            var routine = runner.Execute();
            for (var count = 0; routine.MoveNext(); count++)
            {
                _f.Clock.Advance(16);
                var path = Path.Combine(_f.RunDirectory, "heartbeat.json");
                if (requestId == null && File.Exists(path))
                {
                    var heartbeat = HeartbeatDocument.Parse(File.ReadAllText(path));
                    if (heartbeat.Request != null)
                    {
                        Assert.AreEqual("waiting-host", heartbeat.State);
                        Assert.AreEqual("allow-notifications", heartbeat.Request.Action);
                        requestId = heartbeat.Request.Id;
                        Directory.CreateDirectory(_f.Inbox);
                        File.WriteAllText(Path.Combine(_f.Inbox, "reply-" + requestId + ".json"), "{\"status\":\"NEEDS_PERMISSION\",\"message\":\"owner must unlock\"}");
                    }
                }
                Assert.Less(count, 1000);
            }

            Assert.IsNotNull(requestId, "the request reached the heartbeat");
            var result = _f.ReadResult();
            Assert.AreEqual("NEEDS_PERMISSION", result.Status);
            StringAssert.Contains("owner must unlock", result.Reason);
            Assert.IsEmpty(Directory.GetFiles(_f.Inbox, "reply-*.json"), "the reply is renamed after use");
        }

        [Test]
        public void FilePutIsRemovedByTheLedgerCleanup()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"file.put\",\"timeoutSec\":5,\"path\":\"a/b.txt\",\"text\":\"hi\"}");
            var target = Path.Combine(_f.Root, "gdv", "work", "a", "b.txt");

            _f.Run(plan);

            Assert.AreEqual("PASS", _f.ReadResult().Status);
            Assert.IsFalse(File.Exists(target));
        }

        [Test]
        public void CleanupNoneKeepsTheFile()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"file.put\",\"timeoutSec\":5,\"path\":\"keep.txt\",\"text\":\"hi\"}", ",\"cleanup\":\"none\"");

            _f.Run(plan);

            Assert.IsTrue(File.Exists(Path.Combine(_f.Root, "gdv", "work", "keep.txt")));
        }

        [Test]
        public void FilePutRefusesToLeaveItsFolderAndToOverwriteWithoutPermission()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"file.put\",\"timeoutSec\":5,\"path\":\"../escape.txt\"}"));
            StringAssert.Contains("must not leave", _f.ReadResult().Reason);

            var existing = Path.Combine(_f.Root, "gdv", "work", "e.txt");
            Directory.CreateDirectory(Path.GetDirectoryName(existing));
            File.WriteAllText(existing, "mine");
            _f.Options.RunId = "second";
            var runner = new PlanRunner(HarnessFixture.Plan("{\"type\":\"file.put\",\"timeoutSec\":5,\"path\":\"e.txt\",\"text\":\"x\"}"), null, _f.Options);
            _f.Pump(runner);
            Assert.AreEqual("FAIL", runner.Result.Status);
            Assert.AreEqual("mine", File.ReadAllText(existing));
        }

        [Test]
        public void LedgerRestoresAnOverwrittenFile()
        {
            var existing = Path.Combine(_f.Root, "gdv", "work", "e.txt");
            Directory.CreateDirectory(Path.GetDirectoryName(existing));
            File.WriteAllText(existing, "mine");

            _f.Run(HarnessFixture.Plan("{\"type\":\"file.put\",\"timeoutSec\":5,\"path\":\"e.txt\",\"text\":\"x\",\"overwrite\":true}"));

            Assert.AreEqual("PASS", _f.ReadResult().Status);
            Assert.AreEqual("mine", File.ReadAllText(existing));
        }

        [Test]
        public void LogExpectWaitsForALineLoggedAfterItBegan()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"log.expect\",\"timeoutSec\":30,\"pattern\":\"ready \\\\d+\"}");
            var runner = new PlanRunner(plan, null, _f.Options);
            runner.Context.Logs.Feed("ready 1");
            var fed = false;

            _f.Pump(runner, () =>
            {
                if (!fed && _f.Clock.NowMs > 100)
                {
                    fed = true;
                    runner.Context.Logs.Feed("ready 42");
                }
            });

            var result = runner.Result;
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual("ready 42", result.Steps[0].Message);
        }

        [Test]
        public void LogProbeTurnsMatchingLinesIntoSamples()
        {
            var plan = HarnessFixture.Plan(
                "{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[{\"type\":\"log\",\"name\":\"LOAD\",\"pattern\":\"load (?<value>\\\\d+)ms\",\"unit\":\"ms\",\"metric\":\"loadMs\"}]},"
                + "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"load 12ms\"},{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"other\"},{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"load 30ms\"},"
                + "{\"type\":\"probe.stop\",\"timeoutSec\":5}");

            _f.Run(plan);

            var metric = _f.ReadResult().Metrics.Single(m => m.Name == "loadMs");
            CollectionAssert.AreEqual(new[] { 12.0, 30.0 }, metric.Samples);
            Assert.AreEqual("ms", metric.Unit);
            Assert.AreEqual(30, metric.Stats.Max);
        }

        [Test]
        public void TimerProbeReportsOneSamplePerWindow()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[\"timer:openMs\"]},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1},{\"type\":\"probe.stop\",\"timeoutSec\":5}", ",\"repeat\":2");

            _f.Run(plan);

            var metric = _f.ReadResult().Metrics.Single(m => m.Name == "openMs");
            Assert.AreEqual(2, metric.Stats.N);
            Assert.That(metric.Stats.Median, Is.InRange(1000, 1100));
        }

        [Test]
        public void FrameProbeEmitsDeltaAndP99AndUnavailableNotesWhenStatsAreDisabled()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[\"frameTiming\"]},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1},{\"type\":\"probe.stop\",\"timeoutSec\":5}", string.Empty, "performance");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            var delta = result.Metrics.Single(m => m.Name == "frameTime.deltaMs");
            Assert.That(delta.Stats.Median, Is.EqualTo(16).Within(0.01));
            Assert.AreEqual(1, result.Metrics.Single(m => m.Name == "frameTime.deltaMs.p99").Stats.N);
            if (!UnityEngine.FrameTimingManager.IsFeatureEnabled())
                Assert.IsTrue(result.Unverified.Any(text => text.Contains("frameTime.cpuMs and frameTime.gpuMs unavailable")));
        }

        sealed class UnavailableProbe : Valtrion.Unity.Probes.IProbe
        {
            public string Name => "unavailable";

            public System.Collections.Generic.IReadOnlyList<string> Start(Valtrion.Unity.Probes.IMetricSink sink) => new[] { "nothing to measure" };

            public void Sample(Valtrion.Unity.Probes.IMetricSink sink, long frame, double nowMs)
            {
            }

            public void Stop(Valtrion.Unity.Probes.IMetricSink sink)
            {
            }

            public void Dispose()
            {
            }
        }

        [Test]
        public void RequiredProbeThatIsUnavailableBlocksAndAnOptionalOneIsListedAsUnverified()
        {
            Valtrion.Unity.Probes.ProbeFactory.Register("test.unavailable", (spec, run) => new UnavailableProbe());

            _f.Run(HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"required\":true,\"probes\":[\"test.unavailable\"]}"));
            var blocked = _f.ReadResult();
            Assert.AreEqual("BLOCKED", blocked.Status);
            StringAssert.Contains("nothing to measure", blocked.Reason);

            _f.Options.RunId = "optional";
            var runner = new PlanRunner(HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[\"test.unavailable\"]},{\"type\":\"probe.stop\",\"timeoutSec\":5}"), null, _f.Options);
            _f.Pump(runner);
            Assert.AreEqual("PASS", runner.Result.Status);
            Assert.IsTrue(runner.Result.Unverified.Any(text => text.Contains("probe unavailable: nothing to measure")));
        }

        [Test]
        public void DanglingProbeWindowIsClosedAndReported()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[\"timer:t\"]}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsTrue(result.Unverified.Any(text => text.Contains("probe window was still open")));
            Assert.AreEqual(1, result.Metrics.Single(m => m.Name == "t").Stats.N);
        }

        static System.Collections.IEnumerator YieldsSeconds(StepContext context)
        {
            yield return new UnityEngine.WaitForSeconds(600);
        }

        [Test]
        public void StepYieldingAnUnboundedInstructionFailsInsteadOfEscapingItsTimeout()
        {
            _f.Registry.Register("test.yields", YieldsSeconds);

            _f.Run(HarnessFixture.Plan("{\"type\":\"test.yields\",\"timeoutSec\":1}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("WaitForSeconds", result.Reason);
        }

        [Test]
        public void DetectReadsCandidateJsonAndSanitizesIdentifiers()
        {
            Directory.CreateDirectory(_f.Inbox);
            var file = Path.Combine(_f.Inbox, RunEnvironment.CandidateFile);
            File.WriteAllText(file, "{\"repo\":\"r\",\"sha\":\"" + new string('b', 40) + "\",\"dirty\":false,\"deviceId\":\"iPad (Test)\",\"deviceClass\":\"ipad a14\"}");

            var environment = RunEnvironment.Detect(_f.Inbox);

            Assert.AreEqual(new string('b', 40), environment.Candidate.Sha);
            Assert.IsFalse(environment.Candidate.Dirty);
            Assert.AreEqual("iPad--Test-", environment.Target.DeviceId);
            Assert.AreEqual("ipad-a14", environment.Target.DeviceClass);
            Assert.IsFalse(File.Exists(file), "candidate.json is consumed");
            Assert.AreEqual(1, Directory.GetFiles(_f.Inbox, "candidate.json.done-*").Length);
            var result = new ResultDocument
            {
                RunId = "x", Status = "PASS", Candidate = environment.Candidate, Target = environment.Target, Host = environment.Host,
                Plan = new ResultPlan { Id = "p", Sha256 = new string('c', 64) }, StartedUtc = "2026-10-29T15:30:00Z", FinishedUtc = "2026-10-29T15:30:01Z", Layers = new ResultLayers(),
            };
            Assert.DoesNotThrow(() => ResultDocument.Parse(result.ToJsonString()), "the detected environment forms a valid result");
        }

        [Test]
        public void HugeRepeatOfInstantStepsStillHonoursTheBudgetOneFramePerStep()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"mark\",\"timeoutSec\":5,\"name\":\"m\"}", ",\"repeat\":1000000", "functional", 2);

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("budget", result.Reason);
            Assert.Less(result.Steps.Count, 300);
        }

        [Test]
        public void StaleResultOfARelaunchWithTheSameRunIdIsRemovedAtStart()
        {
            Directory.CreateDirectory(_f.RunDirectory);
            File.WriteAllText(Path.Combine(_f.RunDirectory, "result.json"), "old");
            var runner = new PlanRunner(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1}"), null, _f.Options);
            var routine = runner.Execute();
            routine.MoveNext();

            Assert.IsFalse(File.Exists(Path.Combine(_f.RunDirectory, "result.json")));
        }

        [Test]
        public void AbortStopsTheCoroutine()
        {
            var runner = new PlanRunner(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1}"), null, _f.Options);
            var routine = runner.Execute();
            routine.MoveNext();
            runner.Abort("FAIL", "stop");

            Assert.IsFalse(routine.MoveNext());
        }

        [Test]
        public void AppQuitQuitsOnlyAfterTheResultIsWritten()
        {
            var runner = new PlanRunner(HarnessFixture.Plan("{\"type\":\"app.quit\",\"timeoutSec\":5,\"exitCode\":3}"), null, _f.Options);
            _f.Options.Quit = code =>
            {
                Assert.IsTrue(File.Exists(Path.Combine(_f.RunDirectory, "result.json")));
                _f.Log.Add("quit " + code);
            };

            _f.Pump(runner);

            CollectionAssert.Contains(_f.Log, "quit 3");
        }

        [Test]
        public void RejectedPlanWritesAFailResult()
        {
            var runner = PlanRunner.ForRejectedPlan("plan 'x' was rejected: nope", "{}", _f.Options);

            _f.Pump(runner);

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("nope", result.Reason);
            Assert.AreEqual("invalid-plan", result.Plan.Id);
        }

        [Test]
        public void AbortWritesTheResultOnce()
        {
            var runner = new PlanRunner(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1}"), null, _f.Options);
            var routine = runner.Execute();
            routine.MoveNext();

            runner.Abort("FAIL", "quit");
            runner.Abort("FAIL", "again");

            Assert.AreEqual(1, _f.Log.Count(line => line.StartsWith("GDV-RESULT")));
            Assert.AreEqual("quit", _f.ReadResult().Reason);
        }

        [Test]
        public void StatisticsUseNearestRankPercentiles()
        {
            var stats = Statistics.Compute(new[] { 5.0, 1.0, 3.0, 2.0, 4.0, 10.0, 9.0, 8.0, 7.0, 6.0 });

            Assert.AreEqual(10, stats.N);
            Assert.AreEqual(5.5, stats.Median);
            Assert.AreEqual(9, stats.P90);
            Assert.AreEqual(10, stats.Max);
            Assert.AreEqual(99, Statistics.Percentile(Enumerable.Range(1, 100).Select(i => (double)i).ToList(), 99));
        }
    }
}
