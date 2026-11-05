using System;
using System.Collections.Generic;
using System.IO;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>What the inbox needs from the platform; tests replace it.</summary>
    public interface IPlanEnvironment
    {
        string[] CommandLineArgs { get; }

        string PersistentDataPath { get; }

        DateTime UtcNow { get; }

        /// <summary>The Android intent extra of that name, or null (always null on other platforms).</summary>
        string GetIntentExtra(string name);

        /// <summary>The process environment variable of that name, or null.</summary>
        string GetEnvironmentVariable(string name);
    }

    public sealed class UnityPlanEnvironment : IPlanEnvironment
    {
        public string[] CommandLineArgs => Environment.GetCommandLineArgs();

        public string PersistentDataPath => Application.persistentDataPath;

        public DateTime UtcNow => DateTime.UtcNow;

        public string GetEnvironmentVariable(string name) => Environment.GetEnvironmentVariable(name);

        public string GetIntentExtra(string name)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using (var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
                using (var activity = player.GetStatic<AndroidJavaObject>("currentActivity"))
                using (var intent = activity.Call<AndroidJavaObject>("getIntent"))
                    return intent.Call<bool>("hasExtra", name) ? intent.Call<string>("getStringExtra", name) : null;
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"GDV: cannot read the intent extra '{name}': {ex.Message}");
                return null;
            }
#else
            return null;
#endif
        }
    }

    /// <summary>A project hook that turns a legacy plan file (for example a project's <c>autorec.json</c>) into a plan. The project owns the converter.</summary>
    public interface ILegacyPlanConverter
    {
        /// <summary>The legacy file names this converter looks for, for example <c>autorec.json</c>.</summary>
        IReadOnlyList<string> FileNames { get; }

        bool CanConvert(string fileName, string text);

        PlanDocument Convert(string fileName, string text);
    }

    /// <summary>A plan found by the inbox. <see cref="Plan"/> is null when the text was rejected, and <see cref="Error"/> says why.</summary>
    public sealed class PlanSource
    {
        public string Origin { get; set; }

        public string Path { get; set; }

        public string Text { get; set; }

        public PlanDocument Plan { get; set; }

        public string Error { get; set; }

        public string RunId { get; set; }

        /// <summary>Replaces <c>&lt;persistentDataPath&gt;/gdv/out</c> when set by <c>-gdv-out</c>, <c>GDV_OUT_ROOT</c> or the intent extra <c>gdv-out</c>.</summary>
        public string OutRoot { get; set; }
    }

    /// <summary>Finds the plan of this launch. Sources in order: launch argument <c>-gdv-plan &lt;path&gt;</c>, Android intent extra
    /// <c>gdv-plan</c>, the file <c>&lt;persistentDataPath&gt;/gdv/inbox/plan.json</c>, then a legacy file through a registered converter.
    /// A consumed file is renamed to <c>&lt;name&gt;.done-&lt;utc&gt;</c>, never read twice.</summary>
    public sealed class PlanInbox
    {
        public const string PlanArgument = "-gdv-plan";
        public const string RunIdArgument = "-gdv-run-id";
        public const string OutArgument = "-gdv-out";
        public const string PlanVariable = "GDV_PLAN";
        public const string RunIdVariable = "GDV_RUN_ID";
        public const string OutVariable = "GDV_OUT_ROOT";
        public const string PlanExtra = "gdv-plan";
        public const string RunIdExtra = "gdv-run-id";
        public const string OutExtra = "gdv-out";
        public const string InboxPlanFile = "plan.json";

        /// <summary>Converters registered by a project at start-up (before the bootstrap runs, or from its own <c>RuntimeInitializeOnLoadMethod</c>).</summary>
        public static readonly List<ILegacyPlanConverter> LegacyConverters = new List<ILegacyPlanConverter>();

        readonly IPlanEnvironment _environment;
        readonly IReadOnlyList<ILegacyPlanConverter> _converters;

        public PlanInbox(IPlanEnvironment environment, IReadOnlyList<ILegacyPlanConverter> converters = null)
        {
            _environment = environment ?? throw new ArgumentNullException(nameof(environment));
            _converters = converters ?? LegacyConverters;
        }

        public string InboxDirectory => System.IO.Path.Combine(_environment.PersistentDataPath, "gdv", "inbox");

        /// <summary>Looks for a plan without consuming it; null when this launch has none.</summary>
        public PlanSource Find()
        {
            // Precedence for plan, run id and output folder alike: launch argument, environment variable, intent extra (the plan then falls back to the inbox).
            var runId = Setting(RunIdArgument, RunIdVariable, RunIdExtra);
            var outRoot = Setting(OutArgument, OutVariable, OutExtra);
            var fromArgument = ArgumentValue(PlanArgument);
            // Like an intent, a variable outlives its plan (it is inherited by a relaunch), so a path already consumed is not an error.
            var fromEnvironment = fromArgument == null ? NonEmpty(_environment.GetEnvironmentVariable(PlanVariable)) : null;
            if (fromEnvironment != null && AlreadyConsumed(fromEnvironment))
                fromEnvironment = null;
            var explicitPlan = fromArgument ?? fromEnvironment;
            if (explicitPlan != null)
            {
                var origin = fromArgument != null ? "argument" : "environment";
                var name = fromArgument != null ? PlanArgument : PlanVariable;
                // An explicit plan that names no file is an error to report, never a reason to run some other plan.
                var found = File.Exists(explicitPlan)
                    ? Read(origin, explicitPlan, runId)
                    : new PlanSource { Origin = origin, Path = explicitPlan, RunId = runId, Text = string.Empty, Error = MissingPlanError(explicitPlan, name) };
                found.OutRoot = outRoot;
                return found;
            }
            var fromIntent = _environment.GetIntentExtra(PlanExtra);
            // An intent outlives its plan (the activity can be recreated), so a path whose file was already consumed is not an error.
            if (!string.IsNullOrEmpty(fromIntent) && !AlreadyConsumed(fromIntent))
            {
                var found = File.Exists(fromIntent)
                    ? Read("intent", fromIntent, runId)
                    : new PlanSource { Origin = "intent", Path = fromIntent, RunId = runId, Text = string.Empty, Error = MissingPlanError(fromIntent, "the intent extra " + PlanExtra) };
                found.OutRoot = outRoot;
                return found;
            }
            var inboxPlan = System.IO.Path.Combine(InboxDirectory, InboxPlanFile);
            var source = File.Exists(inboxPlan) ? Read("inbox", inboxPlan, runId) : FindLegacy(runId);
            if (source != null)
                source.OutRoot = outRoot;
            return source;
        }

        static string NonEmpty(string value) => string.IsNullOrEmpty(value) ? null : value;

        string Setting(string argument, string variable, string extra) =>
            ArgumentValue(argument) ?? NonEmpty(_environment.GetEnvironmentVariable(variable)) ?? NonEmpty(_environment.GetIntentExtra(extra));

        /// <summary>Renames the source file to <c>&lt;name&gt;.done-&lt;utc&gt;</c>. A file that cannot be renamed is left alone and reported by the caller's log.</summary>
        public bool MarkConsumed(PlanSource source)
        {
            if (source == null || string.IsNullOrEmpty(source.Path) || !File.Exists(source.Path))
                return false;
            try
            {
                File.Move(source.Path, source.Path + ".done-" + UtcFormat.Compact(_environment.UtcNow));
                return true;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                Debug.LogWarning($"GDV: cannot rename the consumed plan '{source.Path}': {ex.Message}");
                return false;
            }
        }

        PlanSource FindLegacy(string runId)
        {
            foreach (var converter in _converters)
            {
                foreach (var name in converter.FileNames)
                {
                    foreach (var directory in new[] { InboxDirectory, _environment.PersistentDataPath })
                    {
                        var path = System.IO.Path.Combine(directory, name);
                        if (!File.Exists(path))
                            continue;
                        var text = File.ReadAllText(path);
                        if (!converter.CanConvert(name, text))
                            continue;
                        var source = new PlanSource { Origin = "legacy:" + name, Path = path, Text = text, RunId = runId };
                        try
                        {
                            source.Plan = converter.Convert(name, text);
                            source.Text = source.Plan.ToJsonString();
                        }
                        catch (Exception ex) when (ex is ContractException || ex is JsonException || ex is ArgumentException || ex is InvalidOperationException)
                        {
                            source.Error = $"legacy plan '{name}' was not converted: {ex.Message}";
                        }
                        return source;
                    }
                }
            }
            return null;
        }

        static bool AlreadyConsumed(string path) => !File.Exists(path) && LatestConsumedCopy(path) != null;

        /// <summary>The newest <c>&lt;name&gt;.done-&lt;utc&gt;</c> beside a plan path (the copy an earlier run left when it consumed the plan), or null.</summary>
        static string LatestConsumedCopy(string path)
        {
            try
            {
                // A bare file name (`-gdv-plan smoke.plan.json`) is resolved like File.Exists resolves it: against the working directory.
                var full = System.IO.Path.GetFullPath(path);
                var directory = System.IO.Path.GetDirectoryName(full);
                if (string.IsNullOrEmpty(directory))
                    directory = ".";
                if (!Directory.Exists(directory))
                    return null;
                // Filtered by name, not by a search pattern: a file name may itself hold the wildcard characters * and ? (POSIX).
                var prefix = System.IO.Path.GetFileName(full) + ".done-";
                string latest = null;
                foreach (var candidate in Directory.GetFiles(directory))
                {
                    var name = System.IO.Path.GetFileName(candidate);
                    if (name.StartsWith(prefix, StringComparison.Ordinal) && (latest == null || string.CompareOrdinal(candidate, latest) > 0))
                        latest = candidate;
                }
                return latest;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is ArgumentException || ex is NotSupportedException)
            {
                return null;
            }
        }

        /// <summary>Why an explicit plan path names no file. A plan the harness already consumed (renamed to <c>.done-&lt;utc&gt;</c> by an earlier
        /// launch, for example a Mono player started with the same path) is named as such, so it is not mistaken for a path the player cannot read.
        /// The consumed hint can appear only for the launch argument <c>-gdv-plan</c>: a consumed <c>GDV_PLAN</c> or intent extra is ignored earlier
        /// (<see cref="AlreadyConsumed"/>), because both outlive their plan by design.</summary>
        internal static string MissingPlanError(string path, string givenBy)
        {
            var consumed = LatestConsumedCopy(path);
            return consumed == null
                ? $"plan '{path}' given by {givenBy} does not exist"
                : $"plan '{path}' given by {givenBy} does not exist: an earlier run already consumed it (renamed to '{System.IO.Path.GetFileName(consumed)}'); give every launch a fresh copy of the plan";
        }

        static PlanSource Read(string origin, string path, string runId)
        {
            var source = new PlanSource { Origin = origin, Path = path, RunId = runId };
            try
            {
                source.Text = File.ReadAllText(path);
                source.Plan = PlanDocument.Parse(source.Text);
            }
            catch (Exception ex) when (ex is ContractException || ex is JsonException || ex is IOException || ex is UnauthorizedAccessException)
            {
                source.Error = $"plan '{path}' was rejected: {ex.Message}";
            }
            return source;
        }

        string ArgumentValue(string name)
        {
            var arguments = _environment.CommandLineArgs;
            for (var index = 0; arguments != null && index < arguments.Length - 1; index++)
            {
                if (arguments[index] == name)
                    return arguments[index + 1];
            }
            return null;
        }

    }
}
