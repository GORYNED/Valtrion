using System.Linq;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class SessionStepsTests
    {
        const string Step = "{\"type\":\"session.offsets\",\"timeoutSec\":5,\"reader\":\"test\"";

        HarnessFixture _f;

        [SetUp]
        public void SetUp() => _f = new HarnessFixture();

        [TearDown]
        public void TearDown()
        {
            SessionReaders.Shared.Unregister("test");
            _f.Dispose();
        }

        static void Register(SessionOffsets offsets) => SessionReaders.Shared.Register("test", context => offsets);

        static double Single(Valtrion.Unity.Contracts.ResultDocument result, string name) => result.Metrics.Single(m => m.Name == name).Samples.Single();

        [Test]
        public void ReportsOffsetsAndFirstTimestampsOfEveryStream()
        {
            var offsets = new SessionOffsets { AnchorMs = 1000 };
            offsets.Streams.Add(new StreamOffset("video", 12.5, 1040));
            offsets.Streams.Add(new StreamOffset("pressure", -3, 1010));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + ",\"prefix\":\"s\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(12.5, Single(result, "s.video.offsetMs"));
            Assert.AreEqual(1040, Single(result, "s.video.firstTimestampMs"));
            Assert.AreEqual(40, Single(result, "s.video.firstTimestampFromAnchorMs"));
            Assert.AreEqual(-3, Single(result, "s.pressure.offsetMs"));
            Assert.AreEqual(10, Single(result, "s.pressure.firstTimestampFromAnchorMs"));
            Assert.AreEqual(1000, Single(result, "s.anchorMs"));
            Assert.IsFalse(result.Metrics.Any(m => m.Name.Contains("offsetFromStream")), "no relativeTo, no difference");
        }

        [Test]
        public void StreamWithoutFirstTimestampOrAnchorReportsOnlyItsOffset()
        {
            var offsets = new SessionOffsets();
            offsets.Streams.Add(new StreamOffset("video", 7));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + "}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(new[] { "session.video.offsetMs" }, result.Metrics.Select(m => m.Name).ToArray());
        }

        [Test]
        public void RelativeToReportsTheDifferenceOfTheOtherStreams()
        {
            var offsets = new SessionOffsets();
            offsets.Streams.Add(new StreamOffset("video", 12));
            offsets.Streams.Add(new StreamOffset("pressure", 2));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + ",\"relativeTo\":\"pressure\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(10, Single(result, "session.video.offsetFromStreamMs"));
            Assert.IsFalse(result.Metrics.Any(m => m.Name == "session.pressure.offsetFromStreamMs"));
        }

        [Test]
        public void RelativeToAStreamThatIsMissingFailsAndReportsNothing()
        {
            var offsets = new SessionOffsets();
            offsets.Streams.Add(new StreamOffset("video", 12));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + ",\"relativeTo\":\"audio\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("audio", result.Reason);
            Assert.IsEmpty(result.Metrics);
        }

        [Test]
        public void ReaderThatIsNotRegisteredIsBlocked()
        {
            _f.Run(HarnessFixture.Plan(Step + "}"));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            StringAssert.Contains("'test'", result.Reason);
        }

        [Test]
        public void SessionWithoutStreamsFails()
        {
            Register(new SessionOffsets());

            _f.Run(HarnessFixture.Plan(Step + "}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void ReaderReturningNullFails()
        {
            SessionReaders.Shared.Register("test", context => null);

            _f.Run(HarnessFixture.Plan(Step + "}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void NonFiniteValueFailsAndReportsNothing()
        {
            var offsets = new SessionOffsets();
            offsets.Streams.Add(new StreamOffset("video", 1, 2));
            offsets.Streams.Add(new StreamOffset("pressure", double.NaN));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + "}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            Assert.IsEmpty(result.Metrics);
        }

        [Test]
        public void StreamNamesThatCollideAsMetricNamesFail()
        {
            var offsets = new SessionOffsets();
            offsets.Streams.Add(new StreamOffset("a b", 1));
            offsets.Streams.Add(new StreamOffset("a-b", 2));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + "}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void ReaderThatThrowsFailsTheStepWithItsMessage()
        {
            SessionReaders.Shared.Register("test", context => throw new System.IO.InvalidDataException("bad archive"));

            _f.Run(HarnessFixture.Plan(Step + "}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("bad archive", result.Reason);
        }

        [Test]
        public void EachIterationAddsASample()
        {
            var offsets = new SessionOffsets();
            offsets.Streams.Add(new StreamOffset("video", 5));
            Register(offsets);

            _f.Run(HarnessFixture.Plan(Step + "}", ",\"repeat\":3"));

            Assert.AreEqual(3, _f.ReadResult().Metrics.Single(m => m.Name == "session.video.offsetMs").Samples.Count);
        }
    }
}
