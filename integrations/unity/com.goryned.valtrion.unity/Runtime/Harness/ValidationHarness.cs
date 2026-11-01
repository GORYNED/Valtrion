using System;
using System.IO;
using Valtrion.Unity.Contracts;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Valtrion.Unity.Harness
{
    /// <summary>What the automatic start did at launch.</summary>
    public enum AutoStartDecision
    {
        NotEvaluated,
        NoPlan,
        AlreadyRunning,
        SuppressedByTestRun,
        Started,
        Failed,
    }

    /// <summary>Starts runs. The bootstrap calls <see cref="TryAutoStart"/> once after the first scene loaded; it does nothing unless a
    /// plan is found. Tests and project code start a run directly with <see cref="Start"/>.</summary>
    public static class ValidationHarness
    {
        public static PlanRunner Current { get; private set; }

        /// <summary>What the bootstrap itself decided at launch, recorded only by the bootstrap (kept for tests and diagnostics).</summary>
        public static AutoStartDecision BootstrapDecision { get; internal set; }

        /// <summary>An explicit claim that a test owns this play session. Set it from a <c>RuntimeInitializeOnLoadMethod(BeforeSceneLoad)</c> of the
        /// test assembly; it is cleared again at <c>SubsystemRegistration</c>, so it never leaks into a later play session.</summary>
        public static bool TestOwned { get; set; }

        public const string TestRunnerControllerType = "UnityEngine.TestTools.TestRunner.PlaymodeTestsController, UnityEngine.TestRunner";

        /// <summary>True while a Unity Test Framework run owns this play session. Signals, any one is enough: <see cref="TestOwned"/>; the scene
        /// <c>InitTestScene…</c> the runner loads; the runner's own <c>PlaymodeTestsController</c> object (found by type, also when hidden). The automatic
        /// start stays out of such a session: a test starts its plan itself (<c>PlanTest</c>), so one plan never runs twice.</summary>
        public static bool IsTestRun => IsTestRunSignal(TestOwned || CommandLineRunsTests(Environment.GetCommandLineArgs()), SceneManager.GetActiveScene().name, TestRunnerObjectPresent);

        /// <summary>True when the process was started to run tests (<c>-runTests</c>): covers Edit Mode tests that enter Play Mode themselves, where the
        /// runner has no controller object or test scene.</summary>
        public static bool CommandLineRunsTests(string[] arguments)
        {
            foreach (var argument in arguments ?? Array.Empty<string>())
            {
                if (string.Equals(argument, "-runTests", StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            return false;
        }

        public static bool IsTestRunSignal(bool testOwned, string sceneName, Func<bool> runnerObjectPresent) =>
            testOwned || IsTestScene(sceneName) || runnerObjectPresent();

        public static bool IsTestScene(string sceneName) => sceneName != null && sceneName.StartsWith("InitTestScene", StringComparison.Ordinal);

        /// <summary>True when the Test Framework's play-mode controller object exists (it is added to the test scene by the launcher, before the scene loads).</summary>
        public static bool TestRunnerObjectPresent()
        {
            var type = Type.GetType(TestRunnerControllerType, false);
            if (type == null)
                return false;
            // Only objects in a loaded scene count: a hidden leftover from an earlier session is not a running test.
            foreach (var candidate in Resources.FindObjectsOfTypeAll(type))
            {
                if (candidate is Component component && component.gameObject.scene.IsValid())
                    return true;
            }
            return false;
        }

        /// <summary>What the bootstrap does: start the plan of this launch unless a test run owns the session. The cheap plan check comes first, so a
        /// launch without a plan pays nothing for the test-run detection. The parameters are a test seam.</summary>
        public static bool TryAutoStart(IPlanEnvironment environment = null, Func<bool> isTestRun = null, RunOptions options = null) =>
            AutoStart(environment, isTestRun, options) == AutoStartDecision.Started;

        internal static AutoStartDecision AutoStart(IPlanEnvironment environment, Func<bool> isTestRun, RunOptions options)
        {
            if (Current != null && !Current.Finished)
                return AutoStartDecision.AlreadyRunning;
            environment = environment ?? new UnityPlanEnvironment();
            var inbox = new PlanInbox(environment);
            var source = inbox.Find();
            if (source == null)
                return AutoStartDecision.NoPlan;
            var testRun = isTestRun != null ? isTestRun() : IsTestRunSignal(TestOwned || CommandLineRunsTests(environment.CommandLineArgs), SceneManager.GetActiveScene().name, TestRunnerObjectPresent);
            if (testRun)
                return AutoStartDecision.SuppressedByTestRun;
            Begin(inbox, source, environment, options, true);
            return AutoStartDecision.Started;
        }

        internal static void ResetStatics()
        {
            TestOwned = false;
            Current = null;
            BootstrapDecision = AutoStartDecision.NotEvaluated;
        }

        /// <summary>Looks for a plan (launch argument, environment variable, intent extra, inbox, legacy converter); starts it and returns true when one was found.</summary>
        public static bool TryStartFromInbox(IPlanEnvironment environment = null, RunOptions options = null, bool consume = true)
        {
            if (Current != null && !Current.Finished)
                return false;
            environment = environment ?? new UnityPlanEnvironment();
            var inbox = new PlanInbox(environment);
            var source = inbox.Find();
            if (source == null)
                return false;
            Begin(inbox, source, environment, options, consume);
            return true;
        }

        static void Begin(PlanInbox inbox, PlanSource source, IPlanEnvironment environment, RunOptions options, bool consume)
        {
            if (consume)
                inbox.MarkConsumed(source);
            options = options ?? new RunOptions();
            options.PersistentDataPath = options.PersistentDataPath ?? environment.PersistentDataPath;
            options.RunId = options.RunId ?? source.RunId;
            options.OutRoot = options.OutRoot ?? source.OutRoot;
            PlanRunner runner;
            try
            {
                runner = source.Plan == null
                    ? PlanRunner.ForRejectedPlan(source.Error, source.Text, options)
                    : new PlanRunner(source.Plan, source.Text, options);
            }
            catch (Exception ex)
            {
                // The plan is already consumed, so the supervisor must still get a result.
                runner = PlanRunner.ForRejectedPlan($"the harness could not prepare the run: {ex.GetType().Name}: {ex.Message}", source.Text, options);
            }
            Host(runner);
        }

        /// <summary>Starts a run of the plan on a hidden host object and returns its runner.</summary>
        public static PlanRunner Start(PlanDocument plan, string planText, RunOptions options = null) => Host(new PlanRunner(plan, planText, options));

        public static PlanRunner Start(string planPath, RunOptions options = null)
        {
            var text = File.ReadAllText(planPath);
            return Start(PlanDocument.Parse(text), text, options);
        }

        static PlanRunner Host(PlanRunner runner)
        {
            Current = runner;
            var gameObject = new GameObject("GDV Harness") { hideFlags = HideFlags.HideAndDontSave };
            UnityEngine.Object.DontDestroyOnLoad(gameObject);
            var host = gameObject.AddComponent<HarnessHost>();
            host.Begin(runner);
            return runner;
        }
    }

    /// <summary>Drives the runner coroutine, keeps the heartbeat beating every frame and closes the run if the application quits first.</summary>
    sealed class HarnessHost : MonoBehaviour
    {
        PlanRunner _runner;

        public void Begin(PlanRunner runner)
        {
            _runner = runner;
            StartCoroutine(runner.Execute());
        }

        void Update()
        {
            if (_runner == null)
                return;
            if (_runner.Finished)
                Destroy(gameObject);
            else
                _runner.Heartbeat();
        }

        void OnApplicationQuit()
        {
            if (_runner != null)
                _runner.Abort(ValidationStatus.Fail, "the application quit before the plan finished");
            // HideAndDontSave objects survive a stopped Editor play session, so remove it here.
            DestroyImmediate(gameObject);
        }

        void OnDestroy()
        {
            if (_runner != null && Application.isPlaying)
                _runner.Abort(ValidationStatus.Fail, "the harness host was destroyed before the plan finished");
        }
    }

    static class Bootstrap
    {
        // With Enter Play Mode domain reload disabled, statics survive between play sessions.
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        static void Reset() => ValidationHarness.ResetStatics();

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        static void StartIfPlanned()
        {
            try
            {
                ValidationHarness.BootstrapDecision = ValidationHarness.AutoStart(null, null, null);
            }
            catch (Exception ex)
            {
                ValidationHarness.BootstrapDecision = AutoStartDecision.Failed;
                Debug.LogError($"GDV: the harness could not start: {ex}");
            }
        }
    }
}
