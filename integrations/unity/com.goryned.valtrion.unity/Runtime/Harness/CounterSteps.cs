using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Harness
{
    /// <summary>A reading of the device's thermal state: a number the plan can compare between runs and the platform's own word for it.</summary>
    public sealed class ThermalReading
    {
        public ThermalReading(double level, string label)
        {
            Level = level;
            Label = label;
        }

        /// <summary>The level as a number (the project decides the scale, for example nominal 0, fair 1, serious 2, critical 3).</summary>
        public double Level { get; }

        public string Label { get; }
    }

    /// <summary>Reads the thermal state. Returns null when it is unknown. Called on the main thread only.</summary>
    public delegate ThermalReading ThermalReader(StepContext context);

    /// <summary>The thermal readers of a project, by name. The app registers them before the plan runs, on the main thread.</summary>
    public sealed class ThermalReaders
    {
        public static readonly ThermalReaders Shared = new ThermalReaders();

        readonly Dictionary<string, ThermalReader> _readers = new Dictionary<string, ThermalReader>(StringComparer.Ordinal);

        public void Register(string name, ThermalReader reader) => _readers[name] = reader ?? throw new ArgumentNullException(nameof(reader));

        public bool Unregister(string name) => _readers.Remove(name);

        public bool TryGet(string name, out ThermalReader reader) => _readers.TryGetValue(name, out reader);
    }

    /// <summary>Steps that count events in the log between two points of a run, for example dropped frames or faulted phases, so their frequency can be
    /// compared between runs. They report numbers and apply no threshold.</summary>
    public static class CounterSteps
    {
        static readonly string[] Modes = { "count", "sum", "last", "max", "increase" };

        sealed class Counter
        {
            public string Metric;
            public string Unit;
            public string Mode;
            public Regex Pattern;
            public long Lines;
            public long Unparsed;
            public long TimedOut;
            public long Readings;
            public bool WentDown;
            public double Sum;
            public double First;
            public double Last;
            public double Max;
            public bool HasValue;
        }

        /// <summary>One counter's numbers, copied under the lock the log threads use.</summary>
        struct Snapshot
        {
            public double? Value;
            public long Unparsed;
            public long TimedOut;
            public long Readings;
            public bool WentDown;
        }

        /// <summary>A pattern that backtracks without end must not stall the thread that logs (it can be the main thread): a match that takes longer is not counted.</summary>
        static readonly TimeSpan MatchTimeout = TimeSpan.FromMilliseconds(50);

        sealed class Window : IDisposable
        {
            readonly object _gate = new object();
            readonly LogTap _tap;
            bool _open;
            bool _closed;

            public Window(string name, List<Counter> counters, LogTap tap, string iteration)
            {
                Name = name;
                Counters = counters;
                Iteration = iteration;
                _tap = tap;
            }

            public string Name { get; }

            public List<Counter> Counters { get; }

            public string Iteration { get; }

            public string ThermalReader;

            public ThermalReading ThermalStart;

            public void Open()
            {
                _tap.LineReceived += OnLine;
                _open = true;
            }

            /// <summary>Stops following the log. A line that was already on its way to <c>OnLine</c> finds the window closed and is not counted, so nothing changes
            /// after this returns. Called on the main thread only.</summary>
            public void Close()
            {
                if (_open)
                    _tap.LineReceived -= OnLine;
                _open = false;
                lock (_gate)
                    _closed = true;
            }

            public void Dispose() => Close();

            void OnLine(string line)
            {
                lock (_gate)
                {
                    if (_closed)
                        return;
                    foreach (var counter in Counters)
                    {
                        Match match;
                        try
                        {
                            match = counter.Pattern.Match(line);
                        }
                        catch (RegexMatchTimeoutException)
                        {
                            counter.TimedOut++;
                            continue;
                        }
                        if (!match.Success)
                            continue;
                        counter.Lines++;
                        if (counter.Mode == "count")
                            continue;
                        var group = match.Groups["value"];
                        if (!group.Success || !double.TryParse(group.Value, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
                            || double.IsNaN(value) || double.IsInfinity(value))
                        {
                            counter.Unparsed++;
                            continue;
                        }
                        if (!counter.HasValue)
                        {
                            counter.First = value;
                            counter.Max = value;
                            counter.HasValue = true;
                        }
                        else if (value < counter.Last)
                        {
                            counter.WentDown = true;
                        }
                        counter.Readings++;
                        counter.Sum += value;
                        counter.Last = value;
                        counter.Max = Math.Max(counter.Max, value);
                    }
                }
            }

            /// <summary>Copies the numbers of all counters at once under the lock the log threads use.</summary>
            public List<Snapshot> Take()
            {
                lock (_gate)
                    return Counters.Select(c => new Snapshot { Value = Result(c), Unparsed = c.Unparsed, TimedOut = c.TimedOut, Readings = c.Readings, WentDown = c.WentDown }).ToList();
            }
        }

        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("counter.start", Start);
            registry.Register("counter.stop", Stop);
        }

        /// <summary><c>{ "name": "faults", "counters": [ { "metric": "outOfBuffers", "pattern": "outOfBuffers=(?&lt;value&gt;\\d+)", "mode": "increase" } ], "thermal": "device" }</c>
        /// opens a window that watches the log lines from now until <c>counter.stop</c> of the same <c>name</c>. A counter has a <c>metric</c>, a <c>pattern</c>
        /// (a regular expression) and a <c>mode</c>: <c>count</c> (the default) counts the lines that match; <c>sum</c>, <c>last</c>, <c>max</c> and <c>increase</c> read the number
        /// in the group <c>value</c> of the pattern (<c>increase</c> is the last number minus the first, for a running counter that a statistics line repeats).
        /// <c>unit</c> defaults to <c>count</c>. With <c>thermal</c> (a name in <see cref="ThermalReaders.Shared"/>) the thermal state is read now and at
        /// <c>counter.stop</c>. A thermal reader that is not registered ends the step BLOCKED.</summary>
        public static IEnumerator Start(StepContext context)
        {
            var name = Identifiers.Sanitize(context.String("name"), "counters");
            var key = WindowKey(name);
            var counters = ReadCounters(context);
            ThermalReader thermal = null;
            var thermalName = context.Args.TryGet("thermal", out _) ? context.String("thermal") : null;
            if (thermalName != null && !ThermalReaders.Shared.TryGet(thermalName, out thermal))
            {
                context.Blocked($"thermal reader '{thermalName}' is not registered in this build");
                yield break;
            }
            var identity = context.Phase + ":" + context.Iteration;
            if (context.Run.State.TryGetValue(key, out var open))
            {
                var stale = (Window)open;
                if (stale.Iteration == identity)
                {
                    context.Fail($"counter window '{name}' is already open: call counter.stop first");
                    yield break;
                }
                stale.Dispose();
                context.Run.State.Remove(key);
                Unverified(context, $"counter window '{name}' of an earlier iteration was never closed and was replaced");
            }
            var window = new Window(name, counters, context.Run.Logs, identity) { ThermalReader = thermalName };
            if (thermal != null)
            {
                window.ThermalStart = thermal(context);
                if (window.ThermalStart == null)
                    Unverified(context, $"thermal state at the start of '{name}' is unknown");
            }
            window.Open();
            context.Run.State[key] = window;
        }

        /// <summary><c>{ "name": "faults" }</c> closes the window of <c>counter.start</c> and reports every counter as a metric (one sample per window). A <c>count</c> or
        /// <c>sum</c> of nothing is a real zero. <c>last</c>, <c>max</c> and <c>increase</c> have no number when no line carried one: nothing is reported and it is listed as
        /// unverified, as is a line that matched without a readable number. With <c>thermal</c> the metrics <c>&lt;name&gt;.thermalStartLevel</c> and
        /// <c>&lt;name&gt;.thermalEndLevel</c> are reported next to the counters, and the step message names both states, so a number is never kept apart from the
        /// state of the device it was counted in. The thermal state is read at the two ends of the window, not at each line (the reader runs on the main thread only).</summary>
        public static IEnumerator Stop(StepContext context)
        {
            var name = Identifiers.Sanitize(context.String("name"), "counters");
            var key = WindowKey(name);
            if (!context.Run.State.TryGetValue(key, out var open))
            {
                context.Fail($"no counter window '{name}' is open: call counter.start first");
                yield break;
            }
            var window = (Window)open;
            context.Run.State.Remove(key);
            window.Close();
            if (window.Iteration != context.Phase + ":" + context.Iteration)
            {
                context.Fail($"counter window '{name}' was opened in another iteration (a step failed before counter.stop?): call counter.start in this iteration");
                yield break;
            }
            ThermalReading end = null;
            if (window.ThermalReader != null)
            {
                if (!ThermalReaders.Shared.TryGet(window.ThermalReader, out var thermal))
                {
                    context.Blocked($"thermal reader '{window.ThermalReader}' is not registered in this build");
                    yield break;
                }
                end = thermal(context);
            }
            var snapshots = window.Take();
            for (var i = 0; i < window.Counters.Count; i++)
            {
                var counter = window.Counters[i];
                var snapshot = snapshots[i];
                if (snapshot.Value.HasValue)
                    context.AddMetric(counter.Metric, counter.Unit, snapshot.Value.Value);
                else if (counter.Mode == "increase" && snapshot.Readings == 1)
                    Unverified(context, $"counter '{counter.Metric}' has no number: 'increase' needs two readings in '{name}' and only one line had a number");
                else
                    Unverified(context, $"counter '{counter.Metric}' has no number: no log line with a readable value matched in '{name}'");
                if (snapshot.Unparsed > 0)
                    Unverified(context, $"counter '{counter.Metric}': {snapshot.Unparsed} matching line(s) had no readable number in the group 'value'");
                if (snapshot.TimedOut > 0)
                    Unverified(context, $"counter '{counter.Metric}': the pattern took more than {MatchTimeout.TotalMilliseconds:0} ms on {snapshot.TimedOut} line(s), which were not counted");
                if (snapshot.WentDown && counter.Mode == "increase")
                    Unverified(context, $"counter '{counter.Metric}': the number went down inside '{name}' (the running counter was reset?), so the value is not an increase");
            }
            var message = $"{window.Counters.Count} counter(s) in '{name}'";
            if (window.ThermalReader != null)
            {
                if (window.ThermalStart != null)
                    context.AddMetric(name + ".thermalStartLevel", "level", window.ThermalStart.Level);
                if (window.ThermalStart != null && !IsFinite(window.ThermalStart.Level))
                    Unverified(context, $"thermal level at the start of '{name}' is not a finite number and was not reported");
                if (end != null && !IsFinite(end.Level))
                    Unverified(context, $"thermal level at the end of '{name}' is not a finite number and was not reported");
                if (end != null)
                    context.AddMetric(name + ".thermalEndLevel", "level", end.Level);
                else
                    Unverified(context, $"thermal state at the end of '{name}' is unknown");
                message += $"; thermal {Label(window.ThermalStart)} -> {Label(end)}";
            }
            context.Pass(message);
        }

        static bool IsFinite(double value) => !double.IsNaN(value) && !double.IsInfinity(value);

        static double? Result(Counter counter)
        {
            switch (counter.Mode)
            {
                case "count":
                    return counter.Lines;
                case "sum":
                    return counter.HasValue ? counter.Sum : 0.0;
                case "last":
                    return counter.HasValue ? counter.Last : (double?)null;
                case "max":
                    return counter.HasValue ? counter.Max : (double?)null;
                default:
                    // One reading says nothing about an increase: it is unknown, not 0.
                    return counter.Readings >= 2 ? counter.Last - counter.First : (double?)null;
            }
        }

        static string Label(ThermalReading reading) => reading == null ? "unknown" : string.IsNullOrEmpty(reading.Label) ? reading.Level.ToString(CultureInfo.InvariantCulture) : reading.Label;

        static List<Counter> ReadCounters(StepContext context)
        {
            if (!context.Args.TryGet("counters", out var specs) || specs.Kind != JsonKind.Array || specs.Count == 0)
                throw new StepArgumentException("argument 'counters' must be a non-empty array");
            var counters = new List<Counter>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var spec in specs.Items)
            {
                if (spec.Kind != JsonKind.Object)
                    throw new StepArgumentException("a counter is an object with 'metric' and 'pattern'");
                var metric = Text(spec, "metric") ?? throw new StepArgumentException("a counter needs a 'metric'");
                var pattern = Text(spec, "pattern") ?? throw new StepArgumentException($"counter '{metric}' needs a 'pattern'");
                var mode = Text(spec, "mode") ?? "count";
                if (Array.IndexOf(Modes, mode) < 0)
                    throw new StepArgumentException($"counter '{metric}': mode '{mode}' is unknown (count, sum, last, max, increase)");
                if (!seen.Add(Identifiers.Sanitize(metric, "metric")))
                    throw new StepArgumentException($"counter metric '{metric}' is given twice");
                Regex regex;
                try
                {
                    regex = new Regex(pattern, RegexOptions.CultureInvariant, MatchTimeout);
                }
                catch (ArgumentException ex)
                {
                    throw new StepArgumentException($"counter '{metric}': 'pattern' is not a regular expression: {ex.Message}");
                }
                if (mode != "count" && Array.IndexOf(regex.GetGroupNames(), "value") < 0)
                    throw new StepArgumentException($"counter '{metric}': mode '{mode}' reads the number in the group 'value', which the pattern does not have");
                counters.Add(new Counter { Metric = metric, Unit = Text(spec, "unit") ?? "count", Mode = mode, Pattern = regex });
            }
            return counters;
        }

        static string Text(JsonValue spec, string name) => spec.TryGet(name, out var value) && value.Kind == JsonKind.String ? value.AsString : null;

        static string WindowKey(string name) => "counter:" + name;

        static void Unverified(StepContext context, string note)
        {
            if (!context.Run.Unverified.Contains(note))
                context.Run.Unverified.Add(note);
        }
    }
}
