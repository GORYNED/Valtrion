using System.IO;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class BackgroundStepTests
    {
        HarnessFixture _f;

        [SetUp]
        public void SetUp()
        {
            _f = new HarnessFixture();
            _f.Options.HeartbeatIntervalMs = 0;
        }

        [TearDown]
        public void TearDown() => _f.Dispose();

        static string Step(string extra = "", int timeout = 60) => "{\"type\":\"app.background\",\"timeoutSec\":" + timeout + ",\"seconds\":5" + extra + "}";

        /// <summary>Runs the plan and answers the first host request with the given reply, after <paramref name="awayMs"/> of run clock. Returns the request.</summary>
        HostRequest RunWithHost(PlanDocument plan, string reply, double awayMs = 0)
        {
            HostRequest seen = null;
            double? answerAt = null;
            var runner = new PlanRunner(plan, plan.ToJsonString(), _f.Options);
            var routine = runner.Execute();
            for (var count = 0; routine.MoveNext(); count++)
            {
                _f.Clock.Advance(16);
                var path = Path.Combine(_f.RunDirectory, "heartbeat.json");
                if (seen == null && File.Exists(path))
                {
                    var heartbeat = HeartbeatDocument.Parse(File.ReadAllText(path));
                    if (heartbeat.Request != null)
                    {
                        seen = heartbeat.Request;
                        answerAt = _f.Clock.NowMs + awayMs;
                    }
                }
                if (seen != null && answerAt.HasValue && _f.Clock.NowMs >= answerAt.Value && reply != null)
                {
                    Directory.CreateDirectory(_f.Inbox);
                    File.WriteAllText(Path.Combine(_f.Inbox, "reply-" + seen.Id + ".json"), reply);
                    answerAt = null;
                }
                Assert.Less(count, 100000);
            }
            return seen;
        }

        [Test]
        public void PutsTheRequestForTheHostIntoTheHeartbeatAndPassesWhenTheAppIsBack()
        {
            var request = RunWithHost(HarnessFixture.Plan(Step(",\"metric\":\"backgroundMs\"")), "{\"status\":\"PASS\",\"message\":\"foreground again\"}", 2000);

            Assert.IsNotNull(request);
            Assert.AreEqual("app.background", request.Action);
            Assert.IsTrue(request.Args.TryGet("seconds", out var seconds));
            Assert.AreEqual(5, seconds.AsNumber);
            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            StringAssert.Contains("came back", result.Steps[0].Message);
            var metric = result.Metrics.Single(m => m.Name == "backgroundMs");
            Assert.AreEqual("ms", metric.Unit);
            Assert.That(metric.Samples.Single(), Is.InRange(2000, 2200));
        }

        [Test]
        public void ReplyThatIsNotPassEndsTheStepWithThatStatusAndReportsNoTime()
        {
            RunWithHost(HarnessFixture.Plan(Step(",\"metric\":\"backgroundMs\"")), "{\"status\":\"FAIL\",\"message\":\"the app did not return\"}");

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("the app did not return", result.Reason);
            Assert.IsEmpty(result.Metrics);
            Assert.IsNull(HeartbeatDocument.Parse(File.ReadAllText(Path.Combine(_f.RunDirectory, "heartbeat.json"))).Request, "the request left the heartbeat");
        }

        [Test]
        public void NeedsPermissionOfTheHostIsKept()
        {
            RunWithHost(HarnessFixture.Plan(Step()), "{\"status\":\"NEEDS_PERMISSION\",\"message\":\"unlock the device\"}");

            Assert.AreEqual("NEEDS_PERMISSION", _f.ReadResult().Status);
        }

        [Test]
        public void NoReplyEndsAsTimeoutWhichCoversTheTimeInTheBackground()
        {
            RunWithHost(HarnessFixture.Plan(Step("", 3)), null);

            var result = _f.ReadResult();
            Assert.AreEqual("TIMEOUT", result.Status);
            StringAssert.Contains("step timeout of 3 s", result.Reason);
            Assert.IsNull(HeartbeatDocument.Parse(File.ReadAllText(Path.Combine(_f.RunDirectory, "heartbeat.json"))).Request, "the request left the heartbeat");
        }

        [Test]
        public void IsRefusedInsideAnOpenProbeWindow()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"probe.start\",\"timeoutSec\":5,\"probes\":[\"timer:t\"]}," + Step()));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("probe window", result.Reason);
        }

        [Test]
        public void SecondsMustBeGreaterThanZero()
        {
            foreach (var seconds in new[] { "0", "-1" })
            {
                using (var fixture = new HarnessFixture())
                {
                    fixture.Run(HarnessFixture.Plan("{\"type\":\"app.background\",\"timeoutSec\":5,\"seconds\":" + seconds + "}"));

                    var result = fixture.ReadResult();
                    Assert.AreEqual("FAIL", result.Status, seconds);
                    StringAssert.Contains("invalid arguments", result.Reason, seconds);
                }
            }
        }

        [Test]
        public void SecondsAreRequired()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"app.background\",\"timeoutSec\":5}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
        }

        [Test]
        public void InsideARepeatEveryRoundAsksTheHostAgain()
        {
            var requests = 0;
            var plan = HarnessFixture.Plan("{\"type\":\"repeat\",\"timeoutSec\":120,\"count\":3,\"steps\":[" + Step(",\"metric\":\"away\"") + "]}");
            var runner = new PlanRunner(plan, plan.ToJsonString(), _f.Options);
            var routine = runner.Execute();
            string answered = null;
            for (var count = 0; routine.MoveNext(); count++)
            {
                _f.Clock.Advance(16);
                var path = Path.Combine(_f.RunDirectory, "heartbeat.json");
                if (File.Exists(path))
                {
                    var request = HeartbeatDocument.Parse(File.ReadAllText(path)).Request;
                    if (request != null && request.Id != answered)
                    {
                        answered = request.Id;
                        requests++;
                        Directory.CreateDirectory(_f.Inbox);
                        File.WriteAllText(Path.Combine(_f.Inbox, "reply-" + request.Id + ".json"), "{\"status\":\"PASS\"}");
                    }
                }
                Assert.Less(count, 100000);
            }

            Assert.AreEqual(3, requests);
            Assert.AreEqual("PASS", _f.ReadResult().Status);
            Assert.AreEqual(3, _f.ReadResult().Metrics.Single(m => m.Name == "away").Samples.Count);
        }
    }
}
