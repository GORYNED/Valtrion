using System;
using System.Collections;
using System.Collections.Generic;

namespace Valtrion.Unity.Harness
{
    /// <summary>One stream of a recorded session as the project's session reader exposes it.</summary>
    public sealed class StreamOffset
    {
        public StreamOffset(string stream, double offsetMs, double? firstTimestampMs = null)
        {
            Stream = stream;
            OffsetMs = offsetMs;
            FirstTimestampMs = firstTimestampMs;
        }

        /// <summary>The stream's name; it becomes a part of the metric name.</summary>
        public string Stream { get; }

        /// <summary>The persisted start offset of the stream, in milliseconds, exactly as stored.</summary>
        public double OffsetMs { get; }

        /// <summary>The first timestamp of the stream, in milliseconds on the clock the project stores, or null when the stream has none.</summary>
        public double? FirstTimestampMs { get; }
    }

    /// <summary>What a project's session reader returns for the session that was just recorded.</summary>
    public sealed class SessionOffsets
    {
        public List<StreamOffset> Streams { get; } = new List<StreamOffset>();

        /// <summary>The shared clock anchor of the session in milliseconds on the same clock as the first timestamps, or null when there is none.</summary>
        public double? AnchorMs { get; set; }
    }

    public delegate SessionOffsets SessionReader(StepContext context);

    /// <summary>The session readers of a project, by name. A reader is the project's own code that opens the stored session (its file or its
    /// reader class) and returns what is persisted; Valtrion invents no session format. The app registers readers before the plan runs.</summary>
    public sealed class SessionReaders
    {
        public static readonly SessionReaders Shared = new SessionReaders();

        readonly Dictionary<string, SessionReader> _readers = new Dictionary<string, SessionReader>(StringComparer.Ordinal);

        public void Register(string name, SessionReader reader) => _readers[name] = reader ?? throw new ArgumentNullException(nameof(reader));

        public bool Unregister(string name) => _readers.Remove(name);

        public bool TryGet(string name, out SessionReader reader) => _readers.TryGetValue(name, out reader);
    }

    /// <summary>Steps that read a recorded session.</summary>
    public static class SessionSteps
    {
        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("session.offsets", Offsets);
        }

        /// <summary><c>{ "reader": "session", "prefix": "session", "relativeTo": "pressure" }</c> asks the session reader registered under
        /// <c>reader</c> for the session just recorded and reports what is persisted as metrics, without judging them:
        /// <c>&lt;prefix&gt;.&lt;stream&gt;.offsetMs</c> and <c>&lt;prefix&gt;.&lt;stream&gt;.firstTimestampMs</c> for every stream,
        /// <c>&lt;prefix&gt;.anchorMs</c> and <c>&lt;prefix&gt;.&lt;stream&gt;.firstTimestampFromAnchorMs</c> when the reader gives an anchor,
        /// and <c>&lt;prefix&gt;.&lt;stream&gt;.offsetFromStreamMs</c> (the stream's offset minus the offset of the stream named by <c>relativeTo</c>) for
        /// every other stream. <c>prefix</c> defaults to <c>session</c>. A reader that is not registered ends the step BLOCKED; a session without
        /// streams, a repeated stream name, a non-finite value or an unknown <c>relativeTo</c> ends it FAIL and reports nothing.</summary>
        public static IEnumerator Offsets(StepContext context)
        {
            var readerName = context.String("reader");
            var prefix = Identifiers.Sanitize(context.String("prefix", "session"), "session");
            var relativeTo = context.Args.TryGet("relativeTo", out _) ? context.String("relativeTo") : null;
            if (!SessionReaders.Shared.TryGet(readerName, out var reader))
            {
                context.Blocked($"session reader '{readerName}' is not registered in this build");
                yield break;
            }
            var session = reader(context);
            if (session == null || session.Streams.Count == 0)
            {
                context.Fail($"session reader '{readerName}' returned no streams: nothing was recorded or the session cannot be read");
                yield break;
            }
            var seen = new HashSet<string>(StringComparer.Ordinal);
            StreamOffset relative = null;
            foreach (var stream in session.Streams)
            {
                if (string.IsNullOrEmpty(stream.Stream))
                {
                    context.Fail("a stream of the session has no name");
                    yield break;
                }
                var key = Identifiers.Sanitize(stream.Stream, "stream");
                if (!seen.Add(key))
                {
                    context.Fail($"the session has two streams named '{key}' once made into a metric name");
                    yield break;
                }
                if (!IsFinite(stream.OffsetMs) || (stream.FirstTimestampMs.HasValue && !IsFinite(stream.FirstTimestampMs.Value)))
                {
                    context.Fail($"stream '{stream.Stream}' has a value that is not a finite number");
                    yield break;
                }
                if (relativeTo != null && stream.Stream == relativeTo)
                    relative = stream;
            }
            if (session.AnchorMs.HasValue && !IsFinite(session.AnchorMs.Value))
            {
                context.Fail("the session anchor is not a finite number");
                yield break;
            }
            if (relativeTo != null && relative == null)
            {
                context.Fail($"'relativeTo' names the stream '{relativeTo}', which the session does not have");
                yield break;
            }
            if (session.AnchorMs.HasValue)
                context.AddMetric(prefix + ".anchorMs", "ms", session.AnchorMs.Value);
            foreach (var stream in session.Streams)
            {
                var name = prefix + "." + Identifiers.Sanitize(stream.Stream, "stream");
                context.AddMetric(name + ".offsetMs", "ms", stream.OffsetMs);
                if (stream.FirstTimestampMs.HasValue)
                {
                    context.AddMetric(name + ".firstTimestampMs", "ms", stream.FirstTimestampMs.Value);
                    if (session.AnchorMs.HasValue)
                        context.AddMetric(name + ".firstTimestampFromAnchorMs", "ms", stream.FirstTimestampMs.Value - session.AnchorMs.Value);
                }
                if (relative != null && !ReferenceEquals(stream, relative))
                    context.AddMetric(name + ".offsetFromStreamMs", "ms", stream.OffsetMs - relative.OffsetMs);
            }
            context.Pass($"{session.Streams.Count} streams read by '{readerName}'");
        }

        static bool IsFinite(double value) => !double.IsNaN(value) && !double.IsInfinity(value);
    }
}
