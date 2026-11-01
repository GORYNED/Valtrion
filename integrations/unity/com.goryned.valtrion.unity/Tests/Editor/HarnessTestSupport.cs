using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Tests
{
    /// <summary>A clock the test advances by hand, one 16 ms frame per pump step.</summary>
    sealed class ManualClock : IClock
    {
        public double NowMs { get; private set; }

        public DateTime UtcNow => new DateTime(2026, 10, 29, 15, 30, 0, DateTimeKind.Utc).AddMilliseconds(NowMs);

        public long FrameCount { get; private set; }

        public void Advance(double ms)
        {
            NowMs += ms;
            FrameCount++;
        }
    }

    /// <summary>One run in a temporary folder with a manual clock; nothing touches the real persistent data path.</summary>
    sealed class HarnessFixture : IDisposable
    {
        public HarnessFixture()
        {
            Root = Path.Combine(Path.GetTempPath(), "gdv-tests-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(Root);
            Clock = new ManualClock();
            Registry = StepRegistry.CreateDefault(false);
            Registry.Register("test.counter", Counter);
            Registry.Register("test.fail", Fail);
            Registry.Register("test.sample", Sample);
            Registry.Register("test.feed", Feed);
            Options = new RunOptions
            {
                Clock = Clock,
                PersistentDataPath = Root,
                RootDirectory = Path.Combine(Root, "gdv"),
                RunId = "test-run",
                Steps = Registry,
                Conditions = new ConditionRegistry(),
                AttachLog = false,
                Log = Log.Add,
                Quit = code => QuitCode = code,
                Environment = new RunEnvironment
                {
                    Candidate = new ResultCandidate { Repo = "repo", Sha = new string('a', 40), Profile = "Test", Unity = "6000.6", Dirty = false },
                    Target = new ResultTarget { Platform = "editor", DeviceClass = "test", OsMajor = 1, Evidence = EvidenceKind.Editor },
                    Host = new ResultHost { Id = "test", Transport = "local", SelectedBy = "local" },
                },
            };
        }

        public static int CounterValue;

        public string Root { get; }

        public ManualClock Clock { get; }

        public StepRegistry Registry { get; }

        public RunOptions Options { get; }

        public List<string> Log { get; } = new List<string>();

        public int? QuitCode { get; private set; }

        public string Inbox => Path.Combine(Root, "gdv", "inbox");

        public string RunDirectory => Path.Combine(Root, "gdv", "out", "test-run");

        /// <summary>Builds a plan from its JSON; <paramref name="body"/> holds the properties after the required ones.</summary>
        public static PlanDocument Plan(string steps, string extra = "", string kind = "functional", int budget = 60) =>
            PlanDocument.Parse($"{{\"schema\":\"gdv.plan/1\",\"id\":\"test.plan\",\"kind\":\"{kind}\",\"dataPolicy\":\"isolated\",\"budgetSeconds\":{budget}{extra},\"steps\":[{steps}]}}");

        public PlanRunner Run(PlanDocument plan, Action each = null)
        {
            var runner = new PlanRunner(plan, plan.ToJsonString(), Options);
            Pump(runner, each);
            return runner;
        }

        public void Pump(PlanRunner runner, Action each = null)
        {
            var routine = runner.Execute();
            for (var count = 0; routine.MoveNext(); count++)
            {
                Clock.Advance(16);
                each?.Invoke();
                if (count > 200000)
                    Assert.Fail("the run did not finish");
            }
        }

        public ResultDocument ReadResult() => ResultDocument.Parse(File.ReadAllText(Path.Combine(RunDirectory, "result.json")));

        public void Dispose()
        {
            if (Directory.Exists(Root))
                Directory.Delete(Root, true);
        }

        static IEnumerator Counter(StepContext context)
        {
            CounterValue++;
            yield break;
        }

        static IEnumerator Fail(StepContext context)
        {
            context.Fail("boom");
            yield break;
        }

        static IEnumerator Sample(StepContext context)
        {
            context.AddMetric("test.value", "ms", context.Number("value"));
            yield break;
        }

        static IEnumerator Feed(StepContext context)
        {
            context.Run.Logs.Feed(context.String("line"));
            yield break;
        }
    }
}
