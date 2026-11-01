using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using Valtrion.Unity.Contracts;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>Settings of one run. Every member has a default for a real device; tests replace the clock, folders and hooks.</summary>
    public sealed class RunOptions
    {
        public IClock Clock { get; set; }

        /// <summary>Defaults to <c>Application.persistentDataPath</c>.</summary>
        public string PersistentDataPath { get; set; }

        /// <summary>The <c>gdv</c> folder (inbox, out, work). Defaults to <c>&lt;persistentDataPath&gt;/gdv</c>.</summary>
        public string RootDirectory { get; set; }

        public string RunId { get; set; }

        /// <summary>Replaces <c>&lt;RootDirectory&gt;/out</c>; the run writes to <c>&lt;OutRoot&gt;/&lt;runId&gt;/</c>.</summary>
        public string OutRoot { get; set; }

        public StepRegistry Steps { get; set; }

        public ConditionRegistry Conditions { get; set; }

        public RunEnvironment Environment { get; set; }

        public List<ICleanupHandler> CleanupHandlers { get; } = new List<ICleanupHandler>();

        public Action<string> Log { get; set; }

        /// <summary>Called with the exit code after the result is written when a step asked to quit. Defaults to <c>Application.Quit</c>.</summary>
        public Action<int> Quit { get; set; }

        public int HeartbeatIntervalMs { get; set; } = 2000;

        /// <summary>Follow the Unity log. Tests that feed <see cref="LogTap.Feed"/> turn it off.</summary>
        public bool AttachLog { get; set; } = true;

        public bool DiscoverSteps { get; set; } = true;
    }

    /// <summary>Executes a plan on the main thread as a coroutine: setup, then warm-up and measured iterations of the steps. Every step has its own
    /// timeout and the plan has a budget; the first non-PASS step ends the run unless <c>continueOnFail</c>. Statuses are PASS, FAIL, BLOCKED,
    /// NEEDS_PERMISSION and TIMEOUT. A step handler must yield <c>null</c> or <c>WaitForEndOfFrame</c> and never block.</summary>
    public sealed class PlanRunner
    {
        sealed class Work
        {
            public PlanStep Step;
            public int Index;
            public string Phase;
            public int Iteration;
            public bool Measured;
            public bool IterationStart;
            public bool IterationEnd;
        }

        const double StepTimeoutGraceMs = 1000.0;

        readonly PlanDocument _plan;
        readonly string _planText;
        readonly RunOptions _options;
        readonly IClock _clock;
        readonly RunPaths _paths;
        readonly ResultWriter _writer;
        readonly RunContext _run;
        readonly RunEnvironment _environment;
        readonly StepRegistry _steps;
        readonly List<ResultStep> _recorded = new List<ResultStep>();
        readonly string _invalid;
        DateTime _startedUtc;
        double _lastBeatMs = double.MinValue;
        long _seq;
        string _state = "starting";
        StepContext _current;
        Work _currentItem;
        Running _currentRunning;
        string _failureStatus;
        string _failureReason;
        string _forcedStatus;
        string _forcedReason;
        bool _started;
        bool _faultReported;
        HostRequest _lastRequest;

        public PlanRunner(PlanDocument plan, string planText, RunOptions options = null)
            : this(plan, planText, options, null)
        {
        }

        PlanRunner(PlanDocument plan, string planText, RunOptions options, string invalid)
        {
            _plan = plan;
            _planText = planText ?? plan.ToJsonString();
            _options = options ?? new RunOptions();
            _invalid = invalid;
            _clock = _options.Clock ?? new SystemClock();
            var persistent = _options.PersistentDataPath ?? Application.persistentDataPath;
            var root = _options.RootDirectory ?? Path.Combine(persistent, "gdv");
            RunId = Identifiers.Sanitize(_options.RunId ?? $"{plan.Id}-{UtcFormat.Compact(_clock.UtcNow)}-{Guid.NewGuid().ToString("N").Substring(0, 4)}");
            _paths = new RunPaths(persistent, root, RunId, _options.OutRoot);
            _options.Log = _options.Log ?? Debug.Log;
            _writer = new ResultWriter(_paths, _options.Log);
            _environment = _options.Environment ?? RunEnvironment.Detect(_paths.Inbox);
            _steps = _options.Steps ?? StepRegistry.CreateDefault(_options.DiscoverSteps);
            var ledger = new CleanupLedger(RunId, _paths.LedgerPath);
            foreach (var handler in _options.CleanupHandlers)
                ledger.Register(handler);
            ledger.LoadExisting();
            _run = new RunContext(plan, RunId, _clock, _paths, new LogTap(_options.AttachLog), ledger, _options.Conditions ?? ConditionRegistry.Shared);
        }

        /// <summary>A runner for a plan that could not be read: it writes a FAIL result naming the reason and runs nothing.</summary>
        public static PlanRunner ForRejectedPlan(string error, string text, RunOptions options = null)
        {
            var plan = new PlanDocument { Id = "invalid-plan", Kind = "smoke", DataPolicy = "isolated", BudgetSeconds = 1 };
            return new PlanRunner(plan, text ?? string.Empty, options, error);
        }

        public string RunId { get; }

        public bool Finished { get; private set; }

        public ResultDocument Result { get; private set; }

        public string RunDirectory => _paths.RunDirectory;

        public string ResultPath => _paths.ResultPath;

        public RunContext Context => _run;

        /// <summary>The coroutine of the run; start it with <c>StartCoroutine</c> or pump it with <c>MoveNext</c>.</summary>
        public IEnumerator Execute()
        {
            if (_started)
                yield break;
            _started = true;
            _startedUtc = _clock.UtcNow;
            DeleteStale(_paths.ResultPath);
            DeleteStale(_paths.HeartbeatPath);
            _run.RunStartMs = _clock.NowMs;
            _run.IterationStartMs = _run.RunStartMs;
            try
            {
                _writer.WriteCurrentPointer(RunId);
            }
            catch (Exception ex)
            {
                _options.Log($"GDV: cannot write current.json: {ex.Message}");
            }
            Beat("starting", true);
            _state = "running";
            if (_invalid == null)
            {
                foreach (var problem in _steps.DiscoveryProblems)
                    _run.Unverified.Add(problem);
                var count = WorkCount();
                for (long index = 0; index < count && !Finished; index++)
                {
                    var item = WorkAt(index);
                    if (item.IterationStart)
                    {
                        _run.IterationStartMs = _clock.NowMs;
                        _run.Metrics.Enabled = item.Measured;
                    }
                    var step = StartStep(item);
                    while (step.Routine != null)
                    {
                        bool more;
                        try
                        {
                            more = step.Routine.MoveNext();
                        }
                        catch (Exception ex)
                        {
                            Fault(step.Context, ex);
                            break;
                        }
                        Tick();
                        if (!more)
                            break;
                        var now = _clock.NowMs;
                        if (now - _run.RunStartMs > _plan.BudgetSeconds * 1000.0)
                        {
                            step.Context.SetStatus(ValidationStatus.Timeout, $"plan budget of {_plan.BudgetSeconds} s exceeded" + Suffix(step.Context.Message));
                            _forcedStatus = ValidationStatus.Timeout;
                            _forcedReason = $"plan budget of {_plan.BudgetSeconds} s exceeded during {step.Context}";
                            break;
                        }
                        if (now - step.StartMs > item.Step.TimeoutSec * 1000.0)
                        {
                            step.Context.SetStatus(ValidationStatus.Timeout, $"step timeout of {item.Step.TimeoutSec} s" + Suffix(step.Context.Message));
                            break;
                        }
                        // Only a frame wait is forwarded: any other yield instruction or nested coroutine could outlive the step
                        // timeout and the plan budget, which this loop can enforce only between frames.
                        var yielded = step.Routine.Current;
                        if (yielded != null && !(yielded is WaitForEndOfFrame))
                        {
                            step.Context.SetStatus(ValidationStatus.Fail, $"step yielded {yielded.GetType().Name}; a step may yield only null or WaitForEndOfFrame so its timeout holds");
                            break;
                        }
                        yield return yielded;
                        if (Finished)
                            yield break;
                    }
                    if (Finished)
                        yield break;
                    FinishStep(item, step);
                    if (item.IterationEnd)
                        CloseDanglingWindow();
                    if (_forcedStatus != null || (_failureStatus != null && _plan.ContinueOnFail != true))
                        break;
                    if (index + 1 < count && _clock.NowMs - _run.RunStartMs > _plan.BudgetSeconds * 1000.0)
                    {
                        _forcedStatus = ValidationStatus.Timeout;
                        _forcedReason = $"plan budget of {_plan.BudgetSeconds} s exceeded before step {index + 2} of {count}";
                        break;
                    }
                    // At least one frame per step: a long repeat of instant steps must not freeze the player or outrun the budget.
                    yield return null;
                }
            }
            Complete();
        }

        /// <summary>Rewrites the heartbeat when the interval has passed. The host component calls it every frame, so a long wait inside a step still beats.</summary>
        public void Heartbeat()
        {
            Beat(_state, false);
            EnforceStepTimeout();
        }

        // The loop in Execute checks the step timeout only when the host resumes the coroutine. A step that waits for the end of the frame
        // never resumes it where no frame is rendered (a batchmode Editor without graphics), so the host's per-frame call ends such a step
        // here. The grace keeps this from racing the regular check. This path always ends the run (even with continueOnFail): a step that
        // never returned cannot be continued past, and after a long frame stall (a hitch longer than timeout plus grace) it wins the race.
        void EnforceStepTimeout()
        {
            var item = _currentItem;
            var step = _currentRunning;
            if (Finished || item == null || step == null)
                return;
            // Whichever comes first, the step's timeout or the plan budget (which always wins), plus the grace.
            var stepDeadline = step.StartMs + item.Step.TimeoutSec * 1000.0;
            var budgetDeadline = _run.RunStartMs + _plan.BudgetSeconds * 1000.0;
            var budgetFirst = budgetDeadline <= stepDeadline;
            if (_clock.NowMs - (budgetFirst ? budgetDeadline : stepDeadline) <= StepTimeoutGraceMs)
                return;
            const string Stuck = "the step did not return to the runner (a wait for the end of the frame never completed)";
            step.Context.SetStatus(ValidationStatus.Timeout, (budgetFirst ? $"plan budget of {_plan.BudgetSeconds} s exceeded; " : $"step timeout of {item.Step.TimeoutSec} s; ") + Stuck + Suffix(step.Context.Message));
            var reason = budgetFirst ? $"plan budget of {_plan.BudgetSeconds} s exceeded during {step.Context}; {Stuck}" : $"{step.Context} {ValidationStatus.Timeout}: {step.Context.Message}";
            FinishStep(item, step);
            _forcedStatus = ValidationStatus.Timeout;
            _forcedReason = reason;  // not _failureReason: an earlier failure of a continueOnFail run must not hide the hang
            Complete();
        }

        /// <summary>Ends the run now with the given status (the application is quitting or the harness was destroyed). Does nothing once finished.</summary>
        public void Abort(string status, string reason)
        {
            if (Finished)
                return;
            _forcedStatus = status;
            _forcedReason = reason;
            Complete();
        }

        sealed class Running
        {
            public StepContext Context;
            public IEnumerator Routine;
            public double StartMs;
        }

        Running StartStep(Work item)
        {
            var context = new StepContext(_run, item.Step, item.Index, item.Phase, item.Iteration);
            var running = new Running { Context = context, StartMs = _clock.NowMs };
            _current = context;
            _currentItem = item;
            _currentRunning = running;
            if (!_steps.TryGet(item.Step.Type, out var handler))
            {
                context.SetStatus(ValidationStatus.Blocked, $"step type '{item.Step.Type}' is not registered in this build");
            }
            else
            {
                try
                {
                    running.Routine = handler(context);
                }
                catch (Exception ex)
                {
                    Fault(context, ex);
                }
            }
            Beat("running", true);
            return running;
        }

        void FinishStep(Work item, Running step)
        {
            if (step.Routine is IDisposable disposable)
            {
                try
                {
                    disposable.Dispose();
                }
                catch (Exception ex)
                {
                    _options.Log($"GDV: step {step.Context} cleanup threw {ex.GetType().Name}: {ex.Message}");
                }
            }
            var context = step.Context;
            var end = _clock.NowMs;
            if (context.Status != ValidationStatus.Pass && _failureStatus == null)
            {
                _failureStatus = context.Status;
                _failureReason = $"{context} {context.Status}" + Suffix(context.Message);
            }
            if (item.Phase == "setup" || item.Measured || context.Status != ValidationStatus.Pass)
            {
                var marks = new Dictionary<string, double>();
                foreach (var pair in context.Marks)
                    marks[pair.Key] = pair.Value;
                if (item.Phase == "run" && item.Measured)
                    marks["iteration"] = item.Iteration + 1;
                _recorded.Add(new ResultStep
                {
                    Type = item.Step.Type,
                    Status = context.Status,
                    StartMs = (long)Math.Round(step.StartMs - _run.RunStartMs),
                    DurationMs = (long)Math.Round(end - step.StartMs),
                    Marks = marks.Count == 0 ? null : marks,
                    Message = context.Message,
                });
            }
            _current = null;
            _currentItem = null;
            _currentRunning = null;
        }

        long WorkCount()
        {
            var setup = _plan.Setup != null ? _plan.Setup.Count : 0;
            return setup + ((long)(_plan.Warmup ?? 0) + (_plan.Repeat ?? 1)) * _plan.Steps.Count;
        }

        Work WorkAt(long position)
        {
            var setup = _plan.Setup != null ? _plan.Setup.Count : 0;
            if (position < setup)
            {
                var index = (int)position;
                return new Work { Step = _plan.Setup[index], Index = index, Phase = "setup", Iteration = 0, Measured = false, IterationStart = index == 0, IterationEnd = index == setup - 1 };
            }
            position -= setup;
            var steps = _plan.Steps.Count;
            var iteration = (int)(position / steps) - (_plan.Warmup ?? 0);
            var stepIndex = (int)(position % steps);
            return new Work { Step = _plan.Steps[stepIndex], Index = stepIndex, Phase = "run", Iteration = iteration, Measured = iteration >= 0, IterationStart = stepIndex == 0, IterationEnd = stepIndex == steps - 1 };
        }

        // A run id reused by a relaunch must not leave the previous launch's verdict where a supervisor reads it.
        void DeleteStale(string path)
        {
            try
            {
                if (File.Exists(path))
                    File.Delete(path);
            }
            catch (Exception ex)
            {
                _options.Log($"GDV: cannot remove the stale file '{path}': {ex.Message}");
            }
        }

        void Tick()
        {
            if (_run.Window != null)
            {
                try
                {
                    _run.Window.Tick(_clock.FrameCount, _clock.NowMs);
                }
                catch (Exception ex)
                {
                    if (!_faultReported)
                        _run.Unverified.Add($"a probe failed while sampling: {ex.GetType().Name}: {ex.Message}");
                    _faultReported = true;
                }
            }
            // A host request appearing or being answered reaches the heartbeat at once, not at the next interval.
            var changed = !ReferenceEquals(_run.PendingRequest, _lastRequest);
            _lastRequest = _run.PendingRequest;
            Beat(_state, changed);
        }

        void CloseDanglingWindow()
        {
            if (_run.Window == null)
                return;
            var window = _run.Window;
            _run.Window = null;
            try
            {
                window.Stop();
            }
            catch (Exception ex)
            {
                _run.Unverified.Add($"probe window close failed: {ex.Message}");
            }
            _run.Unverified.Add("a probe window was still open when the iteration ended and was closed by the runner");
        }

        void Beat(string state, bool force)
        {
            _state = state;
            var now = _clock.NowMs;
            if (!force && now - _lastBeatMs < _options.HeartbeatIntervalMs)
                return;
            _lastBeatMs = now;
            var current = _current;
            var heartbeat = new HeartbeatDocument
            {
                RunId = RunId,
                Seq = _seq++,
                Utc = UtcFormat.Format(_clock.UtcNow),
                State = _run.PendingRequest != null ? "waiting-host" : state,
                StepIndex = current != null ? current.Index : -1,
                StepType = current != null ? current.Step.Type : string.Empty,
                ElapsedMs = (long)Math.Max(0, now - _run.RunStartMs),
                Message = current != null ? current.Message : null,
                Request = _run.PendingRequest,
            };
            try
            {
                _writer.WriteHeartbeat(heartbeat);
            }
            catch (Exception ex)
            {
                _options.Log($"GDV: cannot write the heartbeat: {ex.Message}");
            }
        }

        void Complete()
        {
            if (Finished)
                return;
            Finished = true;
            _run.PendingRequest = null;
            Beat("finishing", true);
            CloseDanglingWindow();
            try
            {
                if (_plan.Cleanup != "none")
                {
                    foreach (var problem in _run.Ledger.Cleanup())
                        _run.Unverified.Add(problem);
                }
            }
            catch (Exception ex)
            {
                _run.Unverified.Add($"cleanup failed: {ex.GetType().Name}: {ex.Message}");
            }
            finally
            {
                _run.Logs.Dispose();
            }
            var status = _forcedStatus ?? _failureStatus ?? ValidationStatus.Pass;
            var reason = status == ValidationStatus.Pass ? null : _forcedReason ?? _failureReason;
            if (_invalid != null)
            {
                status = ValidationStatus.Fail;
                reason = _invalid;
            }
            if (_run.Metrics.Truncated)
                _run.Unverified.Add($"a metric reached {MetricStore.MaxSamples} samples and later samples were dropped");
            Result = BuildResult(status, reason);
            try
            {
                _writer.WriteResult(Result);
            }
            catch (Exception ex)
            {
                _options.Log($"GDV: cannot write the result: {ex.Message}");
                _writer.EmitTerminalLine(ValidationStatus.Blocked, RunId);
                _state = "done";
                return;
            }
            _writer.EmitTerminalLine(status, RunId);
            Beat("done", true);
            if (_run.QuitRequested)
                (_options.Quit ?? DefaultQuit)(_run.QuitExitCode);
        }

        ResultDocument BuildResult(string status, string reason)
        {
            var unverified = new List<string>(_environment.Unverified);
            unverified.AddRange(_run.Unverified);
            var layers = new ResultLayers { Scenario = status };
            return new ResultDocument
            {
                RunId = RunId,
                Status = status,
                Reason = status == ValidationStatus.Pass ? null : string.IsNullOrEmpty(reason) ? status : reason,
                Candidate = _environment.Candidate,
                Target = _environment.Target,
                Host = _environment.Host,
                Plan = new ResultPlan { Id = _plan.Id, Sha256 = Hashing.Sha256Hex(_planText) },
                StartedUtc = UtcFormat.Format(_startedUtc),
                FinishedUtc = UtcFormat.Format(_clock.UtcNow),
                Layers = layers,
                Steps = _recorded,
                Metrics = _run.Metrics.Build(_plan.Metrics),
                Artifacts = _run.Artifacts,
                Unverified = unverified,
            };
        }

        static void DefaultQuit(int exitCode)
        {
#if UNITY_EDITOR
            UnityEditor.EditorApplication.isPlaying = false;
#else
            Application.Quit(exitCode);
#endif
        }

        // A step that throws fails with the exception type, its message and the first stack frame, and the full exception goes to the log:
        // a bare "Object reference not set to an instance of an object" cannot be located. An argument error is the plan's fault and
        // needs no stack.
        void Fault(StepContext context, Exception ex)
        {
            string text;
            try
            {
                text = Faults.Describe(ex);
            }
            catch (Exception)
            {
                text = ex.GetType().Name + " (details unavailable)";  // a throwing Message override must not end the run without a result
            }
            context.SetStatus(ValidationStatus.Fail, text);
            if (ex is StepArgumentException)
                return;
            try
            {
                _options.Log($"GDV: step {context} threw {ex}");
            }
            catch (Exception)
            {
                // The result matters more than the log line.
            }
        }

        static string Suffix(string message) => string.IsNullOrEmpty(message) ? string.Empty : ": " + message;
    }
}
