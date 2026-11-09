using System.Collections.Generic;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class StorageStepsTests
    {
        const string Begin = "{\"type\":\"storage.begin\",\"timeoutSec\":5,\"reader\":\"free\"";
        const string End = "{\"type\":\"storage.end\",\"timeoutSec\":5,\"reader\":\"free\"";

        HarnessFixture _f;
        readonly Queue<long> _reads = new Queue<long>();

        [SetUp]
        public void SetUp()
        {
            _f = new HarnessFixture();
            _reads.Clear();
            StorageReaders.Shared.Register("free", context => _reads.Dequeue());
        }

        [TearDown]
        public void TearDown()
        {
            StorageReaders.Shared.Unregister("free");
            _f.Dispose();
        }

        static double Single(ResultDocument result, string name) => result.Metrics.Single(m => m.Name == name).Samples.Single();

        [Test]
        public void ReportsFreeSpaceBeforeAfterUsedAndBytesPerSecond()
        {
            _reads.Enqueue(1000000);
            _reads.Enqueue(400000);

            _f.Run(HarnessFixture.Plan(Begin + "}," + End + ",\"seconds\":10}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(1000000, Single(result, "storage.freeBeforeBytes"));
            Assert.AreEqual(400000, Single(result, "storage.freeAfterBytes"));
            Assert.AreEqual(600000, Single(result, "storage.usedBytes"));
            Assert.AreEqual(60000, Single(result, "storage.bytesPerSecond"));
        }

        [Test]
        public void WithoutSecondsTheTimeBetweenTheStepsIsUsed()
        {
            _reads.Enqueue(2000);
            _reads.Enqueue(1000);

            _f.Run(HarnessFixture.Plan(Begin + "},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":2}," + End + "}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(1000.0 / 2.0, Single(result, "storage.bytesPerSecond"), 25.0, "about two seconds passed on the manual clock");
        }

        [Test]
        public void UnknownFreeSpaceIsNotZeroAndIsListedAsUnverified()
        {
            _reads.Enqueue(-1);
            _reads.Enqueue(500);

            _f.Run(HarnessFixture.Plan(Begin + "}," + End + ",\"seconds\":1}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "storage.freeBeforeBytes" || m.Name == "storage.usedBytes" || m.Name == "storage.bytesPerSecond"));
            Assert.AreEqual(500, Single(result, "storage.freeAfterBytes"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("unknown")));
        }

        [Test]
        public void UnknownAfterIsNotZeroEither()
        {
            _reads.Enqueue(500);
            _reads.Enqueue(-1);

            _f.Run(HarnessFixture.Plan(Begin + "}," + End + ",\"seconds\":1}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(500, Single(result, "storage.freeBeforeBytes"));
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "storage.freeAfterBytes" || m.Name == "storage.usedBytes"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("unknown")));
        }

        [Test]
        public void RequiredMakesUnknownBlocked()
        {
            _reads.Enqueue(-1);

            _f.Run(HarnessFixture.Plan(Begin + ",\"required\":true}"));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            StringAssert.Contains("unknown", result.Reason);
        }

        [Test]
        public void ReaderThatIsNotRegisteredIsBlocked()
        {
            StorageReaders.Shared.Unregister("free");

            _f.Run(HarnessFixture.Plan(Begin + "}"));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void EndWithoutBeginFails()
        {
            _reads.Enqueue(1);

            _f.Run(HarnessFixture.Plan(End + "}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("storage.begin", result.Reason);
        }

        [Test]
        public void BeginTwiceInOneIterationFails()
        {
            _reads.Enqueue(1);
            _reads.Enqueue(1);

            _f.Run(HarnessFixture.Plan(Begin + "}," + Begin + "}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void SecondsMustBePositive()
        {
            _reads.Enqueue(1);
            _reads.Enqueue(1);

            _f.Run(HarnessFixture.Plan(Begin + "}," + End + ",\"seconds\":0}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void SpaceThatCameBackGivesANegativeUsedValue()
        {
            _reads.Enqueue(100);
            _reads.Enqueue(300);

            _f.Run(HarnessFixture.Plan(Begin + "}," + End + ",\"seconds\":2}"));

            var result = _f.ReadResult();
            Assert.AreEqual(-200, Single(result, "storage.usedBytes"));
            Assert.AreEqual(-100, Single(result, "storage.bytesPerSecond"));
        }

        [Test]
        public void EachIterationOpensItsOwnWindow()
        {
            for (var i = 0; i < 3; i++)
            {
                _reads.Enqueue(1000);
                _reads.Enqueue(900);
            }

            _f.Run(HarnessFixture.Plan(Begin + "}," + End + ",\"seconds\":1}", ",\"repeat\":3"));

            Assert.AreEqual(3, _f.ReadResult().Metrics.Single(m => m.Name == "storage.usedBytes").Samples.Count);
        }

        [Test]
        public void TimerReportsTheTimeUntilAConditionIsMet()
        {
            var ready = false;
            _f.Options.Conditions.Register("file.ready", () => ready);
            var plan = HarnessFixture.Plan("{\"type\":\"timer.start\",\"timeoutSec\":5,\"name\":\"stop\"},{\"type\":\"timer.stop\",\"timeoutSec\":30,\"name\":\"stop\",\"metric\":\"finishMs\",\"until\":\"file.ready\"}");

            _f.Run(plan, () =>
            {
                if (_f.Clock.NowMs > 1000)
                    ready = true;
            });

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.That(Single(result, "finishMs"), Is.InRange(1000, 1100));
            Assert.AreEqual("ms", result.Metrics.Single(m => m.Name == "finishMs").Unit);
        }

        [Test]
        public void TimerWithoutConditionReportsTheTimeSinceStart()
        {
            var plan = HarnessFixture.Plan("{\"type\":\"timer.start\",\"timeoutSec\":5,\"name\":\"t\"},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1},{\"type\":\"timer.stop\",\"timeoutSec\":5,\"name\":\"t\",\"metric\":\"waited\"}");

            _f.Run(plan);

            Assert.That(Single(_f.ReadResult(), "waited"), Is.InRange(1000, 1100));
        }

        [Test]
        public void TimerThatWasNotStartedFails()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"timer.stop\",\"timeoutSec\":5,\"name\":\"t\",\"metric\":\"m\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("timer.start", result.Reason);
        }

        [Test]
        public void ConditionThatIsNotRegisteredIsBlocked()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"timer.start\",\"timeoutSec\":5,\"name\":\"t\"},{\"type\":\"timer.stop\",\"timeoutSec\":5,\"name\":\"t\",\"metric\":\"m\",\"until\":\"nope\"}"));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void LogSpanMeasuresTheTimeBetweenTwoLogLinesInsideOneStep()
        {
            var plan = HarnessFixture.Plan(
                "{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[{\"type\":\"logSpan\",\"start\":\"stop requested\",\"end\":\"file ready\",\"metric\":\"finishMs\"}]},"
                + "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"stop requested\"},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1},{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"file ready\"},"
                + "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"file ready\"},{\"type\":\"probe.stop\",\"timeoutSec\":5}");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            var metric = result.Metrics.Single(m => m.Name == "finishMs");
            Assert.AreEqual("ms", metric.Unit);
            Assert.AreEqual(1, metric.Samples.Count, "an end line without a start line gives no sample");
            Assert.That(metric.Samples[0], Is.InRange(1000, 1100));
        }

        [Test]
        public void LogSpanWithoutAnEndLineGivesNoSample()
        {
            var plan = HarnessFixture.Plan(
                "{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[{\"type\":\"logSpan\",\"start\":\"a\",\"end\":\"b\",\"metric\":\"m\"}]},"
                + "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"a\"},{\"type\":\"probe.stop\",\"timeoutSec\":5}");

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "m"));
        }

        [Test]
        public void LogSpanRestartsAtTheLatestStartLine()
        {
            var plan = HarnessFixture.Plan(
                "{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[{\"type\":\"logSpan\",\"start\":\"a\",\"end\":\"b\",\"metric\":\"m\"}]},"
                + "{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"a\"},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":1},{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"a\"},"
                + "{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.5},{\"type\":\"test.feed\",\"timeoutSec\":5,\"line\":\"b\"},{\"type\":\"probe.stop\",\"timeoutSec\":5}");

            _f.Run(plan);

            Assert.That(_f.ReadResult().Metrics.Single(m => m.Name == "m").Samples.Single(), Is.InRange(500, 600));
        }

        [Test]
        public void LogSpanWithABadPatternFailsTheStep()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[{\"type\":\"logSpan\",\"start\":\"(\",\"end\":\"b\",\"metric\":\"m\"}]}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("start", result.Reason);
        }

        [Test]
        public void ConditionThatNeverComesTrueTimesOutAndReportsNothing()
        {
            _f.Options.Conditions.Register("never", () => false);

            _f.Run(HarnessFixture.Plan("{\"type\":\"timer.start\",\"timeoutSec\":5,\"name\":\"t\"},{\"type\":\"timer.stop\",\"timeoutSec\":1,\"name\":\"t\",\"metric\":\"m\",\"until\":\"never\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            Assert.IsEmpty(result.Metrics);
        }
    }
}
