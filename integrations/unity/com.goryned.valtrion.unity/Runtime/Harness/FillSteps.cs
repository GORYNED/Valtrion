using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
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
    /// the device, so it refuses to start without a way to read free space and a floor that must stay free, never touches a file it did not create, and removes
    /// its file when the run ends however it ends (also with <c>cleanup: "none"</c>); a file left by a killed app is removed when the next run starts.</summary>
    public static class FillSteps
    {
        /// <summary>The folder below the work folder where fill files live. Nothing else of the harness writes there.</summary>
        public const string FolderName = "fill";

        const string FilePrefix = "gdv-fill-";
        const string FileSuffix = ".bin";
        const string StateKey = "fill:file";
        const long DefaultChunkBytes = 16L * 1024 * 1024;
        const int DefaultMaxAdjustments = 10;
        const int StaleReadingsLimit = 3;

        /// <summary>The first bytes of every fill file. The sweep deletes only a file that starts with them.</summary>
        static readonly byte[] Magic = Encoding.ASCII.GetBytes("VALTRION-FILL-1\n");

        // Fill files that a run in this process holds now: a second run of the same process must not sweep them away.
        static readonly HashSet<string> Active = new HashSet<string>(StringComparer.Ordinal);

        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("storage.fill", Fill);
            registry.Register("storage.release", Release);
            registry.Register("call.time", CallTime);
        }

        /// <summary>The marker every fill file starts with; a test that plants an orphan writes it first.</summary>
        public static byte[] Marker() => (byte[])Magic.Clone();

        static string FolderOf(RunPaths paths) => Path.Combine(paths.Work, FolderName);

        static string FileName(string runId) => FilePrefix + Identifiers.FileSafe(runId) + FileSuffix;

        /// <summary>Removes the fill files that earlier launches left (a killed app, a lost connection, a cancelled run) and describes what it did. Only files directly in the
        /// fill folder, named like the fill files of this harness and starting with its marker, are deleted; the file of a run that holds it in this process, a directory and
        /// every other file are left alone.</summary>
        public static List<string> SweepOrphans(RunPaths paths, string keepRunId)
        {
            var notes = new List<string>();
            var folder = FolderOf(paths);
            if (!Directory.Exists(folder))
                return notes;
            var keep = keepRunId == null ? null : FileName(keepRunId);
            foreach (var file in Directory.GetFiles(folder, FilePrefix + "*" + FileSuffix, SearchOption.TopDirectoryOnly))
            {
                var name = Path.GetFileName(file);
                if (string.Equals(name, keep, StringComparison.Ordinal) || IsActive(file))
                    continue;
                try
                {
                    if (!HasMagic(file))
                    {
                        notes.Add($"{name} was left alone: it does not start with the marker of the fill files");
                        continue;
                    }
                    File.Delete(file);
                    notes.Add($"removed {name}");
                }
                catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
                {
                    notes.Add($"could not remove {name}: {ex.Message}");
                }
            }
            return notes;
        }

        /// <summary>Removes the fill file of this run now, whatever the plan's <c>cleanup</c> says, and takes it out of the ledger. Returns what could not be removed.</summary>
        internal static List<string> RemoveOwned(RunContext run)
        {
            var problems = new List<string>();
            if (!run.State.TryGetValue(StateKey, out var path))
                return problems;
            var file = (string)path;
            try
            {
                if (File.Exists(file))
                    File.Delete(file);
                run.State.Remove(StateKey);
                run.Ledger.Forget(FileCleanupHandler.FileKind, file);
                Deactivate(file);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                problems.Add($"the fill file '{Path.GetFileName(file)}' could not be removed: {ex.Message}");
            }
            return problems;
        }

        static bool IsActive(string file)
        {
            lock (Active)
                return Active.Contains(Path.GetFullPath(file));
        }

        static void Activate(string file)
        {
            lock (Active)
                Active.Add(Path.GetFullPath(file));
        }

        static void Deactivate(string file)
        {
            lock (Active)
                Active.Remove(Path.GetFullPath(file));
        }

        static bool HasMagic(string file)
        {
            using (var stream = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                var head = new byte[Magic.Length];
                var read = 0;
                while (read < head.Length)
                {
                    var part = stream.Read(head, read, head.Length - read);
                    if (part <= 0)
                        return false;
                    read += part;
                }
                for (var i = 0; i < Magic.Length; i++)
                {
                    if (head[i] != Magic[i])
                        return false;
                }
                return true;
            }
        }

        /// <summary><c>{ "reader": "free", "reserveBytes": 536870912, "leaveFreeBytes": 1073741824, "toleranceBytes": 1048576 }</c> writes a file of its own until that much
        /// free space is left, or <c>{ "reader": "free", "reserveBytes": 536870912, "bytes": 1073741824 }</c> writes that many bytes (after a 16-byte marker). The step has
        /// no defaults for these: <c>reserveBytes</c> (at least 1) is the floor that must stay free (required with <c>bytes</c>; with <c>leaveFreeBytes</c> it may be left out and
        /// is then <c>leaveFreeBytes</c> minus <c>toleranceBytes</c>, at least 1: the lowest free space the tolerance admits), <c>toleranceBytes</c> (only with <c>leaveFreeBytes</c>)
        /// says how far the free space may be from the target when it stops. The free space is read again after every write because the system may reclaim or take space meanwhile:
        /// the step writes more when it is above the target by more than the tolerance and cuts the file when it is below by more than the tolerance (at most
        /// <c>maxAdjustments</c> times, default 10, at most 2147483647). Whenever a reading is below the floor, the file is cut back to the floor at once and the step ends
        /// FAIL. A reader that does not show the free space falling after three writes in a row is not trusted: the step ends FAIL. <c>staleBytes</c> replaces the three writes with an amount: how many bytes may be written
        /// while the reading does not fall (by at least half of what was written) before it is not trusted; for a reader that reports space the system can free again, whose reading stays on a plateau while
        /// it is reclaimed. The floor still cuts the file back whenever a reading is below it. <c>chunkBytes</c> (default 16 MiB)
        /// is how much one frame writes. The file is recorded in the cleanup ledger before it is created and removed when the run ends however it ends. It ends BLOCKED,
        /// writing nothing, when the reader is not registered or says unknown (a negative number), when the target is below the floor, when the free space is already
        /// below the target, when a fill file of this run exists, or when the fill folder holds a file this harness did not create. Reports <c>fill.bytesWritten</c> (the
        /// data, without the marker) and <c>fill.freeAfterBytes</c>.</summary>
        public static IEnumerator Fill(StepContext context)
        {
            var readerName = context.String("reader");
            var hasBytes = context.Args.TryGet("bytes", out _);
            var hasLeave = context.Args.TryGet("leaveFreeBytes", out _);
            if (hasBytes == hasLeave)
                throw new StepArgumentException("give exactly one of 'bytes' and 'leaveFreeBytes'");
            var hasTolerance = context.Args.TryGet("toleranceBytes", out _);
            if (hasBytes && hasTolerance)
                throw new StepArgumentException("'toleranceBytes' goes with 'leaveFreeBytes' only");
            if (hasLeave && !hasTolerance)
                throw new StepArgumentException("argument 'toleranceBytes' is required with 'leaveFreeBytes'");
            var bytes = hasBytes ? Bytes(context, "bytes") : 0;
            var leave = hasLeave ? Bytes(context, "leaveFreeBytes") : 0;
            var tolerance = hasLeave ? Bytes(context, "toleranceBytes") : 0;
            // The floor: given, or in leaveFreeBytes mode the lowest free space the tolerance admits. A fill without a floor is refused.
            long reserve;
            if (context.Args.TryGet("reserveBytes", out _))
            {
                reserve = Bytes(context, "reserveBytes");
                if (reserve < 1)
                    throw new StepArgumentException("'reserveBytes' must be at least 1: a fill without a floor is refused");
            }
            else if (hasLeave)
            {
                reserve = Math.Max(1, leave - tolerance);
            }
            else
            {
                throw new StepArgumentException("argument 'reserveBytes' is required with 'bytes'");
            }
            var chunk = context.Args.TryGet("chunkBytes", out _) ? Bytes(context, "chunkBytes") : DefaultChunkBytes;
            if (chunk < 1)
                throw new StepArgumentException("'chunkBytes' must be at least 1");
            var maxAdjustments = context.Args.TryGet("maxAdjustments", out _) ? Bytes(context, "maxAdjustments") : DefaultMaxAdjustments;
            if (maxAdjustments > int.MaxValue)
                throw new StepArgumentException("'maxAdjustments' must not be more than 2147483647");
            if (hasLeave && leave < reserve)
                throw new StepArgumentException("'leaveFreeBytes' must not be below 'reserveBytes'");
            // How much may be written while the reading does not fall before it is not trusted. Without staleBytes: three writes in a row.
            var hasStaleBytes = context.Args.TryGet("staleBytes", out _);
            var staleLimitBytes = hasStaleBytes ? Bytes(context, "staleBytes") : 0;
            if (hasStaleBytes && staleLimitBytes < 1)
                throw new StepArgumentException("'staleBytes' must be at least 1");
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
                context.Blocked("free space is unknown (the reader returned " + Text(free) + "): without it the floor cannot be kept, so nothing is written");
                yield break;
            }
            var target = hasLeave ? leave : free - bytes;
            if (target < reserve)
            {
                context.Blocked($"the fill would leave {Text(target)} bytes free, below the floor of {Text(reserve)}: nothing is written");
                yield break;
            }
            if (hasLeave && free < target - tolerance)
            {
                context.Blocked($"only {Text(free)} bytes are free, already below the target of {Text(target)}: nothing is written");
                yield break;
            }
            Directory.CreateDirectory(folder);
            // The file is in the ledger before it exists: a run that ends in the middle of the write still removes it. If it cannot be created (it appeared in between), the
            // entry is taken out again so that the cleanup never deletes a file this run did not create.
            context.Run.Ledger.Record(FileCleanupHandler.FileKind, path);
            try
            {
                using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
                    stream.Write(Magic, 0, Magic.Length);
            }
            catch
            {
                context.Run.Ledger.Forget(FileCleanupHandler.FileKind, path);
                throw;
            }
            context.Run.State[StateKey] = path;
            Activate(path);
            // The file is opened for every write and closed again: no handle is held while the step waits for the next frame, so the cleanup of a run that is cut off in
            // the middle (cancelled, connection lost) can always delete it, also where an open file cannot be deleted.
            var buffer = Pattern((int)Math.Min(chunk, 1L << 20));
            long written = 0;
            var adjustments = 0;
            var floorReached = false;
            var stale = 0;
            long staleWritten = 0;
            var lastFree = free;
            long lastChange = 0;
            while (true)
            {
                free = reader(context);
                if (free < 0)
                {
                    context.Blocked("free space became unknown while the file was written: the fill stopped and the file is removed by the cleanup");
                    yield break;
                }
                // A reader that stays where it was while we write does not tell the truth (a cached value, a wrong volume): do not write on.
                if (lastChange > 0 && free > lastFree - lastChange / 2)
                {
                    stale++;
                    staleWritten += lastChange;
                }
                else if (lastChange != 0)
                {
                    stale = 0;
                    staleWritten = 0;
                }
                if (hasStaleBytes ? staleWritten > staleLimitBytes : stale >= StaleReadingsLimit)
                {
                    var how = hasStaleBytes ? $"after {Text(staleWritten)} bytes written (more than staleBytes, {Text(staleLimitBytes)})" : $"after {StaleReadingsLimit} writes in a row";
                    context.Fail($"free space did not fall {how} (the reader says {Text(free)} bytes): it cannot keep the floor, the fill stopped and the file is removed by the cleanup");
                    yield break;
                }
                lastFree = free;
                lastChange = 0;
                if (free < reserve)
                {
                    // Below the floor: give the space back at once, down to the floor.
                    var back = Math.Min(reserve - free, written);
                    if (back > 0)
                    {
                        Truncate(path, Magic.Length + written - back);
                        written -= back;
                    }
                    if (hasBytes || back <= 0 || ++adjustments > maxAdjustments)
                    {
                        floorReached = true;
                        break;
                    }
                    lastChange = -back;
                    yield return null;
                    continue;
                }
                if (hasBytes)
                {
                    if (written >= bytes)
                        break;
                    var wanted = Math.Min(chunk, bytes - written);
                    if (free - wanted < reserve)
                    {
                        floorReached = true;
                        break;
                    }
                    Append(path, buffer, wanted);
                    written += wanted;
                    lastChange = wanted;
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
                        Append(path, buffer, wanted);
                        written += wanted;
                        lastChange = wanted;
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
                        Truncate(path, Magic.Length + written - cut);
                        written -= cut;
                        lastChange = -cut;
                    }
                }
                context.Note($"{Text(written)} bytes written");
                yield return null;
            }
            free = reader(context);
            if (free >= 0 && free < reserve)
            {
                var back = Math.Min(reserve - free, written);
                if (back > 0)
                {
                    Truncate(path, Magic.Length + written - back);
                    written -= back;
                }
                floorReached = true;
                free = reader(context);
            }
            context.AddMetric("fill.bytesWritten", "bytes", written);
            if (free >= 0)
                context.AddMetric("fill.freeAfterBytes", "bytes", free);
            if (floorReached)
                context.Fail($"the floor of {Text(reserve)} free bytes was reached after {Text(written)} bytes{(hasBytes ? " of " + Text(bytes) : string.Empty)}; the file was cut back to the floor and is removed by the cleanup");
            else if (free < 0)
                context.Fail("free space after the fill is unknown, so the floor cannot be confirmed; the file is removed by the cleanup");
            else if (hasLeave && Math.Abs(free - target) > tolerance)
                context.Fail($"free space is {Text(free)} bytes, not within {Text(tolerance)} of the target {Text(target)} after {Text(adjustments)} adjustment(s); the file is removed by the cleanup");
            else
                context.Pass($"{Text(written)} bytes written to {Path.GetFileName(path)}");
        }

        /// <summary><c>{ }</c> removes the fill file of this run now (the cleanup at the end of the run would remove it anyway) and takes it out of the ledger. Nothing to
        /// release is not an error.</summary>
        public static IEnumerator Release(StepContext context)
        {
            if (!context.Run.State.ContainsKey(StateKey))
            {
                context.Pass("no fill file to release");
                yield break;
            }
            var problems = RemoveOwned(context.Run);
            if (problems.Count > 0)
            {
                context.Fail(string.Join("; ", problems) + "; the cleanup at the end of the run tries again");
                yield break;
            }
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
            {
                var note = $"project call '{name}': {result.Note ?? "the answer is unknown"}";
                if (!context.Run.Unverified.Contains(note))
                    context.Run.Unverified.Add(note);
            }
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

        /// <summary>Not zeros: a file system that stores zeros as holes or compresses them would not take the space the fill asks for.</summary>
        static byte[] Pattern(int length)
        {
            var buffer = new byte[length];
            new Random(20261109).NextBytes(buffer);
            return buffer;
        }

        static void Append(string path, byte[] buffer, long count)
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

        static string Text(long value) => value.ToString(CultureInfo.InvariantCulture);
    }
}
