using System.IO;
using System.Linq;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    public sealed class FillStepsTests
    {
        const long MiB = 1024 * 1024;

        HarnessFixture _f;
        long _capacity;
        long _extraFree;
        long _takenBySystem;
        long _unknown;
        bool _throwOnRead;

        string FillFolder => Path.Combine(_f.Root, "gdv", "work", "fill");

        string[] FillFiles() => Directory.Exists(FillFolder) ? Directory.GetFiles(FillFolder) : new string[0];

        long Used() => FillFiles().Sum(file => new FileInfo(file).Length);

        [SetUp]
        public void SetUp()
        {
            _f = new HarnessFixture();
            _capacity = 100 * MiB;
            _extraFree = 0;
            _takenBySystem = 0;
            _unknown = 0;
            _throwOnRead = false;
            StorageReaders.Shared.Register("free", context =>
            {
                if (_throwOnRead)
                    throw new IOException("device went away");
                return _unknown != 0 ? -1 : _capacity - Used() + _extraFree - _takenBySystem;
            });
        }

        [TearDown]
        public void TearDown()
        {
            StorageReaders.Shared.Unregister("free");
            ProjectCalls.Shared.Unregister("read");
            _f.Dispose();
        }

        static string Fill(string arguments, int timeout = 60) => "{\"type\":\"storage.fill\",\"timeoutSec\":" + timeout + ",\"reader\":\"free\"," + arguments + ",\"chunkBytes\":" + MiB + "}";

        static double Single(ResultDocument result, string name) => result.Metrics.Single(m => m.Name == name).Samples.Single();

        [Test]
        public void FillsUntilTheRequestedFreeSpaceIsLeftAndRemovesTheFileAtTheEnd()
        {
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"leaveFreeBytes\":" + 30 * MiB + ",\"toleranceBytes\":" + MiB / 2));
            long during = -1;

            var runner = new PlanRunner(plan, null, _f.Options);
            _f.Pump(runner, () => during = System.Math.Max(during, Used()));

            var result = runner.Result;
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(70 * MiB, during, 1 * MiB, "about 70 MiB were occupied while the run was going");
            Assert.AreEqual(70 * MiB, Single(result, "fill.bytesWritten"), 1 * MiB);
            Assert.AreEqual(30 * MiB, Single(result, "fill.freeAfterBytes"), 1 * MiB);
            Assert.AreEqual(0, Used(), "the file is gone after the run");
            Assert.IsEmpty(FillFiles());
        }

        [Test]
        public void LeaveFreeModeNeedsNoReserveAndKeepsTheFloorTheToleranceAdmits()
        {
            var plan = HarnessFixture.Plan(Fill("\"leaveFreeBytes\":" + 30 * MiB + ",\"toleranceBytes\":" + MiB / 2));

            _f.Run(plan);

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(30 * MiB, Single(result, "fill.freeAfterBytes"), 1 * MiB);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void WithoutAReserveTheFloorIsTheTargetMinusTheTolerance()
        {
            _capacity = 60 * MiB;
            var plan = HarnessFixture.Plan(Fill("\"leaveFreeBytes\":" + 20 * MiB + ",\"toleranceBytes\":" + 2 * MiB));
            var runner = new PlanRunner(plan, null, _f.Options);
            var squeezed = false;

            _f.Pump(runner, () =>
            {
                // Just before the target the system takes 8 MiB: free space falls to 16 MiB, below the floor of 18 MiB.
                if (!squeezed && Used() >= 36 * MiB)
                {
                    squeezed = true;
                    _takenBySystem = 8 * MiB;
                }
            });

            var result = runner.Result;
            Assert.IsTrue(squeezed);
            Assert.AreEqual("PASS", result.Status, "the file was cut back until the free space was within the tolerance of the target again");
            Assert.AreEqual(20 * MiB, Single(result, "fill.freeAfterBytes"), 2 * MiB);
        }

        [Test]
        public void AnExplicitReserveIsUsedAsBefore()
        {
            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 25 * MiB + ",\"leaveFreeBytes\":" + 20 * MiB + ",\"toleranceBytes\":" + MiB)));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status, "leaveFreeBytes below an explicit reserveBytes is refused");
            StringAssert.Contains("invalid arguments", result.Reason);
        }

        [Test]
        public void BytesModeStillNeedsAReserve()
        {
            _f.Run(HarnessFixture.Plan(Fill("\"bytes\":" + MiB)));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("reserveBytes", result.Reason);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void BytesModeWritesExactlyThatManyBytes()
        {
            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 5 * MiB) + ",{\"type\":\"storage.release\",\"timeoutSec\":5}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(5 * MiB, Single(result, "fill.bytesWritten"));
            Assert.AreEqual(95 * MiB, Single(result, "fill.freeAfterBytes"), 64, "the 16-byte marker is on the disk too");
        }

        [Test]
        public void ReleaseRemovesTheFileAtOnceAndTheLedgerForgetsIt()
        {
            long afterRelease = -1;
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 3 * MiB) + ",{\"type\":\"storage.release\",\"timeoutSec\":5},{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.1}");

            var runner = new PlanRunner(plan, null, _f.Options);
            _f.Pump(runner, () =>
            {
                if (runner.Context.Ledger.Entries.Count == 0 && afterRelease < 0)
                    afterRelease = Used();
            });

            Assert.AreEqual("PASS", runner.Result.Status);
            Assert.AreEqual(0, afterRelease);
        }

        [Test]
        public void ReleaseWithoutAFillIsNotAnError()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"storage.release\",\"timeoutSec\":5}"));

            Assert.AreEqual("PASS", _f.ReadResult().Status);
        }

        [Test]
        public void TheFloorIsNeverCrossedInBytesMode()
        {
            _capacity = 20 * MiB;

            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 15 * MiB + ",\"bytes\":" + 10 * MiB)));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status, "the target would be below the floor, so nothing is written");
            Assert.AreEqual(0, Used());
            Assert.IsEmpty(FillFiles());
        }

        [Test]
        public void ASystemThatTakesSpaceMeanwhileStopsTheFillAtTheFloor()
        {
            _capacity = 30 * MiB;
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 15 * MiB));
            var runner = new PlanRunner(plan, null, _f.Options);

            _f.Pump(runner, () =>
            {
                if (Used() >= 5 * MiB)
                    _takenBySystem = 8 * MiB;
            });

            var result = runner.Result;
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("floor", result.Reason);
            Assert.AreEqual(0, Used(), "the file is removed after the failure");
        }

        [Test]
        public void SpaceThatTheSystemGaveBackIsFilledAgain()
        {
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"leaveFreeBytes\":" + 40 * MiB + ",\"toleranceBytes\":" + MiB / 2));
            var runner = new PlanRunner(plan, null, _f.Options);
            var given = false;

            _f.Pump(runner, () =>
            {
                if (!given && Used() >= 30 * MiB)
                {
                    given = true;
                    _extraFree = 12 * MiB;
                }
            });

            var result = runner.Result;
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(40 * MiB, Single(result, "fill.freeAfterBytes"), 1 * MiB);
            Assert.AreEqual(72 * MiB, Single(result, "fill.bytesWritten"), 1 * MiB, "12 MiB more were needed to keep the free space at the target");
        }

        [Test]
        public void SpaceTheSystemTookIsGivenBackByCuttingTheFile()
        {
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"leaveFreeBytes\":" + 40 * MiB + ",\"toleranceBytes\":" + MiB / 2));
            var runner = new PlanRunner(plan, null, _f.Options);
            var taken = false;

            _f.Pump(runner, () =>
            {
                if (!taken && Used() >= 59 * MiB)
                {
                    taken = true;
                    _takenBySystem = 6 * MiB;
                }
            });

            var result = runner.Result;
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(40 * MiB, Single(result, "fill.freeAfterBytes"), 1 * MiB);
            Assert.AreEqual(54 * MiB, Single(result, "fill.bytesWritten"), 1 * MiB);
        }

        [Test]
        public void UnknownFreeSpaceBlocksTheStepAndWritesNothing()
        {
            _unknown = 1;

            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + MiB)));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            StringAssert.Contains("unknown", result.Reason);
            Assert.AreEqual(0, Used());
            Assert.IsEmpty(FillFiles());
        }

        [Test]
        public void FreeSpaceThatIsAlreadyBelowTheTargetBlocksTheStep()
        {
            _capacity = 20 * MiB;

            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 5 * MiB + ",\"leaveFreeBytes\":" + 30 * MiB + ",\"toleranceBytes\":" + MiB / 2)));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void ReaderThatIsNotRegisteredBlocksTheStep()
        {
            StorageReaders.Shared.Unregister("free");

            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":1,\"bytes\":1")));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void ForeignFileInTheFillFolderBlocksTheStepAndIsNotTouched()
        {
            Directory.CreateDirectory(FillFolder);
            var foreign = Path.Combine(FillFolder, "photo.jpg");
            File.WriteAllText(foreign, "not mine");

            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + MiB)));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            StringAssert.Contains("did not create", result.Reason);
            Assert.AreEqual("not mine", File.ReadAllText(foreign));
        }

        [Test]
        public void FillFileOfAnEarlierLaunchIsRemovedAtStartAndListed()
        {
            Directory.CreateDirectory(FillFolder);
            var orphan = Path.Combine(FillFolder, "gdv-fill-killed-run.bin");
            File.WriteAllBytes(orphan, FillSteps.Marker().Concat(new byte[1000]).ToArray());
            var other = Path.Combine(FillFolder, "notes.txt");
            File.WriteAllText(other, "keep");

            _f.Run(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.1}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.IsFalse(File.Exists(orphan));
            Assert.IsTrue(File.Exists(other), "only the harness's own fill files are removed");
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("gdv-fill-killed-run.bin")));
        }

        [Test]
        public void FileIsRemovedWhenALaterStepFailsTheRun()
        {
            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 4 * MiB) + ",{\"type\":\"test.fail\",\"timeoutSec\":5}"));

            Assert.AreEqual("FAIL", _f.ReadResult().Status);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void FileIsRemovedWhenTheRunIsCancelledInTheMiddleOfTheFill()
        {
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 50 * MiB));
            var runner = new PlanRunner(plan, null, _f.Options);

            _f.Pump(runner, () =>
            {
                if (Used() >= 10 * MiB && !runner.Finished)
                    runner.Abort("BLOCKED", "connection lost");
            });

            Assert.AreEqual("BLOCKED", runner.Result.Status);
            Assert.AreEqual(0, Used());
            Assert.AreEqual(0, runner.Context.Ledger.Entries.Count);
        }

        [Test]
        public void FileIsRemovedWhenTheStepTimesOut()
        {
            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 90 * MiB, 1)));

            Assert.AreEqual("TIMEOUT", _f.ReadResult().Status);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void FileIsRemovedWhenTheReaderFailsInTheMiddle()
        {
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 30 * MiB));
            var runner = new PlanRunner(plan, null, _f.Options);

            _f.Pump(runner, () =>
            {
                if (Used() >= 5 * MiB)
                    _throwOnRead = true;
            });

            Assert.AreEqual("FAIL", runner.Result.Status);
            StringAssert.Contains("device went away", runner.Result.Reason);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void FileIsRemovedAlsoWhenThePlanSaysCleanupNone()
        {
            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 4 * MiB), ",\"cleanup\":\"none\""));

            Assert.AreEqual("PASS", _f.ReadResult().Status);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void ReaderThatDoesNotShowTheSpaceFallingStopsTheFill()
        {
            StorageReaders.Shared.Register("free", context => 100 * MiB);
            long most = 0;
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"leaveFreeBytes\":" + 20 * MiB + ",\"toleranceBytes\":" + MiB));
            var runner = new PlanRunner(plan, null, _f.Options);

            _f.Pump(runner, () => most = System.Math.Max(most, Used()));

            Assert.AreEqual("FAIL", runner.Result.Status);
            StringAssert.Contains("did not fall", runner.Result.Reason);
            Assert.LessOrEqual(most, 6 * MiB, "it stopped after a few chunks, not at the end of the disk");
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void BytesModeThatTheSystemSqueezedBelowTheFloorCutsTheFileBackAtOnce()
        {
            _capacity = 30 * MiB;
            var plan = HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 15 * MiB));
            var runner = new PlanRunner(plan, null, _f.Options);
            long freeAtTheEnd = -1;

            _f.Pump(runner, () =>
            {
                if (Used() >= 5 * MiB && _takenBySystem == 0)
                    _takenBySystem = 12 * MiB;
                if (!runner.Finished)
                    freeAtTheEnd = _capacity - Used() - _takenBySystem;
            });

            Assert.AreEqual("FAIL", runner.Result.Status);
            StringAssert.Contains("cut back", runner.Result.Reason);
            Assert.GreaterOrEqual(Single(runner.Result, "fill.freeAfterBytes"), 10 * MiB, "the free space is back at the floor");
        }

        [Test]
        public void SweepLeavesDirectoriesOtherNamesAndFilesWithoutTheMarkerAlone()
        {
            Directory.CreateDirectory(FillFolder);
            var directory = Path.Combine(FillFolder, "gdv-fill-dir.bin");
            Directory.CreateDirectory(directory);
            File.WriteAllText(Path.Combine(directory, "inside.txt"), "x");
            var noMarker = Path.Combine(FillFolder, "gdv-fill-nomarker.bin");
            File.WriteAllText(noMarker, "somebody else's data");
            var other = Path.Combine(FillFolder, "readme.txt");
            File.WriteAllText(other, "keep");

            _f.Run(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.1}"));

            Assert.IsTrue(Directory.Exists(directory));
            Assert.IsTrue(File.Exists(Path.Combine(directory, "inside.txt")));
            Assert.IsTrue(File.Exists(noMarker));
            Assert.IsTrue(File.Exists(other));
            Assert.IsTrue(_f.ReadResult().Unverified.Any(u => u.Contains("gdv-fill-nomarker.bin") && u.Contains("left alone")));
        }

        [Test]
        public void ASecondRunInTheSameProcessDoesNotSweepAwayTheFileOfTheFirst()
        {
            var first = new PlanRunner(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + 20 * MiB)), null, _f.Options);
            var routine = first.Execute();
            for (var i = 0; i < 8; i++)
            {
                Assert.IsTrue(routine.MoveNext());
                _f.Clock.Advance(16);
            }
            var before = Used();
            Assert.Greater(before, 0);
            _f.Options.RunId = "second-run";
            var second = new PlanRunner(HarnessFixture.Plan("{\"type\":\"wait\",\"timeoutSec\":5,\"seconds\":0.1}"), null, _f.Options);

            _f.Pump(second);

            Assert.AreEqual("PASS", second.Result.Status);
            Assert.GreaterOrEqual(Used(), before, "the file of the first run is still there");
            while (routine.MoveNext())
                _f.Clock.Advance(16);
            Assert.AreEqual("PASS", first.Result.Status);
            Assert.AreEqual(20 * MiB, Single(first.Result, "fill.bytesWritten"));
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void RunIdWithAColonMakesASafeFileName()
        {
            _f.Options.RunId = "plan:2026-11-09";

            _f.Run(HarnessFixture.Plan(Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + MiB)));

            Assert.AreEqual("PASS", new PlanRunnerResultReader(_f).Status);
            Assert.AreEqual(0, Used());
        }

        [Test]
        public void SecondFillWithoutReleaseIsBlocked()
        {
            var step = Fill("\"reserveBytes\":" + 10 * MiB + ",\"bytes\":" + MiB);

            _f.Run(HarnessFixture.Plan(step + "," + step));

            var result = _f.ReadResult();
            Assert.AreEqual("BLOCKED", result.Status);
            StringAssert.Contains("storage.release", result.Reason);
        }

        [Test]
        public void BadArgumentsFailTheStepAndWriteNothing()
        {
            foreach (var arguments in new[]
            {
                "\"bytes\":1",
                "\"reserveBytes\":1",
                "\"leaveFreeBytes\":10",
                "\"reserveBytes\":1,\"bytes\":1,\"leaveFreeBytes\":10,\"toleranceBytes\":1",
                "\"reserveBytes\":1,\"leaveFreeBytes\":10",
                "\"reserveBytes\":-1,\"bytes\":1",
                "\"reserveBytes\":1.5,\"bytes\":1",
                "\"reserveBytes\":100,\"leaveFreeBytes\":10,\"toleranceBytes\":1",
                "\"reserveBytes\":0,\"bytes\":1",
                "\"reserveBytes\":1,\"bytes\":1,\"toleranceBytes\":1",
                "\"reserveBytes\":1,\"bytes\":1,\"maxAdjustments\":4294967296",
                "\"reserveBytes\":1,\"bytes\":1,\"maxAdjustments\":-1",
            })
            {
                using (var fixture = new HarnessFixture())
                {
                    fixture.Run(HarnessFixture.Plan(Fill(arguments)));

                    var result = fixture.ReadResult();
                    Assert.AreEqual("FAIL", result.Status, arguments);
                    StringAssert.Contains("invalid arguments", result.Reason, arguments);
                }
            }
        }

        sealed class PlanRunnerResultReader
        {
            readonly string _status;

            public PlanRunnerResultReader(HarnessFixture fixture)
            {
                var path = Directory.GetFiles(Path.Combine(fixture.Root, "gdv", "out"), "result.json", SearchOption.AllDirectories).Single();
                _status = ResultDocument.Parse(File.ReadAllText(path)).Status;
            }

            public string Status => _status;
        }

        [Test]
        public void CallTimeReportsTheDurationOfAProjectCall()
        {
            ProjectCalls.Shared.Register("read", context =>
            {
                _f.Clock.Advance(12);
                return null;
            });

            _f.Run(HarnessFixture.Plan("{\"type\":\"call.time\",\"timeoutSec\":5,\"call\":\"read\",\"metric\":\"readMs\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(12, Single(result, "readMs"));
            Assert.AreEqual("ms", result.Metrics.Single(m => m.Name == "readMs").Unit);
        }

        [Test]
        public void UnknownAnswerStillReportsTheTimeAndIsListedAsUnverified()
        {
            ProjectCalls.Shared.Register("read", context =>
            {
                _f.Clock.Advance(5);
                return ProjectCallResult.Unknown("free space is unknown (-1)");
            });

            _f.Run(HarnessFixture.Plan("{\"type\":\"call.time\",\"timeoutSec\":5,\"call\":\"read\",\"metric\":\"readMs\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("PASS", result.Status);
            Assert.AreEqual(5, Single(result, "readMs"));
            Assert.IsTrue(result.Unverified.Any(u => u.Contains("free space is unknown")));
        }

        [Test]
        public void CallThatThrowsFailsTheStepAndReportsNothing()
        {
            ProjectCalls.Shared.Register("read", context => throw new IOException("no disk"));

            _f.Run(HarnessFixture.Plan("{\"type\":\"call.time\",\"timeoutSec\":5,\"call\":\"read\",\"metric\":\"readMs\"}"));

            var result = _f.ReadResult();
            Assert.AreEqual("FAIL", result.Status);
            StringAssert.Contains("no disk", result.Reason);
            Assert.IsEmpty(result.Metrics);
        }

        [Test]
        public void CallThatIsNotRegisteredIsBlocked()
        {
            _f.Run(HarnessFixture.Plan("{\"type\":\"call.time\",\"timeoutSec\":5,\"call\":\"read\",\"metric\":\"readMs\"}"));

            Assert.AreEqual("BLOCKED", _f.ReadResult().Status);
        }

        [Test]
        public void CallTimeInARepeatGivesADistribution()
        {
            var calls = 0;
            ProjectCalls.Shared.Register("read", context =>
            {
                calls++;
                _f.Clock.Advance(calls * 10);
                return null;
            });

            _f.Run(HarnessFixture.Plan("{\"type\":\"repeat\",\"timeoutSec\":30,\"count\":4,\"steps\":[{\"type\":\"call.time\",\"timeoutSec\":5,\"call\":\"read\",\"metric\":\"readMs\"}]}"));

            var metric = _f.ReadResult().Metrics.Single(m => m.Name == "readMs");
            Assert.AreEqual(new[] { 10.0, 20.0, 30.0, 40.0 }, metric.Samples.ToArray());
            Assert.AreEqual(4, metric.Stats.N);
            Assert.AreEqual(25, metric.Stats.Median);
            Assert.AreEqual(40, metric.Stats.Max);
        }
    }
}
