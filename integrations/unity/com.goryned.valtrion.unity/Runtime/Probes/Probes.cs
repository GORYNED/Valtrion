using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;
using Valtrion.Unity.Harness;
using Valtrion.Unity.Json;
using Unity.Profiling;
using UnityEngine;

namespace Valtrion.Unity.Probes
{
    /// <summary>Where a probe puts its samples.</summary>
    public interface IMetricSink
    {
        void Add(string metric, string unit, double value);

        /// <summary>Also report the 99th percentile of this metric over the probe window as the metric <c>&lt;name&gt;.p99</c>, one sample per window.</summary>
        void RequestP99(string metric);
    }

    /// <summary>One measurement source inside a probe window (<c>probe.start</c> … <c>probe.stop</c>).</summary>
    public interface IProbe : IDisposable
    {
        string Name { get; }

        /// <summary>Starts measuring. Returns one note per metric that is unavailable and will be missing from the result; empty when all is available.</summary>
        IReadOnlyList<string> Start(IMetricSink sink);

        /// <summary>Called once per distinct frame while the window is open.</summary>
        void Sample(IMetricSink sink, long frame, double nowMs);

        void Stop(IMetricSink sink);
    }

    /// <summary>The open measurement window of a run: its probes, and the p99 bookkeeping.</summary>
    public sealed class ProbeWindow : IMetricSink
    {
        readonly List<IProbe> _probes;
        readonly MetricStore _store;
        readonly Dictionary<string, string> _p99Units = new Dictionary<string, string>();
        readonly Dictionary<string, List<double>> _p99Samples = new Dictionary<string, List<double>>();
        long _lastFrame = long.MinValue;

        public ProbeWindow(List<IProbe> probes, MetricStore store)
        {
            _probes = probes;
            _store = store;
        }

        public IReadOnlyList<IProbe> Probes => _probes;

        public List<string> Start()
        {
            var notes = new List<string>();
            foreach (var probe in _probes)
            {
                foreach (var note in probe.Start(this))
                    notes.Add($"probe {probe.Name}: {note}");
            }
            return notes;
        }

        public void Tick(long frame, double nowMs)
        {
            if (frame == _lastFrame)
                return;
            _lastFrame = frame;
            foreach (var probe in _probes)
                probe.Sample(this, frame, nowMs);
        }

        /// <summary>Disposes every probe without collecting; used when a probe failed to start.</summary>
        public void Abandon()
        {
            foreach (var probe in _probes)
            {
                try
                {
                    probe.Dispose();
                }
                catch (Exception)
                {
                    // Nothing more can be done for a probe that cannot dispose.
                }
            }
        }

        public void Stop()
        {
            Exception first = null;
            foreach (var probe in _probes)
            {
                try
                {
                    probe.Stop(this);
                }
                catch (Exception ex)
                {
                    first = first ?? ex;
                }
                finally
                {
                    try
                    {
                        probe.Dispose();
                    }
                    catch (Exception ex)
                    {
                        first = first ?? ex;
                    }
                }
            }
            foreach (var pair in _p99Samples)
            {
                if (pair.Value.Count == 0)
                    continue;
                pair.Value.Sort();
                _store.Add(pair.Key + ".p99", _p99Units[pair.Key], Statistics.Percentile(pair.Value, 99));
            }
            if (first != null)
                throw first;
        }

        public void Add(string metric, string unit, double value)
        {
            _store.Add(metric, unit, value);
            if (_p99Samples.TryGetValue(metric, out var list) && !double.IsNaN(value) && !double.IsInfinity(value))
            {
                _p99Units[metric] = unit;
                list.Add(value);
            }
        }

        public void RequestP99(string metric)
        {
            if (!_p99Samples.ContainsKey(metric))
                _p99Samples.Add(metric, new List<double>());
            _p99Units[metric] = "count";
        }
    }

    /// <summary>Frame time from the wall clock (<c>frameTime.deltaMs</c>, always available) and from <c>FrameTimingManager</c>
    /// (<c>frameTime.cpuMs</c>, <c>frameTime.gpuMs</c>). When frame timing statistics are disabled the last two are reported as unavailable, never as zero.</summary>
    public sealed class FrameTimingProbe : IProbe
    {
        readonly IClock _clock;
        readonly Func<bool> _statsEnabled;
        FrameTiming[] _timings;
        bool _enabled;
        long _lastFrame = long.MinValue;
        double _lastMs;

        public FrameTimingProbe(IClock clock, Func<bool> statsEnabled = null)
        {
            _clock = clock;
            _statsEnabled = statsEnabled ?? FrameTimingManager.IsFeatureEnabled;
        }

        public string Name => "frameTiming";

        public IReadOnlyList<string> Start(IMetricSink sink)
        {
            var notes = new List<string>();
            sink.RequestP99("frameTime.deltaMs");
            _enabled = _statsEnabled();
            if (_enabled)
            {
                _timings = new FrameTiming[1];
                sink.RequestP99("frameTime.cpuMs");
                sink.RequestP99("frameTime.gpuMs");
            }
            else
            {
                notes.Add("frameTime.cpuMs and frameTime.gpuMs unavailable: frame timing statistics are disabled (Player Settings, Frame Timing Stats)");
            }
            _lastFrame = _clock.FrameCount;
            _lastMs = _clock.NowMs;
            return notes;
        }

        public void Sample(IMetricSink sink, long frame, double nowMs)
        {
            if (frame > _lastFrame)
                sink.Add("frameTime.deltaMs", "ms", (nowMs - _lastMs) / (frame - _lastFrame));
            _lastFrame = frame;
            _lastMs = nowMs;
            if (!_enabled)
                return;
            FrameTimingManager.CaptureFrameTimings();
            if (FrameTimingManager.GetLatestTimings(1, _timings) == 0)
                return;
            if (_timings[0].cpuFrameTime > 0)
                sink.Add("frameTime.cpuMs", "ms", _timings[0].cpuFrameTime);
            if (_timings[0].gpuFrameTime > 0)
                sink.Add("frameTime.gpuMs", "ms", _timings[0].gpuFrameTime);
        }

        public void Stop(IMetricSink sink)
        {
        }

        public void Dispose()
        {
        }
    }

    /// <summary>Per-frame values of the Unity profiler counters: GC allocated in frame, main thread time, total and system used memory.</summary>
    public sealed class ProfilerRecorderProbe : IProbe
    {
        sealed class Counter
        {
            public string Metric;
            public string Unit;
            public ProfilerCategory Category;
            public string Stat;
            public double Scale;
            public ProfilerRecorder Recorder;
        }

        readonly Counter[] _counters =
        {
            new Counter { Metric = "gc.allocBytes", Unit = "bytes", Category = ProfilerCategory.Memory, Stat = "GC Allocated In Frame", Scale = 1 },
            new Counter { Metric = "mainThread.ms", Unit = "ms", Category = ProfilerCategory.Internal, Stat = "Main Thread", Scale = 1e-6 },
            new Counter { Metric = "memory.totalUsedMB", Unit = "MB", Category = ProfilerCategory.Memory, Stat = "Total Used Memory", Scale = 1.0 / (1024 * 1024) },
            new Counter { Metric = "memory.systemUsedMB", Unit = "MB", Category = ProfilerCategory.Memory, Stat = "System Used Memory", Scale = 1.0 / (1024 * 1024) },
        };

        public string Name => "profiler";

        public IReadOnlyList<string> Start(IMetricSink sink)
        {
            var notes = new List<string>();
            foreach (var counter in _counters)
            {
                counter.Recorder = ProfilerRecorder.StartNew(counter.Category, counter.Stat, 1);
                if (!counter.Recorder.Valid)
                    notes.Add($"{counter.Metric} unavailable: profiler counter '{counter.Stat}' does not exist in this player");
                else if (counter.Metric == "mainThread.ms")
                    sink.RequestP99(counter.Metric);
            }
            return notes;
        }

        public void Sample(IMetricSink sink, long frame, double nowMs)
        {
            foreach (var counter in _counters)
            {
                if (counter.Recorder.Valid && counter.Recorder.Count > 0)
                    sink.Add(counter.Metric, counter.Unit, counter.Recorder.LastValue * counter.Scale);
            }
        }

        public void Stop(IMetricSink sink)
        {
        }

        public void Dispose()
        {
            foreach (var counter in _counters)
            {
                if (counter.Recorder.Valid)
                    counter.Recorder.Dispose();
            }
        }
    }

    /// <summary>One sample, in milliseconds, for each window: the time from <c>probe.start</c> to <c>probe.stop</c>.</summary>
    public sealed class TimerProbe : IProbe
    {
        readonly IClock _clock;
        readonly string _metric;
        double _start;

        public TimerProbe(string metric, IClock clock)
        {
            _metric = metric;
            _clock = clock;
        }

        public string Name => "timer:" + _metric;

        public IReadOnlyList<string> Start(IMetricSink sink)
        {
            _start = _clock.NowMs;
            return Array.Empty<string>();
        }

        public void Sample(IMetricSink sink, long frame, double nowMs)
        {
        }

        public void Stop(IMetricSink sink) => sink.Add(_metric, "ms", _clock.NowMs - _start);

        public void Dispose()
        {
        }
    }

    /// <summary>Turns log lines that match a regular expression into samples: the number in the group <c>value</c>, or 1 per line when there is none.</summary>
    public sealed class LogTagProbe : IProbe
    {
        readonly string _metric;
        readonly Regex _pattern;
        readonly string _unit;
        readonly LogTap _tap;
        readonly object _gate = new object();
        readonly List<double> _values = new List<double>();
        bool _subscribed;

        public LogTagProbe(string metric, Regex pattern, string unit, LogTap tap)
        {
            _metric = metric;
            _pattern = pattern;
            _unit = unit;
            _tap = tap;
        }

        public string Name => "log:" + _metric;

        public IReadOnlyList<string> Start(IMetricSink sink)
        {
            _tap.LineReceived += OnLine;
            _subscribed = true;
            return Array.Empty<string>();
        }

        public void Sample(IMetricSink sink, long frame, double nowMs) => Drain(sink);

        public void Stop(IMetricSink sink)
        {
            Unsubscribe();
            Drain(sink);
        }

        public void Dispose() => Unsubscribe();

        void Unsubscribe()
        {
            if (_subscribed)
                _tap.LineReceived -= OnLine;
            _subscribed = false;
        }

        void OnLine(string line)
        {
            var match = _pattern.Match(line);
            if (!match.Success)
                return;
            var value = 1.0;
            var group = match.Groups["value"];
            if (group.Success && double.TryParse(group.Value, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed))
                value = parsed;
            lock (_gate)
                _values.Add(value);
        }

        void Drain(IMetricSink sink)
        {
            double[] taken;
            lock (_gate)
            {
                taken = _values.ToArray();
                _values.Clear();
            }
            foreach (var value in taken)
                sink.Add(_metric, _unit, value);
        }
    }

    /// <summary>Measures the time between two log lines inside the probe window: from a line that matches <c>start</c> to the next line that matches
    /// <c>end</c>, one sample (milliseconds on the run clock) per pair. It measures what happens inside a step that cannot be split, for example the time
    /// a recording takes to finish after the stop request when both are logged. A second start line before the end line restarts the span (the latest
    /// start counts); an end line without a start line, and a start line that is never followed by an end line, give no sample.</summary>
    public sealed class LogSpanProbe : IProbe
    {
        readonly string _metric;
        readonly Regex _start;
        readonly Regex _end;
        readonly LogTap _tap;
        readonly IClock _clock;
        readonly object _gate = new object();
        readonly List<double> _values = new List<double>();
        double? _startedMs;
        bool _subscribed;

        public LogSpanProbe(string metric, Regex start, Regex end, LogTap tap, IClock clock)
        {
            _metric = metric;
            _start = start;
            _end = end;
            _tap = tap;
            _clock = clock;
        }

        public string Name => "logSpan:" + _metric;

        public IReadOnlyList<string> Start(IMetricSink sink)
        {
            _tap.LineReceived += OnLine;
            _subscribed = true;
            return Array.Empty<string>();
        }

        public void Sample(IMetricSink sink, long frame, double nowMs) => Drain(sink);

        public void Stop(IMetricSink sink)
        {
            Unsubscribe();
            Drain(sink);
        }

        public void Dispose() => Unsubscribe();

        void Unsubscribe()
        {
            if (_subscribed)
                _tap.LineReceived -= OnLine;
            _subscribed = false;
        }

        void OnLine(string line)
        {
            var now = _clock.NowMs;
            lock (_gate)
            {
                if (_startedMs.HasValue && _end.IsMatch(line))
                {
                    _values.Add(now - _startedMs.Value);
                    _startedMs = null;
                }
                else if (_start.IsMatch(line))
                {
                    _startedMs = now;
                }
            }
        }

        void Drain(IMetricSink sink)
        {
            double[] taken;
            lock (_gate)
            {
                taken = _values.ToArray();
                _values.Clear();
            }
            foreach (var value in taken)
                sink.Add(_metric, "ms", value);
        }
    }

    public delegate IProbe ProbeBuilder(JsonValue spec, RunContext run);

    /// <summary>Creates probes from the entries of the <c>probes</c> argument of <c>probe.start</c>. An entry is a name
    /// (<c>"frameTiming"</c>, <c>"profiler"</c>, <c>"timer:name"</c>, <c>"log:TAG"</c>; <c>logSpan</c> is an object) or an object with <c>type</c> and the type's properties.
    /// A project adds probe types with <see cref="Register"/>.</summary>
    public static class ProbeFactory
    {
        static readonly Dictionary<string, ProbeBuilder> Builders = new Dictionary<string, ProbeBuilder>(StringComparer.Ordinal)
        {
            { "frameTiming", (spec, run) => new FrameTimingProbe(run.Clock) },
            { "profiler", (spec, run) => new ProfilerRecorderProbe() },
            { "memory", (spec, run) => new ProfilerRecorderProbe() },
            { "timer", (spec, run) => new TimerProbe(Required(spec, "name"), run.Clock) },
            { "log", BuildLog },
            { "logSpan", BuildLogSpan },
        };

        public static void Register(string type, ProbeBuilder builder) => Builders[type] = builder ?? throw new ArgumentNullException(nameof(builder));

        public static IProbe Create(JsonValue spec, RunContext run)
        {
            if (spec.Kind == JsonKind.String)
            {
                var text = spec.AsString;
                var colon = text.IndexOf(':');
                spec = colon < 0
                    ? JsonValue.NewObject().Set("type", JsonValue.From(text))
                    : JsonValue.NewObject().Set("type", JsonValue.From(text.Substring(0, colon))).Set("name", JsonValue.From(text.Substring(colon + 1)));
            }
            if (spec.Kind != JsonKind.Object || !spec.TryGet("type", out var type) || type.Kind != JsonKind.String)
                throw new StepArgumentException("a probe is a name or an object with a 'type'");
            if (!Builders.TryGetValue(type.AsString, out var builder))
                throw new StepArgumentException($"unknown probe type '{type.AsString}'");
            return builder(spec, run);
        }

        static IProbe BuildLog(JsonValue spec, RunContext run)
        {
            var name = Required(spec, "name");
            var pattern = Optional(spec, "pattern") ?? Regex.Escape(name) + @"(?:\D*?(?<value>-?\d+(?:\.\d+)?))?";
            Regex regex;
            try
            {
                regex = new Regex(pattern, RegexOptions.CultureInvariant);
            }
            catch (ArgumentException ex)
            {
                throw new StepArgumentException($"log probe pattern is not a regular expression: {ex.Message}");
            }
            return new LogTagProbe(Optional(spec, "metric") ?? "log." + name, regex, Optional(spec, "unit") ?? "count", run.Logs);
        }

        static IProbe BuildLogSpan(JsonValue spec, RunContext run)
        {
            var metric = Required(spec, "metric");
            return new LogSpanProbe(metric, CompilePattern(Required(spec, "start"), "start"), CompilePattern(Required(spec, "end"), "end"), run.Logs, run.Clock);
        }

        static Regex CompilePattern(string pattern, string what)
        {
            try
            {
                return new Regex(pattern, RegexOptions.CultureInvariant);
            }
            catch (ArgumentException ex)
            {
                throw new StepArgumentException($"logSpan probe '{what}' is not a regular expression: {ex.Message}");
            }
        }

        static string Optional(JsonValue spec, string name) =>
            spec.TryGet(name, out var value) && value.Kind == JsonKind.String ? value.AsString : null;

        static string Required(JsonValue spec, string name) =>
            Optional(spec, name) ?? throw new StepArgumentException($"probe '{name}' is missing");
    }
}
