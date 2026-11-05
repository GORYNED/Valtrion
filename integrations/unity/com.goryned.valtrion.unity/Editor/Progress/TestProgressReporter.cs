using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using UnityEditor;
using UnityEditor.TestTools.TestRunner.Api;

namespace Valtrion.Unity.Editor.Progress
{
    /// <summary>Tells the host how far a Test Runner run has come. While <c>valtrion unity run</c> supervises the Editor it sets the
    /// environment variable <c>GDV_PROGRESS_FILE</c>; this reporter then keeps that file up to date: how many tests there are, how many
    /// are done, passed, failed and skipped, which test runs now. While a run goes the file is also rewritten every two seconds from a
    /// background thread (<c>utc</c>: the Editor process is alive), and <c>mainUtc</c> says when the main thread last ran, so a host can tell
    /// a long test (the main thread is busy with it) from an Editor that has stopped answering altogether. Without the variable the
    /// reporter does nothing. It only writes that one file and never changes a test or its result.</summary>
    [InitializeOnLoad]
    internal static class TestProgressReporter
    {
        internal const string EnvironmentVariable = "GDV_PROGRESS_FILE";
        internal const string Schema = "gdv.testprogress/1";

        // The Test Runner keeps registered callbacks across a domain reload; the counters are mirrored into the SessionState so that they do too.
        const string Prefix = "Valtrion.TestProgress.";
        const double MinSecondsBetweenWrites = 0.25;
        const int HeartbeatMilliseconds = 2000;

        static readonly object s_lock = new object();
        static readonly Stopwatch s_clock = Stopwatch.StartNew();
        static string s_path;
        static string s_state = "";
        static int s_total, s_done, s_passed, s_failed, s_skipped;
        static string s_current = "", s_started = "";
        static DateTime s_mainUtc = DateTime.UtcNow;
        static double s_lastWrite = -1;
        static Timer s_timer;

        static TestProgressReporter()
        {
            s_path = Environment.GetEnvironmentVariable(EnvironmentVariable);
            if (string.IsNullOrEmpty(s_path))
                return;
            Restore();
            var api = UnityEngine.ScriptableObject.CreateInstance<TestRunnerApi>();
            api.RegisterCallbacks(new Callbacks());
            EditorApplication.update += () => s_mainUtc = DateTime.UtcNow;
            s_timer = new Timer(_ => Heartbeat(), null, HeartbeatMilliseconds, HeartbeatMilliseconds);
            AssemblyReloadEvents.beforeAssemblyReload += () => s_timer?.Dispose();
        }

        static void Restore()
        {
            s_state = SessionState.GetString(Prefix + "state", "");
            s_total = SessionState.GetInt(Prefix + "total", 0);
            s_done = SessionState.GetInt(Prefix + "done", 0);
            s_passed = SessionState.GetInt(Prefix + "passed", 0);
            s_failed = SessionState.GetInt(Prefix + "failed", 0);
            s_skipped = SessionState.GetInt(Prefix + "skipped", 0);
            s_current = SessionState.GetString(Prefix + "current", "");
            s_started = SessionState.GetString(Prefix + "started", "");
        }

        // Main thread only (SessionState): keeps the counters over a domain reload.
        static void Mirror()
        {
            SessionState.SetString(Prefix + "state", s_state);
            SessionState.SetInt(Prefix + "total", s_total);
            SessionState.SetInt(Prefix + "done", s_done);
            SessionState.SetInt(Prefix + "passed", s_passed);
            SessionState.SetInt(Prefix + "failed", s_failed);
            SessionState.SetInt(Prefix + "skipped", s_skipped);
            SessionState.SetString(Prefix + "current", s_current);
            SessionState.SetString(Prefix + "started", s_started);
        }

        static void Heartbeat()
        {
            if (s_state == "running")
                WriteFile(force: true);
        }

        [Serializable]
        sealed class Callbacks : ICallbacks
        {
            public void RunStarted(ITestAdaptor testsToRun)
            {
                lock (s_lock)
                {
                    s_total = CountTests(testsToRun);
                    s_done = s_passed = s_failed = s_skipped = 0;
                    s_current = "";
                    s_started = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
                    s_state = "running";
                    s_mainUtc = DateTime.UtcNow;
                    Mirror();
                }
                WriteFile(force: true);
            }

            public void RunFinished(ITestResultAdaptor result)
            {
                lock (s_lock)
                {
                    s_state = "finished";
                    s_mainUtc = DateTime.UtcNow;
                    Mirror();
                }
                WriteFile(force: true);
            }

            public void TestStarted(ITestAdaptor test)
            {
                if (test.HasChildren)
                    return;
                lock (s_lock)
                {
                    s_current = test.FullName ?? test.Name ?? "";
                    s_mainUtc = DateTime.UtcNow;
                    Mirror();
                }
                WriteFile(force: false);
            }

            public void TestFinished(ITestResultAdaptor result)
            {
                if (result.HasChildren)
                    return;
                lock (s_lock)
                {
                    s_done++;
                    switch (result.TestStatus)
                    {
                        case TestStatus.Passed: s_passed++; break;
                        case TestStatus.Failed: s_failed++; break;
                        default: s_skipped++; break;
                    }
                    s_mainUtc = DateTime.UtcNow;
                    Mirror();
                }
                WriteFile(force: false);
            }

            static int CountTests(ITestAdaptor node)
            {
                if (node == null)
                    return 0;
                if (!node.HasChildren)
                    return node.IsSuite ? 0 : 1;
                var total = 0;
                foreach (var child in node.Children)
                    total += CountTests(child);
                return total;
            }
        }

        // Any thread: the heartbeat comes from a timer thread, the rest from the main thread.
        static void WriteFile(bool force)
        {
            lock (s_lock)
            {
                if (string.IsNullOrEmpty(s_path))
                    return;
                var now = s_clock.Elapsed.TotalSeconds;
                if (!force && s_lastWrite >= 0 && now - s_lastWrite < MinSecondsBetweenWrites)
                    return;
                s_lastWrite = now;
                try
                {
                    var json = new StringBuilder(320);
                    json.Append("{\"schema\":\"").Append(Schema).Append("\",\"pid\":").Append(Process.GetCurrentProcess().Id)
                        .Append(",\"state\":\"").Append(s_state).Append("\",\"total\":").Append(s_total)
                        .Append(",\"done\":").Append(s_done).Append(",\"passed\":").Append(s_passed)
                        .Append(",\"failed\":").Append(s_failed).Append(",\"skipped\":").Append(s_skipped)
                        .Append(",\"current\":").Append(Quote(s_current))
                        .Append(",\"startedUtc\":").Append(Quote(s_started))
                        .Append(",\"mainUtc\":").Append(Quote(s_mainUtc.ToString("o", CultureInfo.InvariantCulture)))
                        .Append(",\"utc\":").Append(Quote(DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))).Append('}');
                    var folder = Path.GetDirectoryName(s_path);
                    if (!string.IsNullOrEmpty(folder))
                        Directory.CreateDirectory(folder);
                    var partial = s_path + ".tmp";
                    File.WriteAllText(partial, json.ToString(), new UTF8Encoding(false));
                    if (File.Exists(s_path))
                        File.Replace(partial, s_path, null);
                    else
                        File.Move(partial, s_path);
                }
                catch (Exception)
                {
                    // A view of the run: a file that cannot be written must never fail a test.
                }
            }
        }

        static string Quote(string text)
        {
            var quoted = new StringBuilder(text.Length + 2).Append('"');
            foreach (var character in text)
            {
                switch (character)
                {
                    case '"': quoted.Append("\\\""); break;
                    case '\\': quoted.Append("\\\\"); break;
                    case '\n': quoted.Append("\\n"); break;
                    case '\r': quoted.Append("\\r"); break;
                    case '\t': quoted.Append("\\t"); break;
                    default:
                        if (character < ' ')
                            quoted.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            quoted.Append(character);
                        break;
                }
            }
            return quoted.Append('"').ToString();
        }
    }
}
