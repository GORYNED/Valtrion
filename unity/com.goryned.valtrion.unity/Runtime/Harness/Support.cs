using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>The time source of a run. Tests replace it so timeouts and waits are deterministic.</summary>
    public interface IClock
    {
        /// <summary>Monotonic milliseconds since an arbitrary start.</summary>
        double NowMs { get; }

        DateTime UtcNow { get; }

        /// <summary>Frame counter; probes sample once per distinct value.</summary>
        long FrameCount { get; }
    }

    public sealed class SystemClock : IClock
    {
        readonly Stopwatch _watch = Stopwatch.StartNew();

        public double NowMs => _watch.Elapsed.TotalMilliseconds;

        public DateTime UtcNow => DateTime.UtcNow;

        public long FrameCount => Time.frameCount;
    }

    /// <summary>A step was given arguments it cannot use; the step ends as FAIL with the message.</summary>
    public sealed class StepArgumentException : Exception
    {
        public StepArgumentException(string message) : base(message)
        {
        }
    }

    public static class UtcFormat
    {
        public static string Format(DateTime utc) => utc.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);

        public static string Compact(DateTime utc) => utc.ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'", CultureInfo.InvariantCulture);
    }

    public static class Identifiers
    {
        /// <summary>Maps any text to a contract identifier (letters, digits, <c>.</c> <c>_</c> <c>:</c> <c>-</c>, starting with a letter or digit).</summary>
        public static string Sanitize(string text, string fallback = "x")
        {
            var builder = new StringBuilder();
            foreach (var character in text ?? string.Empty)
            {
                if (builder.Length >= 200)
                    break;
                var letter = character < 128 && char.IsLetterOrDigit(character);
                var joiner = character == '.' || character == '_' || character == ':' || character == '-';
                if (letter || (joiner && builder.Length > 0))
                    builder.Append(character);
                else if (builder.Length > 0)
                    builder.Append('-');
            }
            return builder.Length == 0 ? fallback : builder.ToString();
        }

        /// <summary>An identifier safe as a file or directory name on every platform (no <c>:</c>).</summary>
        public static string FileSafe(string identifier) => identifier.Replace(':', '_');
    }

    public static class Hashing
    {
        public static string Sha256Hex(byte[] bytes)
        {
            using (var sha = SHA256.Create())
            {
                var hash = sha.ComputeHash(bytes);
                var builder = new StringBuilder(hash.Length * 2);
                foreach (var value in hash)
                    builder.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                return builder.ToString();
            }
        }

        public static string Sha256Hex(string text) => Sha256Hex(new UTF8Encoding(false).GetBytes(text));

        public static string Sha256HexOfFile(string path) => Sha256Hex(File.ReadAllBytes(path));
    }

    /// <summary>Writes a file so a reader never sees a partial one: temporary name in the same folder, then rename.</summary>
    public static class AtomicFile
    {
        public static void WriteAllText(string path, string text)
        {
            var directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory))
                Directory.CreateDirectory(directory);
            var temporary = path + ".tmp-" + Guid.NewGuid().ToString("N").Substring(0, 8);
            File.WriteAllText(temporary, text, new UTF8Encoding(false));
            try
            {
                if (File.Exists(path))
                {
                    try
                    {
                        File.Replace(temporary, path, null);
                        return;
                    }
                    catch (Exception ex) when (ex is IOException || ex is PlatformNotSupportedException || ex is UnauthorizedAccessException)
                    {
                        File.Delete(path);
                    }
                }
                File.Move(temporary, path);
            }
            finally
            {
                if (File.Exists(temporary))
                    File.Delete(temporary);
            }
        }
    }

    /// <summary>Where a run reads and writes. <c>Root</c> is <c>&lt;persistentDataPath&gt;/gdv</c> unless a test supplies another folder.</summary>
    public sealed class RunPaths
    {
        public RunPaths(string persistentDataPath, string root, string runId, string outRoot = null)
        {
            PersistentDataPath = persistentDataPath;
            Root = root;
            Inbox = Path.Combine(root, "inbox");
            OutRoot = string.IsNullOrEmpty(outRoot) ? Path.Combine(root, "out") : outRoot;
            Work = Path.Combine(root, "work");
            RunDirectory = Path.Combine(OutRoot, Identifiers.FileSafe(runId));
        }

        public string PersistentDataPath { get; }

        public string Root { get; }

        public string Inbox { get; }

        public string OutRoot { get; }

        public string Work { get; }

        public string RunDirectory { get; }

        public string ResultPath => Path.Combine(RunDirectory, "result.json");

        public string HeartbeatPath => Path.Combine(RunDirectory, "heartbeat.json");

        public string LedgerPath => Path.Combine(RunDirectory, "ledger.json");

        /// <summary>Resolves a relative path below <paramref name="baseDirectory"/>; rooted paths and <c>..</c> are refused.</summary>
        public static string Below(string baseDirectory, string relative)
        {
            if (string.IsNullOrEmpty(relative) || Path.IsPathRooted(relative) || relative.StartsWith("/", StringComparison.Ordinal) || relative.StartsWith("\\", StringComparison.Ordinal))
                throw new StepArgumentException($"path '{relative}' must be relative");
            foreach (var part in relative.Split('/', '\\'))
            {
                if (part == "..")
                    throw new StepArgumentException($"path '{relative}' must not leave its folder");
            }
            return Path.GetFullPath(Path.Combine(baseDirectory, relative));
        }
    }

    /// <summary>Statistics of one metric: <c>n</c>, median, nearest-rank p90, max and the sample standard deviation.</summary>
    public static class Statistics
    {
        public static Contracts.MetricStats Compute(IReadOnlyList<double> samples)
        {
            var sorted = new List<double>(samples);
            sorted.Sort();
            var n = sorted.Count;
            if (n == 0)
                return new Contracts.MetricStats { N = 0, Median = 0, P90 = 0, Max = 0 };
            var median = n % 2 == 1 ? sorted[n / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
            double? stdev = null;
            if (n > 1)
            {
                var mean = 0.0;
                foreach (var value in sorted)
                    mean += value;
                mean /= n;
                var squares = 0.0;
                foreach (var value in sorted)
                    squares += (value - mean) * (value - mean);
                stdev = Math.Sqrt(squares / (n - 1));
            }
            return new Contracts.MetricStats { N = n, Median = median, P90 = Percentile(sorted, 90), Max = sorted[n - 1], Stdev = stdev };
        }

        /// <summary>Nearest-rank percentile of a sorted, non-empty list.</summary>
        public static double Percentile(IReadOnlyList<double> sorted, double percent)
        {
            var rank = (int)Math.Ceiling(percent / 100.0 * sorted.Count) - 1;
            return sorted[Math.Max(0, Math.Min(sorted.Count - 1, rank))];
        }
    }

    /// <summary>Samples of every metric of a run. Samples are ignored while <see cref="Enabled"/> is false (setup and warm-up).</summary>
    public sealed class MetricStore
    {
        public const int MaxSamples = 20000;

        sealed class Series
        {
            public string Unit;
            public readonly List<double> Samples = new List<double>();
        }

        readonly List<string> _order = new List<string>();
        readonly Dictionary<string, Series> _series = new Dictionary<string, Series>();

        public bool Enabled { get; set; } = true;

        /// <summary>True when a series reached <see cref="MaxSamples"/> and later samples were dropped.</summary>
        public bool Truncated { get; private set; }

        public void Add(string name, string unit, double value)
        {
            if (!Enabled || double.IsNaN(value) || double.IsInfinity(value))
                return;
            name = Identifiers.Sanitize(name, "metric");
            if (!_series.TryGetValue(name, out var series))
            {
                series = new Series { Unit = unit };
                _series.Add(name, series);
                _order.Add(name);
            }
            if (series.Samples.Count >= MaxSamples)
            {
                Truncated = true;
                return;
            }
            series.Samples.Add(value);
        }

        public IReadOnlyList<double> Samples(string name) => _series.TryGetValue(name, out var series) ? series.Samples : (IReadOnlyList<double>)Array.Empty<double>();

        public List<Contracts.ResultMetric> Build(Dictionary<string, Contracts.MetricSpec> specs)
        {
            var metrics = new List<Contracts.ResultMetric>();
            foreach (var name in _order)
            {
                var series = _series[name];
                var unit = specs != null && specs.TryGetValue(name, out var spec) ? spec.Unit : series.Unit;
                metrics.Add(new Contracts.ResultMetric { Name = name, Unit = unit ?? "count", Samples = new List<double>(series.Samples), Stats = Statistics.Compute(series.Samples) });
            }
            return metrics;
        }
    }
}
