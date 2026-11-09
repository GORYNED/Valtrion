using System.Collections;
using System.Linq;
using System.Threading.Tasks;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class CounterStepsTests
    {
        HarnessFixture _f;
        ThermalReading _thermal;

        [SetUp]
        public void SetUp()
        {
            _f = new HarnessFixture();
            _thermal = new ThermalReading(0, "nominal");
            ThermalReaders.Shared.Register("device", context => _thermal);
            _f.Registry.Register("test.feedFromThread", FeedFromThread);
            _f.Registry.Register("test.heat", context =>
            {
                _thermal = new ThermalReading(2, "serious");
                return new object[0].GetEnumerator();
            });
        }

        [TearDown]
        public void TearDown()
        {
            ThermalReaders.Shared.Unregister("device");
            _f.Dispose();
        }

        static IEnumerator FeedFromThread(StepContext context)
        {
            var line = context.String("line");
            Task.Run(() => context.Run.Logs.Feed(line)).Wait();
            yield break;
        }

        static string Start(string counters, string extra = "") => "{\"type\":\"counter.start\",\"timeoutSec\":5,\"name\":\"faults\",\"counters\":[" + counters + "]" + extra + "}";

        const string Stop = "{\"type\":\"counter.stop\",\"timeoutSec\":5,\"name\":\"faults\"}";

        static string Feed(string line) => "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"" + line + "\"}";

        static double Single(ResultDocument result, string name) => result.Metrics.Single(m => m.Name == name).Samples.Single();

        [Test]
        public void CountsOnlyTheMatchingLinesBetweenStartAndStop()
        {
            var plan = HarnessFixture.Plan(Feed("dropped before") + "," + Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}") + "," + Feed("dropped 1") + "," + Feed("other") + "," + Feed("dropped 2") + "," + Stop + "," + Feed("dropped after"));

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(2, Single(result, "dropped"));
            Assert.AreEqual("count", result.Metrics.Single(m => m.Name == "dropped").Unit);
        }

        [Test]
        public void ACountOfNothingIsARealZero()
        {
            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}") + "," + Feed("other") + "," + Stop));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(0, Single(result, "dropped"));
        }

        [Test]
        public void ReadsTheNumberInTheLineWithSumLastMaxAndIncrease()
        {
            const string pattern = "outOfBuffers=(?<value>\\\\d+)";
            var counters = "{\"metric\":\"sum\",\"pattern\":\"" + pattern + "\",\"mode\":\"sum\"},"
                + "{\"metric\":\"last\",\"pattern\":\"" + pattern + "\",\"mode\":\"last\"},"
                + "{\"metric\":\"max\",\"pattern\":\"" + pattern + "\",\"mode\":\"max\"},"
                + "{\"metric\":\"increase\",\"pattern\":\"" + pattern + "\",\"mode\":\"increase\",\"unit\":\"events\"}";
            var plan = HarnessFixture.Plan(Start(counters) + "," + Feed("stats outOfBuffers=3") + "," + Feed("stats outOfBuffers=9") + "," + Feed("stats outOfBuffers=5") + "," + Stop);

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(17, Single(result, "sum"));
            Assert.AreEqual(5, Single(result, "last"));
            Assert.AreEqual(9, Single(result, "max"));
            Assert.AreEqual(2, Single(result, "increase"));
            Assert.AreEqual("events", result.Metrics.Single(m => m.Name == "increase").Unit);
        }

        [Test]
        public void ModesThatNeedANumberReportNothingWhenNoLineHadOne()
        {
            var plan = HarnessFixture.Plan(Start("{\"metric\":\"last\",\"pattern\":\"stats(?<value>\\\\d+)?\",\"mode\":\"last\"}") + "," + Feed("stats") + "," + Stop);

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "last"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("'last'")));
        }

        [Test]
        public void IncreaseNeedsTwoReadingsAndOneIsUnknownNotZero()
        {
            var counter = "{\"metric\":\"inc\",\"pattern\":\"n=(?<value>\\\\d+)\",\"mode\":\"increase\"}";

            _f.Run(HarnessFixture.Plan(Start(counter) + "," + Feed("n=4") + "," + Stop));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "inc"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("needs two readings")));
        }

        [Test]
        public void ARunningCounterThatWentDownIsListedAsUnverified()
        {
            var counter = "{\"metric\":\"inc\",\"pattern\":\"n=(?<value>\\\\d+)\",\"mode\":\"increase\"}";

            _f.Run(HarnessFixture.Plan(Start(counter) + "," + Feed("n=9") + "," + Feed("n=2") + "," + Stop));

            var result = _f.ReadResult();
            Assert.AreEqual(-7, Single(result, "inc"), "the value is reported as it is, not clamped");
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("went down")));
        }

        [Test]
        public void LineThatMatchesWithoutAReadableNumberIsListedAndTheRestAreCounted()
        {
            var counter = "{\"metric\":\"sum\",\"pattern\":\"n=(?<value>\\\\w+)\",\"mode\":\"sum\"}";

            _f.Run(HarnessFixture.Plan(Start(counter) + "," + Feed("n=4") + "," + Feed("n=x") + "," + Feed("n=6") + "," + Stop));

            var result = _f.ReadResult();
            Assert.AreEqual(10, Single(result, "sum"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("1 matching line(s) had no readable number")));
        }

        [Test]
        public void PatternThatBacktracksWithoutEndDoesNotStallTheLoggerAndIsListed()
        {
            var counter = "{\"metric\":\"bad\",\"pattern\":\"^(a+)+$\"},{\"metric\":\"good\",\"pattern\":\"b\"}";
            var slow = new string('a', 60) + "!";
            var watch = System.Diagnostics.Stopwatch.StartNew();

            _f.Run(HarnessFixture.Plan(Start(counter) + "," + Feed(slow) + "," + Feed("b") + "," + Stop));

            Assert.Less(watch.ElapsedMilliseconds, 10000, "the match is bounded by a timeout");
            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(1, Single(result, "good"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("took more than")));
        }

        [Test]
        public void ThermalLevelThatIsNotANumberIsListed()
        {
            _thermal = new ThermalReading(double.NaN, "odd");

            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}", ",\"thermal\":\"device\"") + "," + Stop));

            var result = _f.ReadResult();
            Assert.IsFalse(result.Metrics.Any(m => m.Name.Contains("thermal")));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("not a finite number")));
        }

        [Test]
        public void ThermalStateUnknownAtTheStartButKnownAtTheEndIsReportedHonestly()
        {
            _thermal = null;
            _f.Registry.Register("test.cool", context =>
            {
                _thermal = new ThermalReading(1, "fair");
                return new object[0].GetEnumerator();
            });

            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}", ",\"thermal\":\"device\"") + ",{\"type\":\"test.cool\",\"timeoutSec\":5}," + Stop));

            var result = _f.ReadResult();
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "faults.thermalStartLevel"));
            Assert.AreEqual(1, Single(result, "faults.thermalEndLevel"));
            StringAssert.Contains("unknown -> fair", result.Steps.Last().Message);
        }

        [Test]
        public void ThermalReaderUnregisteredBetweenStartAndStopIsBlocked()
        {
            _f.Registry.Register("test.unregister", context =>
            {
                ThermalReaders.Shared.Unregister("device");
                return new object[0].GetEnumerator();
            });

            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}", ",\"thermal\":\"device\"") + ",{\"type\":\"test.unregister\",\"timeoutSec\":5}," + Stop));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void ThermalStateIsReportedNextToTheNumbers()
        {
            var plan = HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}", ",\"thermal\":\"device\"") + ",{\"type\":\"test.heat\",\"timeoutSec\":5}," + Feed("dropped") + "," + Stop);

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(0, Single(result, "faults.thermalStartLevel"));
            Assert.AreEqual(2, Single(result, "faults.thermalEndLevel"));
            Assert.AreEqual(1, Single(result, "dropped"));
            StringAssert.Contains("nominal -> serious", result.Steps.Last().Message);
        }

        [Test]
        public void UnknownThermalStateIsNotZero()
        {
            _thermal = null;

            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}", ",\"thermal\":\"device\"") + "," + Stop));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsFalse(result.Metrics.Any(m => m.Name.Contains("thermal")));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("thermal state") && u.Contains("unknown")));
            StringAssert.Contains("unknown -> unknown", result.Steps.Last().Message);
        }

        [Test]
        public void ThermalReaderThatIsNotRegisteredIsBlocked()
        {
            ThermalReaders.Shared.Unregister("device");

            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}", ",\"thermal\":\"device\"")));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void LinesLoggedFromAnotherThreadAreCounted()
        {
            var plan = HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}") + ",{\"type\":\"test.feedFromThread\",\"timeoutSec\":5,\"line\":\"dropped\"},{\"type\":\"test.feedFromThread\",\"timeoutSec\":5,\"line\":\"dropped\"}," + Stop);

            _f.Run(plan);

            Assert.AreEqual(2, Single(_f.ReadResult(), "dropped"));
        }

        [Test]
        public void EachIterationReportsItsOwnWindowAndWarmupReportsNone()
        {
            var plan = HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}") + "," + Feed("dropped") + "," + Stop, ",\"repeat\":3,\"warmup\":1");

            _f.Run(plan);

            var samples = _f.ReadResult().Metrics.Single(m => m.Name == "dropped").Samples;
            Assert.AreEqual(new[] { 1.0, 1.0, 1.0 }, samples.ToArray());
        }

        [Test]
        public void ARunWideWindowGivesOneNumberPerIteration()
        {
            var steps = Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}") + "," + Feed("dropped") + "," + Feed("dropped") + "," + Stop;

            _f.Run(HarnessFixture.Plan(steps, ",\"repeat\":2"));

            Assert.AreEqual(new[] { 2.0, 2.0 }, _f.ReadResult().Metrics.Single(m => m.Name == "dropped").Samples.ToArray());
        }

        [Test]
        public void StopWithoutStartFails()
        {
            _f.Run(HarnessFixture.Plan(Stop));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("counter.start", result.Reason);
        }

        [Test]
        public void StartTwiceInOneIterationFails()
        {
            var one = "{\"metric\":\"dropped\",\"pattern\":\"dropped\"}";

            _f.Run(HarnessFixture.Plan(Start(one) + "," + Start(one)));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void StopOfAWindowOpenedInAnotherIterationFails()
        {
            var plan = HarnessFixture.Plan(Stop, ",\"setup\":[" + Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}") + "]");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("another iteration", result.Reason);
            Assert.IsEmpty(result.Metrics);
        }

        [Test]
        public void WindowOfAnEarlierIterationIsReplacedWithANote()
        {
            _f.Run(HarnessFixture.Plan(Start("{\"metric\":\"dropped\",\"pattern\":\"dropped\"}"), ",\"repeat\":2"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("earlier iteration")));
        }

        [Test]
        public void BadArgumentsFailTheStep()
        {
            foreach (var counters in new[]
            {
                "",
                "{\"metric\":\"a\"}",
                "{\"pattern\":\"a\"}",
                "{\"metric\":\"a\",\"pattern\":\"(\"}",
                "{\"metric\":\"a\",\"pattern\":\"a\",\"mode\":\"median\"}",
                "{\"metric\":\"a\",\"pattern\":\"a\",\"mode\":\"sum\"}",
                "{\"metric\":\"a\",\"pattern\":\"a\"},{\"metric\":\"a\",\"pattern\":\"b\"}",
            })
            {
                using (var fixture = new HarnessFixture())
                {
                    fixture.Run(HarnessFixture.Plan(Start(counters)));

                    Assert.AreEqual("FAIL", fixture.ReadResult().Status, counters);
                }
            }
        }
    }
}
