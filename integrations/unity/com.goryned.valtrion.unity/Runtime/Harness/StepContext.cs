using System;
using System.Collections.Generic;
using System.Globalization;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;
using Valtrion.Unity.Probes;

namespace Valtrion.Unity.Harness
{
    /// <summary>Everything shared by the steps of one run.</summary>
    public sealed class RunContext
    {
        internal RunContext(PlanDocument plan, string runId, IClock clock, RunPaths paths, LogTap logs, CleanupLedger ledger, ConditionRegistry conditions)
        {
            Plan = plan;
            RunId = runId;
            Clock = clock;
            Paths = paths;
            Logs = logs;
            Ledger = ledger;
            Conditions = conditions;
        }

        public PlanDocument Plan { get; }

        public string RunId { get; }

        public IClock Clock { get; }

        public RunPaths Paths { get; }

        public LogTap Logs { get; }

        public CleanupLedger Ledger { get; }

        public ConditionRegistry Conditions { get; }

        /// <summary>The step types of this run; <c>repeat</c> runs inner steps through it.</summary>
        public StepRegistry Steps { get; internal set; }

        public MetricStore Metrics { get; } = new MetricStore();

        public List<ResultArtifact> Artifacts { get; } = new List<ResultArtifact>();

        public List<string> Unverified { get; } = new List<string>();

        /// <summary>State that steps of one run hand to each other (an open storage window, a started timer), by key. Steps own their keys and prefix them.</summary>
        public Dictionary<string, object> State { get; } = new Dictionary<string, object>(StringComparer.Ordinal);

        /// <summary>The open probe window, or null. <c>external.ui</c> is refused while it is open.</summary>
        public ProbeWindow Window { get; internal set; }

        /// <summary>A request the host must answer; written into the heartbeat while the step waits.</summary>
        public HostRequest PendingRequest { get; set; }

        public double RunStartMs { get; internal set; }

        public double IterationStartMs { get; internal set; }

        public bool QuitRequested { get; set; }

        public int QuitExitCode { get; set; }

        internal int RequestCounter;
    }

    /// <summary>The view of one step execution: its arguments, its outcome and the shared run.</summary>
    public sealed class StepContext
    {
        readonly Dictionary<string, double> _marks = new Dictionary<string, double>();

        internal StepContext(RunContext run, PlanStep step, int index, string phase, int iteration)
        {
            Run = run;
            Step = step;
            Index = index;
            Phase = phase;
            Iteration = iteration;
        }

        public RunContext Run { get; }

        public PlanStep Step { get; }

        /// <summary>Position in the setup or steps list.</summary>
        public int Index { get; }

        /// <summary>"setup" or "run".</summary>
        public string Phase { get; }

        /// <summary>Zero-based measured iteration; negative during warm-up, 0 during setup.</summary>
        public int Iteration { get; }

        /// <summary>The one-based round of the <c>repeat</c> step this step runs in, or 0 outside a <c>repeat</c>. A step that writes a file names it after the round.</summary>
        public long Round { get; internal set; }

        public JsonValue Args => Step.Args;

        public IClock Clock => Run.Clock;

        public string Status { get; private set; } = ValidationStatus.Pass;

        public string Message { get; private set; }

        public IReadOnlyDictionary<string, double> Marks => _marks;

        public void Pass(string message = null) => SetStatus(ValidationStatus.Pass, message);

        public void Fail(string message) => SetStatus(ValidationStatus.Fail, message);

        public void Blocked(string message) => SetStatus(ValidationStatus.Blocked, message);

        public void NeedsPermission(string message) => SetStatus(ValidationStatus.NeedsPermission, message);

        public void SetStatus(string status, string message)
        {
            if (Array.IndexOf(ValidationStatus.All, status) < 0)
                throw new ArgumentException($"unknown status '{status}'", nameof(status));
            Status = status;
            Message = message;
        }

        /// <summary>Progress text reported if the step times out.</summary>
        public void Note(string message)
        {
            if (Status == ValidationStatus.Pass)
                Message = message;
        }

        public void Mark(string name, double value)
        {
            if (!double.IsNaN(value) && !double.IsInfinity(value))
                _marks[name] = value;
        }

        public void AddMetric(string name, string unit, double value) => Run.Metrics.Add(name, unit, value);

        public string String(string name, string fallback = null)
        {
            if (Args.TryGet(name, out var value))
            {
                if (value.Kind != JsonKind.String)
                    throw new StepArgumentException($"argument '{name}' must be a string");
                return value.AsString;
            }
            return fallback ?? throw new StepArgumentException($"argument '{name}' is required");
        }

        public double Number(string name, double? fallback = null)
        {
            if (Args.TryGet(name, out var value))
            {
                if (value.Kind != JsonKind.Number)
                    throw new StepArgumentException($"argument '{name}' must be a number");
                return value.AsNumber;
            }
            if (fallback == null)
                throw new StepArgumentException($"argument '{name}' is required");
            return fallback.Value;
        }

        public bool Bool(string name, bool fallback)
        {
            if (!Args.TryGet(name, out var value))
                return fallback;
            if (value.Kind != JsonKind.Bool)
                throw new StepArgumentException($"argument '{name}' must be true or false");
            return value.AsBool;
        }

        public override string ToString() => string.Format(CultureInfo.InvariantCulture, "{0}[{1}] {2}", Phase, Index, Step.Type);
    }
}
