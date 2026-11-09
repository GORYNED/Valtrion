using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using Valtrion.Unity.Contracts;

namespace Valtrion.Unity.Harness
{
    /// <summary>The result of a project call that <c>call.time</c> measured. Return null from the call when it has nothing to add.</summary>
    public sealed class ProjectCallResult
    {
        /// <summary>The call worked but its answer is unknown (for example a free-space read that says "unknown"). The time is still reported, and the note is listed as unverified.</summary>
        public static ProjectCallResult Unknown(string note) => new ProjectCallResult { IsUnknown = true, Note = note };

        public bool IsUnknown { get; private set; }

        public string Note { get; private set; }
    }

    /// <summary>A project call the plan can time. It runs synchronously on the main thread.</summary>
    public delegate ProjectCallResult ProjectCall(StepContext context);

    /// <summary>The project calls <c>call.time</c> can run, by name. The app registers them before the plan runs, on the main thread.</summary>
    public sealed class ProjectCalls
    {
        public static readonly ProjectCalls Shared = new ProjectCalls();

        readonly Dictionary<string, ProjectCall> _calls = new Dictionary<string, ProjectCall>(StringComparer.Ordinal);

        public void Register(string name, ProjectCall call) => _calls[name] = call ?? throw new ArgumentNullException(nameof(call));

        public bool Unregister(string name) => _calls.Remove(name);

        public bool TryGet(string name, out ProjectCall call) => _calls.TryGetValue(name, out call);
    }

    /// <summary>Steps that occupy free space of the device with a file the run owns, and measure the duration of a project call. The fill changes the state of
    /// the device, so it refuses to start without a way to read free space and a floor that must stay free, never touches a file it did not create, and is
    /// removed by the cleanup ledger when the run ends however it ends; a file left by a killed app is removed by the next launch.</summary>
    public static class FillSteps
    {
        /// <summary>The folder below the work folder where fill files live. Nothing else of the harness writes there.</summary>
        public const string FolderName = "fill";

        const string FilePrefix = "gdv-fill-";
        const string FileSuffix = ".bin";
        const string StateKey = "fill:file";
        const long DefaultChunkBytes = 16L * 1024 * 1024;
        const int DefaultMaxAdjustments = 10;

        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("storage.fill", Fill);
            registry.Register("storage.release", Release);
            registry.Register("call.time", CallTime);
        }

        static string FolderOf(RunPaths paths) => Path.Combine(paths.Work, FolderName);

        /// <summary>Removes the fill files that earlier launches left (a killed app, a lost connection, a cancelled run) and returns what it removed. Only files named
        /// like the fill files of this harness, directly in the fill folder, are touched; the file of <paramref name="keepRunId"/> is left to the cleanup ledger.</summary>
        public static List<string> SweepOrphans(RunPaths paths, string keepRunId)
        {
            var removed = new List<string>();
            var folder = FolderOf(paths);
            if (!Directory.Exists(folder))
                return removed;
            var keep = keepRunId == null ? null : FileName(keepRunId);
            foreach (var file in Directory.GetFiles(folder, FilePrefix + "*" + FileSuffix, SearchOption.TopDirectoryOnly))
            {
                if (string.Equals(Path.GetFileName(file), keep, StringComparison.Ordinal))
                    continue;
                try
                {
                    File.Delete(file);
                    removed.Add(Path.GetFileName(file));
                }
                catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
                {
                    removed.Add(Path.GetFileName(file) + " (could not be removed: " + ex.Message + ")");
                }
            }
            return removed;
        }

        static string FileName(string runId) => FilePrefix + Identifiers.FileSafe(runId) + FileSuffix;

        /// <summary><c>{ "reader": "free", "reserveBytes": 536870912, "leaveFreeBytes": 1073741824, "toleranceBytes": 1048576 }</c> writes a file of its own until that much
        /// free space is left, or <c>{ "reader": "free", "reserveBytes": 536870912, "bytes": 1073741824 }</c> writes exactly that many bytes. The step has no defaults for these:
        /// <c>reserveBytes</c> is the floor that must stay free (the fill never takes the free space below it: it refuses to start when the target is below it and stops when a write would cross it), and
        /// <c>toleranceBytes</c> says how far the free space may be from <c>leaveFreeBytes</c> when it stops. The free space is read again after every write because the
        /// system may reclaim or take space meanwhile; the step writes more when it is above the target by more than the tolerance, and cuts the file when it is below by
        /// more than the tolerance, at most <c>maxAdjustments</c> times (default 10). <c>chunkBytes</c> (default 16 MiB) is how much one frame writes. The file is
        /// recorded in the cleanup ledger before the first byte is written. It ends BLOCKED, writing nothing, when the reader is not registered or returns unknown
        /// (a negative number), when the free space is already below the target or the floor, when a fill file of this run exists, or when the fill folder holds a
        /// file this harness did not create. Reports <c>fill.bytesWritten</c> and <c>fill.freeAfterBytes</c>.</summary>
        public static IEnumerator Fill(StepContext context)
        {
            var readerName = context.String("reader");
            var reserve = Bytes(context, "reserveBytes");
            var hasBytes = context.Args.TryGet("bytes", out _);
            var hasLeave = context.Args.TryGet("leaveFreeBytes", out _);
            if (hasBytes == hasLeave)
                throw new StepArgumentException("give exactly one of 'bytes' and 'leaveFreeBytes'");
            long? bytes = hasBytes ? Bytes(context, "bytes") : (long?)null;
            long? leave = hasLeave ? Bytes(context, "leaveFreeBytes") : (long?)null;
            long tolerance = 0;
            if (hasLeave)
                tolerance = Bytes(context, "toleranceBytes");
            var chunk = context.Args.TryGet("chunkBytes", out _) ? Bytes(context, "chunkBytes") : DefaultChunkBytes;
            if (chunk < 1)
                throw new StepArgumentException("'chunkBytes' must be at least 1");
            var maxAdjustments = (int)(context.Args.TryGet("maxAdjustments", out _) ? Bytes(context, "maxAdjustments") : DefaultMaxAdjustments);
            if (hasLeave && leave.Value < reserve)
                throw new StepArgumentException("'leaveFreeBytes' must not be below 'reserveBytes'");
            if (!StorageReaders.Shared.TryGet(readerName, out var reader))
            {
                context.Blocked($"storage reader '{readerName}' is not registered in this build");
                yield break;
            }
            var folder = FolderOf(context.Run.Paths);
            var path = Path.Combine(folder, FileName(context.Run.RunId));
            if (context.Run.State.ContainsKey(StateKey) || File.Exists(path))
            {
                context.Blocked("a fill file of this run exists: call storage.release first");
                yield break;
            }
            var foreign = ForeignFile(folder);
            if (foreign != null)
            {
                context.Blocked($"the fill folder holds '{foreign}', which this harness did not create: it is not touched and nothing is written");
                yield break;
            }
            var free = reader(context);
            if (free < 0)
            {
                context.Blocked("free space is unknown (the reader returned " + free.ToString(CultureInfo.InvariantCulture) + "): without it the floor cannot be kept, so nothing is written");
                yield break;
            }
            var target = hasLeave ? leave.Value : free - bytes.Value;
            if (target < reserve)
            {
                context.Blocked($"the fill would leave {target.ToString(CultureInfo.InvariantCulture)} bytes free, below the floor of {reserve.ToString(CultureInfo.InvariantCulture)}: nothing is written");
                yield break;
            }
            if (hasLeave && free < target - tolerance)
            {
                context.Blocked($"only {free.ToString(CultureInfo.InvariantCulture)} bytes are free, already below the target of {target.ToString(CultureInfo.InvariantCulture)}: nothing is written");
                yield break;
            }
            Directory.CreateDirectory(folder);
            // Record the file before the first byte: a run that ends in the middle of the write still removes it.
            context.Run.Ledger.Record(FileCleanupHandler.FileKind, path);
            context.Run.State[StateKey] = path;
            long written = 0;
            var adjustments = 0;
            var floorReached = false;
            // The file is opened for every write and closed again: no handle is held while the step waits for the next frame, so the cleanup of a run that is
            // cut off in the middle (cancelled, connection lost) can always delete it, also where an open file cannot be deleted.
            using (new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            {
            }
            {
                var buffer = new byte[(int)Math.Min(chunk, 1L << 20)];
                while (true)
                {
                    free = reader(context);
                    if (free < 0)
                    {
                        context.Blocked("free space became unknown while the file was written: the fill stopped and the file is removed by the cleanup");
                        yield break;
                    }
                    if (hasBytes)
                    {
                        if (written >= bytes.Value)
                            break;
                        var wanted = Math.Min(chunk, bytes.Value - written);
                        // Never take the free space below the floor, whatever was asked for.
                        if (free - wanted < reserve)
                        {
                            floorReached = true;
                            break;
                        }
                        WriteBytes(path, buffer, wanted);
                        written += wanted;
                    }
                    else
                    {
                        var difference = free - target;
                        if (Math.Abs(difference) <= tolerance)
                            break;
                        if (difference > 0)
                        {
                            var wanted = Math.Min(Math.Min(chunk, difference), free - reserve);
                            if (wanted <= 0)
                                break;
                            WriteBytes(path, buffer, wanted);
                            written += wanted;
                        }
                        else
                        {
                            // The free space is below the target (more was taken than asked, or the system took space meanwhile): give some back.
                            if (adjustments >= maxAdjustments)
                                break;
                            adjustments++;
                            var cut = Math.Min(-difference, written);
                            if (cut <= 0)
                                break;
                            Truncate(path, written - cut);
                            written -= cut;
                        }
                    }
                    context.Note($"{written.ToString(CultureInfo.InvariantCulture)} bytes written");
                    yield return null;
                }
            }
            free = reader(context);
            context.AddMetric("fill.bytesWritten", "bytes", written);
            if (free >= 0)
                context.AddMetric("fill.freeAfterBytes", "bytes", free);
            else
                Unverified(context, "free space after the fill is unknown");
            if (floorReached)
                context.Fail($"the floor of {reserve.ToString(CultureInfo.InvariantCulture)} free bytes was reached after {written.ToString(CultureInfo.InvariantCulture)} of {bytes.Value.ToString(CultureInfo.InvariantCulture)} bytes; the file is removed by the cleanup");
            else if (hasLeave && free >= 0 && Math.Abs(free - target) > tolerance)
                context.Fail($"free space is {free.ToString(CultureInfo.InvariantCulture)} bytes, not within {tolerance.ToString(CultureInfo.InvariantCulture)} of the target {target.ToString(CultureInfo.InvariantCulture)} after {adjustments.ToString(CultureInfo.InvariantCulture)} adjustment(s); the file is removed by the cleanup");
            else
                context.Pass($"{written.ToString(CultureInfo.InvariantCulture)} bytes written to {Path.GetFileName(path)}");
        }

        /// <summary><c>{ }</c> removes the fill file of this run now (the cleanup at the end of the run would remove it anyway) and takes it out of the ledger. Nothing to
        /// release is not an error.</summary>
        public static IEnumerator Release(StepContext context)
        {
            if (!context.Run.State.TryGetValue(StateKey, out var path))
            {
                context.Pass("no fill file to release");
                yield break;
            }
            var file = (string)path;
            try
            {
                if (File.Exists(file))
                    File.Delete(file);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                context.Fail($"the fill file could not be removed now: {ex.Message}; the cleanup at the end of the run tries again");
                yield break;
            }
            context.Run.State.Remove(StateKey);
            context.Run.Ledger.Forget(FileCleanupHandler.FileKind, file);
            context.Pass("fill file released");
        }

        /// <summary><c>{ "call": "freeSpace.read", "metric": "freeSpaceReadMs" }</c> runs the project call registered under <c>call</c> once and reports how long it took in
        /// milliseconds as <c>metric</c>. Put it in a <c>repeat</c> step for a distribution. A call that returns <see cref="ProjectCallResult.Unknown"/> still has its time
        /// reported and the note is listed as unverified; a call that throws ends the step FAIL and reports nothing. A call that is not registered ends BLOCKED.</summary>
        public static IEnumerator CallTime(StepContext context)
        {
            var name = context.String("call");
            var metric = context.String("metric");
            if (!ProjectCalls.Shared.TryGet(name, out var call))
            {
                context.Blocked($"project call '{name}' is not registered in this build");
                yield break;
            }
            var start = context.Clock.NowMs;
            var result = call(context);
            var elapsed = context.Clock.NowMs - start;
            context.AddMetric(metric, "ms", elapsed);
            if (result != null && result.IsUnknown)
                Unverified(context, $"project call '{name}': {result.Note ?? "the answer is unknown"}");
        }

        static string ForeignFile(string folder)
        {
            if (!Directory.Exists(folder))
                return null;
            foreach (var entry in Directory.GetFileSystemEntries(folder))
            {
                var name = Path.GetFileName(entry);
                if (!(name.StartsWith(FilePrefix, StringComparison.Ordinal) && name.EndsWith(FileSuffix, StringComparison.Ordinal) && !Directory.Exists(entry)))
                    return name;
            }
            return null;
        }

        static void WriteBytes(string path, byte[] buffer, long count)
        {
            using (var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read))
            {
                while (count > 0)
                {
                    var part = (int)Math.Min(buffer.Length, count);
                    stream.Write(buffer, 0, part);
                    count -= part;
                }
                stream.Flush(true);
            }
        }

        static void Truncate(string path, long length)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Write, FileShare.Read))
            {
                stream.SetLength(length);
                stream.Flush(true);
            }
        }

        static long Bytes(StepContext context, string name)
        {
            var value = context.Number(name);
            if (value < 0 || value != Math.Floor(value) || value > 9007199254740991.0)
                throw new StepArgumentException($"'{name}' must be a whole number of at least 0");
            return (long)value;
        }

        static void Unverified(StepContext context, string note)
        {
            if (!context.Run.Unverified.Contains(note))
                context.Run.Unverified.Add(note);
        }
    }
}
