using System.Collections;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class RepeatStepTests
    {
        HarnessFixture _f;
        int _calls;
        int _failOnCall;

        [SetUp]
        public void SetUp()
        {
            _f = new HarnessFixture();
            _calls = 0;
            _failOnCall = -1;
            _f.Registry.Register("test.call", Call);
            _f.Registry.Register("test.blocked", context =>
            {
                context.Blocked("no device");
                return new object[0].GetEnumerator();
            });
            _f.Registry.Register("test.throws", context => throw new System.InvalidOperationException("kaboom"));
            _f.Registry.Register("test.badYield", BadYield);
            _f.Registry.Register("test.hang", Hang);
        }

        [TearDown]
        public void TearDown() => _f.Dispose();

        IEnumerator Call(StepContext context)
        {
            _calls++;
            context.AddMetric("call.ms", "ms", _calls);
            if (_calls == _failOnCall)
                context.Fail("call " + _calls + " failed");
            yield return null;
        }

        static IEnumerator BadYield(StepContext context)
        {
            yield return new object();
        }

        static IEnumerator Hang(StepContext context)
        {
            while (true)
                yield return null;
        }

        static string Repeat(int count, string steps, string extra = "", int timeout = 30) =>
            "{\"type\":\"repeat\",\"timeoutSec\":" + timeout + ",\"count\":" + count + ",\"steps\":[" + steps + "]" + extra + "}";

        const string CallStep = "{\"type\":\"test.call\",\"timeoutSec\":5}";

        [Test]
        public void RunsTheInnerStepsCountTimesAndReportsTheSummary()
        {
            _f.Run(HarnessFixture.Plan(Repeat(5, CallStep)));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(5, _calls);
            Assert.AreEqual(new double[] { 1, 2, 3, 4, 5 }, result.Metrics.Single(m => m.Name == "call.ms").Samples.ToArray(), "one sample per round");
            Assert.AreEqual(5, result.Metrics.Single(m => m.Name == "repeat.rounds").Samples.Single());
            Assert.AreEqual(0, result.Metrics.Single(m => m.Name == "repeat.failedRounds").Samples.Single());
            Assert.AreEqual(5, result.Metrics.Single(m => m.Name == "call.ms").Stats.N);
            StringAssert.Contains("5 of 5 rounds run, 0 failed", result.Steps[0].Message);
        }

        [Test]
        public void StepsOfARoundRunInOrder()
        {
            var order = new System.Collections.Generic.List<string>();
            _f.Registry.Register("test.a", context =>
            {
                order.Add("a");
                return new object[0].GetEnumerator();
            });
            _f.Registry.Register("test.b", context =>
            {
                order.Add("b");
                return new object[0].GetEnumerator();
            });

            _f.Run(HarnessFixture.Plan(Repeat(2, "{\"type\":\"test.a\",\"timeoutSec\":5},{\"type\":\"test.b\",\"timeoutSec\":5}")));

            Assert.AreEqual("abab", string.Concat(order));
        }

        [Test]
        public void ArgumentsOfTheInnerStepReachIt()
        {
            _f.Run(HarnessFixture.Plan(Repeat(2, "{\"type\":\"test.sample\",\"timeoutSec\":5,\"value\":7}")));

            Assert.AreEqual(new double[] { 7, 7 }, _f.ReadResult().Metrics.Single(m => m.Name == "test.value").Samples.ToArray());
        }

        [Test]
        public void FirstFailureStopsTheLoopAndNamesTheRound()
        {
            _failOnCall = 3;

            _f.Run(HarnessFixture.Plan(Repeat(10, CallStep)));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            Assert.AreEqual(3, _calls);
            StringAssert.Contains("3 of 10 rounds run, 1 failed", result.Reason);
            StringAssert.Contains("round 3, test.call FAIL: call 3 failed", result.Reason);
            Assert.AreEqual(3, result.Metrics.Single(m => m.Name == "repeat.rounds").Samples.Single());
            Assert.AreEqual(1, result.Metrics.Single(m => m.Name == "repeat.failedRounds").Samples.Single());
        }

        [Test]
        public void ContinueOnFailFinishesTheRoundsAndStillEndsFail()
        {
            _failOnCall = 2;

            _f.Run(HarnessFixture.Plan(Repeat(4, CallStep, ",\"continueOnFail\":true")));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            Assert.AreEqual(4, _calls);
            StringAssert.Contains("4 of 4 rounds run, 1 failed; first failure: round 2", result.Reason);
            Assert.AreEqual(1, result.Metrics.Single(m => m.Name == "repeat.failedRounds").Samples.Single());
        }

        [Test]
        public void FailedRoundSkipsTheRestOfThatRound()
        {
            var second = 0;
            _f.Registry.Register("test.second", context =>
            {
                second++;
                return new object[0].GetEnumerator();
            });
            _failOnCall = 1;

            _f.Run(HarnessFixture.Plan(Repeat(3, CallStep + ",{\"type\":\"test.second\",\"timeoutSec\":5}", ",\"continueOnFail\":true")));

            Assert.AreEqual(3, _calls);
            Assert.AreEqual(2, second, "round 1 failed before the second step");
        }

        [Test]
        public void BlockedInnerStepStopsTheLoopWithThatStatus()
        {
            _f.Run(HarnessFixture.Plan(Repeat(5, "{\"type\":\"test.blocked\",\"timeoutSec\":5}", ",\"continueOnFail\":true")));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            StringAssert.Contains("no device", result.Reason);
        }

        [Test]
        public void UnknownInnerStepTypeIsBlocked()
        {
            _f.Run(HarnessFixture.Plan(Repeat(2, "{\"type\":\"no.such.step\",\"timeoutSec\":5}")));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void InnerStepThatThrowsIsAFailedRoundWithItsMessage()
        {
            _f.Run(HarnessFixture.Plan(Repeat(2, "{\"type\":\"test.throws\",\"timeoutSec\":5}")));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("kaboom", result.Reason);
        }

        [Test]
        public void InnerStepThatYieldsSomethingElseFails()
        {
            _f.Run(HarnessFixture.Plan(Repeat(1, "{\"type\":\"test.badYield\",\"timeoutSec\":5}")));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("may yield only null", result.Reason);
        }

        [Test]
        public void InnerStepIsBoundedByItsOwnTimeout()
        {
            _f.Run(HarnessFixture.Plan(Repeat(3, "{\"type\":\"test.hang\",\"timeoutSec\":1}", "", 60)));

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("step timeout of 1 s", result.Reason);
        }

        [Test]
        public void WholeLoopIsBoundedByTheRepeatStepsTimeout()
        {
            _f.Run(HarnessFixture.Plan(Repeat(100000, "{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1}", "", 3)));

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("step timeout of 3 s", result.Reason);
            StringAssert.Contains("round", result.Reason);
        }

        [Test]
        public void InnerStepCleansUpWhenTheRepeatStepTimesOut()
        {
            var cleaned = 0;
            _f.Registry.Register("test.cleanup", context => Cleaning(() => cleaned++));

            _f.Run(HarnessFixture.Plan(Repeat(5, "{\"type\":\"test.cleanup\",\"timeoutSec\":50}", "", 2)));

            Assert.AreEqual("TIMEOUT", _f.ReadResult().Status);
            Assert.AreEqual(1, cleaned, "the inner step's finally block ran when the outer step was cut off");
        }

        [Test]
        public void InnerStepCleansUpWhenThePlanBudgetRunsOut()
        {
            var cleaned = 0;
            _f.Registry.Register("test.cleanup", context => Cleaning(() => cleaned++));

            _f.Run(HarnessFixture.Plan(Repeat(5, "{\"type\":\"test.cleanup\",\"timeoutSec\":50}", "", 50), "", "functional", 2));

            Assert.AreEqual("TIMEOUT", _f.ReadResult().Status);
            Assert.AreEqual(1, cleaned);
        }

        static IEnumerator Cleaning(System.Action cleanup)
        {
            try
            {
                while (true)
                    yield return null;
            }
            finally
            {
                cleanup();
            }
        }

        [Test]
        public void RoundMetricGetsTheDurationOfEveryRoundAlsoAFailedOne()
        {
            _failOnCall = 2;

            _f.Run(HarnessFixture.Plan(Repeat(3, CallStep, ",\"roundMetric\":\"roundMs\",\"continueOnFail\":true")));

            Assert.AreEqual(3, _f.ReadResult().Metrics.Single(m => m.Name == "roundMs").Samples.Count);
        }

        [Test]
        public void RoundMetricGetsTheDurationOfEveryRound()
        {
            _f.Run(HarnessFixture.Plan(Repeat(3, "{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.5}", ",\"roundMetric\":\"roundMs\"")));

            var samples = _f.ReadResult().Metrics.Single(m => m.Name == "roundMs").Samples;
            Assert.AreEqual(3, samples.Count);
            foreach (var sample in samples)
                Assert.That(sample, Is.InRange(500, 600));
        }

        [Test]
        public void PrefixNamesTheSummaryMetrics()
        {
            _f.Run(HarnessFixture.Plan(Repeat(2, CallStep, ",\"prefix\":\"arm\"")));

            var names = _f.ReadResult().Metrics.Select(m => m.Name).ToArray();
            CollectionAssert.Contains(names, "arm.rounds");
            CollectionAssert.Contains(names, "arm.failedRounds");
        }

        [Test]
        public void WarmupIterationsRunTheRoundsButAddNoSamples()
        {
            _f.Run(HarnessFixture.Plan(Repeat(2, CallStep), ",\"repeat\":2,\"warmup\":1"));

            var result = _f.ReadResult();
            Assert.AreEqual(6, _calls);
            Assert.AreEqual(4, result.Metrics.Single(m => m.Name == "call.ms").Samples.Count);
            Assert.AreEqual(2, result.Metrics.Single(m => m.Name == "repeat.rounds").Samples.Count);
        }

        [Test]
        public void InnerStepSeesTheRoundAndTheIterationOfTheOuterStep()
        {
            var rounds = new System.Collections.Generic.List<long>();
            _f.Registry.Register("test.round", context =>
            {
                rounds.Add(context.Round);
                return new object[0].GetEnumerator();
            });

            _f.Run(HarnessFixture.Plan(Repeat(3, "{\"type\":\"test.round\",\"timeoutSec\":5}")));

            CollectionAssert.AreEqual(new long[] { 1, 2, 3 }, rounds);
        }

        [Test]
        public void ThePlanContractRejectsABadRepeatBeforeAnythingRuns()
        {
            foreach (var step in new[]
            {
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"steps\":[" + CallStep + "]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":0,\"steps\":[" + CallStep + "]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":1.5,\"steps\":[" + CallStep + "]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":2}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":2,\"steps\":[]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":2,\"steps\":[{\"type\":\"test.call\"}]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":2,\"steps\":[{\"timeoutSec\":5}]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":2,\"steps\":[5]}",
                "{\"type\":\"repeat\",\"timeoutSec\":5,\"count\":2,\"steps\":[" + Repeat(2, CallStep) + "]}",
            })
            {
                Assert.Throws<ContractException>(() => HarnessFixture.Plan(step), step);
            }
        }

        [Test]
        public void ARunnerHandedABadRepeatAnyWayFailsTheStepWithoutRunningIt()
        {
            foreach (var mutate in new System.Action<PlanStep>[]
            {
                step => step.Args.Set("count", Valtrion.Unity.Json.JsonValue.From(0)),
                step => step.Args.Set("steps", Valtrion.Unity.Json.JsonValue.NewArray()),
                step => step.Args.Set("count", Valtrion.Unity.Json.JsonValue.From("3")),
            })
            {
                using (var fixture = new HarnessFixture())
                {
                    var calls = 0;
                    fixture.Registry.Register("test.call", context =>
                    {
                        calls++;
                        return new object[0].GetEnumerator();
                    });
                    var plan = HarnessFixture.Plan(Repeat(2, CallStep));
                    mutate(plan.Steps[0]);

                    fixture.Run(plan);

                    var result = fixture.ReadResult();
                    Assert.AreEqual("FAIL", result.Status);
                    StringAssert.Contains("invalid arguments", result.Reason);
                    Assert.AreEqual(0, calls);
                }
            }
        }

        [Test]
        public void ProbeWindowOpenedBeforeTheRepeatStaysOpenAcrossRounds()
        {
            _f.Run(HarnessFixture.Plan(
                "{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[{\"type\":\"log\",\"name\":\"T\",\"pattern\":\"tick\",\"metric\":\"ticks\"}]},"
                + Repeat(3, "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"tick\"}") + ",{\"type\":\"probe.stop\",\"timeoutSec\":5}"));

            Assert.AreEqual(3, _f.ReadResult().Metrics.Single(m => m.Name == "ticks").Samples.Count);
        }
    }
}
