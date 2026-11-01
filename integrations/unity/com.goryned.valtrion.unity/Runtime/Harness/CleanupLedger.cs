using System;
using System.Collections.Generic;
using System.IO;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Harness
{
    public sealed class LedgerEntry
    {
        public string Kind { get; set; }

        /// <summary>What was created (a file path for kind <c>file</c>).</summary>
        public string Key { get; set; }

        /// <summary>Kind-specific detail; for <c>file</c> the path of the backup of an overwritten file, or null.</summary>
        public string Detail { get; set; }
    }

    /// <summary>A project hook that undoes one kind of ledger entry (a created account, a seeded record, a granted flag).</summary>
    public interface ICleanupHandler
    {
        string Kind { get; }

        void Cleanup(LedgerEntry entry);
    }

    /// <summary>Removes a file the run created, or restores the file it overwrote.</summary>
    public sealed class FileCleanupHandler : ICleanupHandler
    {
        public const string FileKind = "file";

        public string Kind => FileKind;

        public void Cleanup(LedgerEntry entry)
        {
            if (!string.IsNullOrEmpty(entry.Detail) && File.Exists(entry.Detail))
            {
                File.Copy(entry.Detail, entry.Key, true);
                File.Delete(entry.Detail);
            }
            else if (File.Exists(entry.Key))
            {
                File.Delete(entry.Key);
            }
        }
    }

    /// <summary>Records what a run created. Cleanup touches only ledger entries, newest first, and the ledger is written to disk
    /// after every change so an interrupted run can still be cleaned up from its file.</summary>
    public sealed class CleanupLedger
    {
        readonly string _path;
        readonly string _runId;
        readonly List<LedgerEntry> _entries = new List<LedgerEntry>();
        readonly Dictionary<string, ICleanupHandler> _handlers = new Dictionary<string, ICleanupHandler>();

        public CleanupLedger(string runId, string path)
        {
            _runId = runId;
            _path = path;
            Register(new FileCleanupHandler());
        }

        public IReadOnlyList<LedgerEntry> Entries => _entries;

        /// <summary>Adopts the entries of a ledger file left by an earlier launch with the same run id, so its leftovers are cleaned too.
        /// An unreadable file is ignored; the new ledger then overwrites it.</summary>
        public int LoadExisting()
        {
            if (_path == null || !File.Exists(_path))
                return 0;
            try
            {
                var document = JsonValue.Parse(File.ReadAllText(_path));
                if (document.Kind != JsonKind.Object || !document.TryGet("entries", out var entries) || entries.Kind != JsonKind.Array)
                    return 0;
                var loaded = 0;
                foreach (var node in entries.Items)
                {
                    if (node.Kind != JsonKind.Object || !node.TryGet("kind", out var kind) || kind.Kind != JsonKind.String || !node.TryGet("key", out var key) || key.Kind != JsonKind.String)
                        continue;
                    var detail = node.TryGet("detail", out var d) && d.Kind == JsonKind.String ? d.AsString : null;
                    _entries.Add(new LedgerEntry { Kind = kind.AsString, Key = key.AsString, Detail = detail });
                    loaded++;
                }
                return loaded;
            }
            catch (Exception ex) when (ex is JsonException || ex is IOException)
            {
                return 0;
            }
        }

        public void Register(ICleanupHandler handler)
        {
            if (handler == null)
                throw new ArgumentNullException(nameof(handler));
            _handlers[handler.Kind] = handler;
        }

        public void Record(string kind, string key, string detail = null)
        {
            _entries.Add(new LedgerEntry { Kind = kind, Key = key, Detail = detail });
            Persist();
        }

        /// <summary>Runs the handlers of the recorded entries, newest first. Returns one message per entry that could not be cleaned.</summary>
        public List<string> Cleanup()
        {
            var problems = new List<string>();
            for (var index = _entries.Count - 1; index >= 0; index--)
            {
                var entry = _entries[index];
                if (!_handlers.TryGetValue(entry.Kind, out var handler))
                {
                    problems.Add($"cleanup: no handler for ledger kind '{entry.Kind}' ({entry.Key})");
                    continue;
                }
                try
                {
                    handler.Cleanup(entry);
                    _entries.RemoveAt(index);
                }
                catch (Exception ex)
                {
                    problems.Add($"cleanup of {entry.Kind} '{entry.Key}' failed: {ex.Message}");
                }
            }
            Persist();
            return problems;
        }

        void Persist()
        {
            if (_path == null)
                return;
            var entries = JsonValue.NewArray();
            foreach (var entry in _entries)
                entries.Add(JsonValue.NewObject().Set("kind", JsonValue.From(entry.Kind)).Set("key", JsonValue.From(entry.Key)).Set("detail", JsonValue.From(entry.Detail)));
            AtomicFile.WriteAllText(_path, JsonValue.NewObject().Set("runId", JsonValue.From(_runId)).Set("entries", entries).Write(true));
        }
    }
}
