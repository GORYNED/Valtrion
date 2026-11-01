using System;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Harness
{
    /// <summary>Writes the files a supervisor reads: <c>heartbeat.json</c> and <c>result.json</c> (both atomically) and the single
    /// terminal log line <c>GDV-RESULT &lt;STATUS&gt; run=&lt;id&gt;</c>.</summary>
    public sealed class ResultWriter
    {
        public const string TerminalPrefix = "GDV-RESULT";

        readonly RunPaths _paths;
        readonly Action<string> _log;
        bool _terminalEmitted;

        public ResultWriter(RunPaths paths, Action<string> log)
        {
            _paths = paths;
            _log = log;
        }

        public void WriteHeartbeat(HeartbeatDocument heartbeat) => AtomicFile.WriteAllText(_paths.HeartbeatPath, heartbeat.ToJsonString());

        /// <summary>Points <c>out/current.json</c> at this run so a host that did not choose the run id can find it.</summary>
        public void WriteCurrentPointer(string runId) =>
            AtomicFile.WriteAllText(System.IO.Path.Combine(_paths.OutRoot, "current.json"), JsonValue.NewObject().Set("runId", JsonValue.From(runId)).Write(true));

        public void WriteResult(ResultDocument result) => AtomicFile.WriteAllText(_paths.ResultPath, result.ToJsonString());

        /// <summary>Logs the terminal line once; later calls do nothing.</summary>
        public bool EmitTerminalLine(string status, string runId)
        {
            if (_terminalEmitted)
                return false;
            _terminalEmitted = true;
            _log($"{TerminalPrefix} {status} run={runId}");
            return true;
        }
    }
}
