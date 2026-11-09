using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;

namespace Valtrion.Unity.Harness
{
    /// <summary>Reads the free space of the device in bytes. A negative value means "unknown" and is never treated as zero.</summary>
    public delegate long FreeSpaceReader(StepContext context);

    /// <summary>The free-space readers of a project, by name (the project's own call, for example its device storage API). The app registers
    /// them before the plan runs, on the main thread.</summary>
    public sealed class StorageReaders
    {
        public static readonly StorageReaders Shared = new StorageReaders();

        readonly Dictionary<string, FreeSpaceReader> _readers = new Dictionary<string, FreeSpaceReader>(StringComparer.Ordinal);

        public void Register(string name, FreeSpaceReader reader) => _readers[name] = reader ?? throw new ArgumentNullException(nameof(reader));

        public bool Unregister(string name) => _readers.Remove(name);

        public bool TryGet(string name, out FreeSpaceReader reader) => _readers.TryGetValue(name, out reader);
    }

    /// <summary>Steps that measure storage and the time a stop takes. They report numbers and apply no threshold.</summary>
    public static class StorageSteps
    {
        sealed class Window
        {
            public long FreeBefore;
            public double StartMs;
            public string Iteration;
        }

        sealed class Stamp
        {
            public double StartMs;
            public string Iteration;
        }

        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("storage.begin", Begin);
            registry.Register("storage.end", End);
            registry.Register("timer.start", TimerStart);
            registry.Register("timer.stop", TimerStop);
        }

        /// <summary><c>{ "reader": "free", "prefix": "storage", "required": false }</c> reads the free space through the reader registered under
        /// <c>reader</c>, reports <c>&lt;prefix&gt;.freeBeforeBytes</c> and opens a window for <c>storage.end</c>. A reader that is not registered ends the
        /// step BLOCKED. A reader that says "unknown" (a negative value) reports nothing and is listed as unverified, or ends the step BLOCKED when
        /// <c>required</c> is true. <c>prefix</c> defaults to <c>storage</c>.</summary>
        public static IEnumerator Begin(StepContext context)
        {
            var prefix = Prefix(context);
            var key = WindowKey(prefix);
            if (!TryReader(context, out var reader))
                yield break;
            var identity = Identity(context);
            if (context.Run.State.TryGetValue(key, out var open))
            {
                if (((Window)open).Iteration == identity)
                {
                    context.Fail($"storage window '{prefix}' is already open: call storage.end first");
                    yield break;
                }
                Unverified(context, $"storage window '{prefix}' of an earlier iteration was never closed and was replaced");
            }
            var free = reader(context);
            if (free < 0)
            {
                var note = $"free space before '{prefix}' is unknown (the reader returned {free.ToString(CultureInfo.InvariantCulture)})";
                if (context.Bool("required", false))
                {
                    context.Blocked(note);
                    yield break;
                }
                Unverified(context, note);
            }
            else
            {
                context.AddMetric(prefix + ".freeBeforeBytes", "bytes", free);
            }
            context.Run.State[key] = new Window { FreeBefore = free, StartMs = context.Clock.NowMs, Iteration = identity };
        }

        /// <summary><c>{ "reader": "free", "prefix": "storage", "seconds": 10, "required": false }</c> closes the window of <c>storage.begin</c>: reports
        /// <c>&lt;prefix&gt;.freeAfterBytes</c>, <c>&lt;prefix&gt;.usedBytes</c> (free before minus free after; negative when space came back) and
        /// <c>&lt;prefix&gt;.bytesPerSecond</c> (used bytes divided by <c>seconds</c>, or by the time between the two steps when <c>seconds</c> is not given).
        /// A value that is unknown on either side leaves out what depends on it and is listed as unverified, or ends the step BLOCKED with
        /// <c>required</c>. Without an open window the step ends FAIL.</summary>
        public static IEnumerator End(StepContext context)
        {
            var prefix = Prefix(context);
            var key = WindowKey(prefix);
            var seconds = context.Args.TryGet("seconds", out _) ? context.Number("seconds") : (double?)null;
            if (seconds.HasValue && !(seconds.Value > 0))
                throw new StepArgumentException("'seconds' must be greater than zero");
            if (!TryReader(context, out var reader))
                yield break;
            if (!context.Run.State.TryGetValue(key, out var open))
            {
                context.Fail($"no storage window '{prefix}' is open: call storage.begin first");
                yield break;
            }
            var window = (Window)open;
            if (window.Iteration != Identity(context))
            {
                context.Run.State.Remove(key);
                context.Fail($"storage window '{prefix}' was opened in another iteration (a step failed before storage.end?): call storage.begin in this iteration");
                yield break;
            }
            var elapsedSeconds = seconds ?? (context.Clock.NowMs - window.StartMs) / 1000.0;
            var free = reader(context);
            context.Run.State.Remove(key);
            var unknown = new List<string>();
            if (free < 0)
                unknown.Add($"free space after '{prefix}' is unknown (the reader returned {free.ToString(CultureInfo.InvariantCulture)})");
            else
                context.AddMetric(prefix + ".freeAfterBytes", "bytes", free);
            if (window.FreeBefore < 0)
                unknown.Add($"free space before '{prefix}' was unknown");
            if (unknown.Count == 0)
            {
                var used = (double)window.FreeBefore - free;
                context.AddMetric(prefix + ".usedBytes", "bytes", used);
                if (elapsedSeconds > 0)
                    context.AddMetric(prefix + ".bytesPerSecond", "B/s", used / elapsedSeconds);
                else
                    Unverified(context, $"bytes per second of '{prefix}' was not reported: no time passed between storage.begin and storage.end");
            }
            if (unknown.Count == 0)
                yield break;
            var text = string.Join("; ", unknown);
            if (context.Bool("required", false))
                context.Blocked(text);
            else
                Unverified(context, text);
        }

        /// <summary><c>{ "name": "stop" }</c> remembers now as the start of the timer <c>name</c>, for <c>timer.stop</c>. Starting a timer again restarts it.</summary>
        public static IEnumerator TimerStart(StepContext context)
        {
            context.Run.State[TimerKey(context.String("name"))] = new Stamp { StartMs = context.Clock.NowMs, Iteration = Identity(context) };
            yield break;
        }

        /// <summary><c>{ "name": "stop", "metric": "finishMs", "until": "file.ready" }</c> reports the milliseconds since <c>timer.start</c> of the same
        /// <c>name</c> as the metric <c>metric</c>. With <c>until</c> it first waits for the named condition of the <see cref="ConditionRegistry"/> (as
        /// <c>waitUntil</c> does), so the metric is the time a project operation took to finish, for example from a stop request to a finished file.
        /// A timer that was not started ends FAIL; a condition that is not registered ends BLOCKED.</summary>
        public static IEnumerator TimerStop(StepContext context)
        {
            var name = context.String("name");
            var metric = context.String("metric");
            Func<bool> condition = null;
            if (context.Args.TryGet("until", out _))
            {
                var conditionName = context.String("until");
                if (!context.Run.Conditions.TryGet(conditionName, out condition))
                {
                    context.Blocked($"condition '{conditionName}' is not registered in this build");
                    yield break;
                }
                context.Note($"waiting for condition '{conditionName}'");
            }
            if (!context.Run.State.TryGetValue(TimerKey(name), out var started))
            {
                context.Fail($"timer '{name}' was not started: call timer.start first");
                yield break;
            }
            var stamp = (Stamp)started;
            if (stamp.Iteration != Identity(context))
            {
                context.Run.State.Remove(TimerKey(name));
                context.Fail($"timer '{name}' was started in another iteration (a step failed before timer.stop?): call timer.start in this iteration");
                yield break;
            }
            while (condition != null)
            {
                bool met;
                try
                {
                    met = condition();
                }
                catch (Exception ex)
                {
                    context.Fail($"condition threw {ex.GetType().Name}: {ex.Message}{Faults.FirstFrame(ex)}");
                    yield break;
                }
                if (met)
                    break;
                yield return null;
            }
            context.Run.State.Remove(TimerKey(name));
            context.AddMetric(metric, "ms", context.Clock.NowMs - stamp.StartMs);
        }

        static string Identity(StepContext context) => context.Phase + ":" + context.Iteration;

        /// <summary>Lists a note as unverified once: a repeated plan would otherwise repeat the same text for every iteration.</summary>
        static void Unverified(StepContext context, string note)
        {
            if (!context.Run.Unverified.Contains(note))
                context.Run.Unverified.Add(note);
        }

        static string Prefix(StepContext context) => Identifiers.Sanitize(context.String("prefix", "storage"), "storage");

        static string WindowKey(string prefix) => "storage:" + prefix;

        static string TimerKey(string name) => "timer:" + name;

        static bool TryReader(StepContext context, out FreeSpaceReader reader)
        {
            var name = context.String("reader");
            if (StorageReaders.Shared.TryGet(name, out reader))
                return true;
            context.Blocked($"storage reader '{name}' is not registered in this build");
            return false;
        }
    }
}
