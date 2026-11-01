using System;
using System.Text.RegularExpressions;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>Follows <c>Application.logMessageReceivedThreaded</c> for a run: a ring of the latest lines for <c>log.expect</c> and a
    /// live event for log probes. Lines may arrive on any thread.</summary>
    public sealed class LogTap : IDisposable
    {
        const int Capacity = 2000;

        readonly object _gate = new object();
        readonly string[] _ring = new string[Capacity];
        readonly bool _attached;
        long _sequence;

        public LogTap(bool attach)
        {
            _attached = attach;
            if (attach)
                Application.logMessageReceivedThreaded += OnLog;
        }

        /// <summary>Raised on the thread that logged. Handlers must be thread safe.</summary>
        public event Action<string> LineReceived;

        public long Sequence
        {
            get
            {
                lock (_gate)
                    return _sequence;
            }
        }

        public void Feed(string line)
        {
            line = line ?? string.Empty;
            lock (_gate)
            {
                _ring[(int)(_sequence % Capacity)] = line;
                _sequence++;
            }
            LineReceived?.Invoke(line);
        }

        /// <summary>Finds the first line logged after <paramref name="afterSequence"/> (a value of <see cref="Sequence"/>) that matches.</summary>
        public bool TryFind(Regex pattern, long afterSequence, out string line)
        {
            lock (_gate)
            {
                var first = Math.Max(afterSequence, _sequence - Capacity);
                for (var index = first; index < _sequence; index++)
                {
                    var candidate = _ring[(int)(index % Capacity)];
                    if (pattern.IsMatch(candidate))
                    {
                        line = candidate;
                        return true;
                    }
                }
            }
            line = null;
            return false;
        }

        public void Dispose()
        {
            if (_attached)
                Application.logMessageReceivedThreaded -= OnLog;
        }

        void OnLog(string condition, string stackTrace, LogType type) => Feed(condition);
    }
}
