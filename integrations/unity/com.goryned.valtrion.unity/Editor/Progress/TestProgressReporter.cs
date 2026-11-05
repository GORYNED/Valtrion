using System;
using System.Globalization;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEditor.TestTools.TestRunner.Api;

namespace Valtrion.Unity.Editor.Progress
{
    /// <summary>Tells the host how far a Test Runner run has come. While <c>valtrion unity run</c> supervises the Editor it sets the
    /// environment variable <c>GDV_PROGRESS_FILE</c>; this reporter then keeps that file up to date (how many tests there are, how many
    /// are done, passed, failed and skipped, and which test runs now), so a dashboard can show "120 of 361" instead of a moving bar. Without
    /// the variable it does nothing. It only writes that one file and never changes a test or its result.</summary>
    [InitializeOnLoad]
    internal static class TestProgressReporter
    {
        internal const string EnvironmentVariable = "GDV_PROGRESS_FILE";
        internal const string Schema = "gdv.testprogress/1";

        static TestProgressReporter()
        {
            var path = Environment.GetEnvironmentVariable(EnvironmentVariable);
            if (string.IsNullOrEmpty(path))
                return;
            var api = UnityEngine.ScriptableObject.CreateInstance<TestRunnerApi>();
            api.RegisterCallbacks(new Callbacks(path));
        }

        [Serializable]
        sealed class Callbacks : ICallbacks
        {
            // The Test Runner keeps registered callbacks across a domain reload; the counters live in the SessionState so that they do too.
            const string Prefix = "Valtrion.TestProgress.";
            const double MinSecondsBetweenWrites = 0.25;

            readonly string _path;
            double _lastWrite;

            public Callbacks(string path) { _path = path; }

            static int Get(string name) => SessionState.GetInt(Prefix + name, 0);
            static void Set(string name, int value) => SessionState.SetInt(Prefix + name, value);

            public void RunStarted(ITestAdaptor testsToRun)
            {
                Set("total", CountTests(testsToRun));
                foreach (var name in new[] { "done", "passed", "failed", "skipped" })
                    Set(name, 0);
                SessionState.SetString(Prefix + "current", "");
                SessionState.SetString(Prefix + "started", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
                Write("running", force: true);
            }

            public void RunFinished(ITestResultAdaptor result) => Write("finished", force: true);

            public void TestStarted(ITestAdaptor test)
            {
                if (test.HasChildren)
                    return;
                SessionState.SetString(Prefix + "current", test.FullName ?? test.Name ?? "");
                Write("running", force: false);
            }

            public void TestFinished(ITestResultAdaptor result)
            {
                if (result.HasChildren)
                    return;
                Set("done", Get("done") + 1);
                switch (result.TestStatus)
                {
                    case TestStatus.Passed: Set("passed", Get("passed") + 1); break;
                    case TestStatus.Failed: Set("failed", Get("failed") + 1); break;
                    default: Set("skipped", Get("skipped") + 1); break;
                }
                Write("running", force: false);
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

            void Write(string state, bool force)
            {
                var now = EditorApplication.timeSinceStartup;
                if (!force && now - _lastWrite < MinSecondsBetweenWrites)
                    return;
                _lastWrite = now;
                try
                {
                    var json = new StringBuilder(256);
                    json.Append("{\"schema\":\"").Append(Schema).Append("\",\"pid\":").Append(System.Diagnostics.Process.GetCurrentProcess().Id)
                        .Append(",\"state\":\"").Append(state).Append("\",\"total\":").Append(Get("total"))
                        .Append(",\"done\":").Append(Get("done")).Append(",\"passed\":").Append(Get("passed"))
                        .Append(",\"failed\":").Append(Get("failed")).Append(",\"skipped\":").Append(Get("skipped"))
                        .Append(",\"current\":").Append(Quote(SessionState.GetString(Prefix + "current", "")))
                        .Append(",\"startedUtc\":").Append(Quote(SessionState.GetString(Prefix + "started", "")))
                        .Append(",\"utc\":").Append(Quote(DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))).Append('}');
                    var folder = Path.GetDirectoryName(_path);
                    if (!string.IsNullOrEmpty(folder))
                        Directory.CreateDirectory(folder);
                    var partial = _path + ".tmp";
                    File.WriteAllText(partial, json.ToString(), new UTF8Encoding(false));
                    if (File.Exists(_path))
                        File.Replace(partial, _path, null);
                    else
                        File.Move(partial, _path);
                }
                catch (Exception)
                {
                    // A view of the run: a file that cannot be written must never fail a test.
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
}
