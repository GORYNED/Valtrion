using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;
using Valtrion.Unity.Probes;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>The step types every plan can use: <c>wait</c>, <c>waitUntil</c>, <c>mark</c>, <c>probe.start</c>, <c>probe.stop</c>, <c>gc</c>,
    /// <c>idle</c>, <c>screenshot</c>, <c>log.expect</c>, <c>file.put</c>, <c>app.quit</c>, <c>external.ui</c>, <c>session.offsets</c>, <c>storage.begin</c>, <c>storage.end</c>, <c>timer.start</c>, <c>timer.stop</c>, <c>counter.start</c>, <c>counter.stop</c> and <c>repeat</c>. They are registered by an explicit table, so they
    /// need neither reflection nor linker preservation.</summary>
    public static class BuiltInSteps
    {
        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("wait", Wait);
            registry.Register("waitUntil", WaitUntil);
            registry.Register("mark", Mark);
            registry.Register("probe.start", ProbeStart);
            registry.Register("probe.stop", ProbeStop);
            registry.Register("gc", Gc);
            registry.Register("idle", Idle);
            registry.Register("screenshot", Screenshot);
            registry.Register("log.expect", LogExpect);
            registry.Register("file.put", FilePut);
            registry.Register("app.quit", AppQuit);
            registry.Register("external.ui", ExternalUi);
            SessionSteps.RegisterAll(registry);
            StorageSteps.RegisterAll(registry);
            CounterSteps.RegisterAll(registry);
            RepeatStep.RegisterAll(registry);
        }

        /// <summary><c>{ "seconds": 2 }</c> waits that long on the run clock.</summary>
        public static IEnumerator Wait(StepContext context)
        {
            var seconds = context.Number("seconds");
            if (seconds < 0)
                throw new StepArgumentException("'seconds' must not be negative");
            var end = context.Clock.NowMs + seconds * 1000.0;
            context.Note($"waiting {seconds} s");
            while (context.Clock.NowMs < end)
                yield return null;
        }

        /// <summary><c>{ "condition": "menu.ready" }</c> polls a condition of the <see cref="ConditionRegistry"/> every frame until it is true.</summary>
        public static IEnumerator WaitUntil(StepContext context)
        {
            var name = context.String("condition");
            if (!context.Run.Conditions.TryGet(name, out var condition))
            {
                context.Blocked($"condition '{name}' is not registered in this build");
                yield break;
            }
            context.Note($"waiting for condition '{name}'");
            while (true)
            {
                bool met;
                try
                {
                    met = condition();
                }
                catch (Exception ex)
                {
                    context.Fail($"condition '{name}' threw {ex.GetType().Name}: {ex.Message}{Faults.FirstFrame(ex)}");
                    yield break;
                }
                if (met)
                    break;
                yield return null;
            }
            context.Pass($"condition '{name}' met");
        }

        /// <summary><c>{ "name": "ready", "metric": "readyMs" }</c> records the milliseconds since the iteration started as a mark of the step,
        /// and as a sample of <c>metric</c> when given.</summary>
        public static IEnumerator Mark(StepContext context)
        {
            var name = context.String("name");
            var elapsed = context.Clock.NowMs - context.Run.IterationStartMs;
            context.Mark(name, elapsed);
            if (context.Args.TryGet("metric", out var metric) && metric.Kind == JsonKind.String)
                context.AddMetric(metric.AsString, "ms", elapsed);
            yield break;
        }

        /// <summary><c>{ "probes": ["frameTiming", "profiler", "timer:name", "log:TAG"], "required": false }</c> opens the measurement window.
        /// A probe that is unavailable is listed as unverified, or ends the step as BLOCKED when <c>required</c> is true.</summary>
        public static IEnumerator ProbeStart(StepContext context)
        {
            if (context.Run.Window != null)
            {
                context.Fail("a probe window is already open");
                yield break;
            }
            if (!context.Args.TryGet("probes", out var specs) || specs.Kind != JsonKind.Array || specs.Count == 0)
                throw new StepArgumentException("argument 'probes' must be a non-empty array");
            var probes = new List<IProbe>();
            foreach (var spec in specs.Items)
                probes.Add(ProbeFactory.Create(spec, context.Run));
            var window = new ProbeWindow(probes, context.Run.Metrics);
            context.Run.Window = window;
            List<string> notes;
            try
            {
                notes = window.Start();
            }
            catch
            {
                context.Run.Window = null;
                window.Abandon();
                throw;
            }
            if (notes.Count == 0)
                yield break;
            foreach (var note in notes)
                context.Run.Unverified.Add(note);
            if (context.Bool("required", false))
                context.Blocked(string.Join("; ", notes));
            else
                context.Pass("unavailable: " + string.Join("; ", notes));
        }

        public static IEnumerator ProbeStop(StepContext context)
        {
            var window = context.Run.Window;
            if (window == null)
            {
                context.Fail("no probe window is open");
                yield break;
            }
            context.Run.Window = null;
            window.Stop();
        }

        /// <summary><c>{ "unloadAssets": true }</c> collects managed garbage and, by default, unloads unused assets.</summary>
        public static IEnumerator Gc(StepContext context)
        {
            var unload = context.Bool("unloadAssets", true);
            GC.Collect();
            GC.WaitForPendingFinalizers();
            GC.Collect();
            if (unload)
            {
                var operation = Resources.UnloadUnusedAssets();
                while (!operation.isDone)
                    yield return null;
            }
        }

        /// <summary><c>{ "frames": 10 }</c> lets that many frames pass (default 1).</summary>
        public static IEnumerator Idle(StepContext context)
        {
            var frames = (long)context.Number("frames", 1);
            if (frames < 1)
                throw new StepArgumentException("'frames' must be at least 1");
            var target = context.Clock.FrameCount + frames;
            while (context.Clock.FrameCount < target)
                yield return null;
        }

        /// <summary>Whether frames are rendered, which a screen capture needs. Replaceable so a test can exercise the unavailable path.</summary>
        public static Func<bool> ScreenCaptureAvailable { get; set; } = () => SystemInfo.graphicsDeviceType != UnityEngine.Rendering.GraphicsDeviceType.Null;

        /// <summary><c>{ "name": "menu" }</c> saves the composited screen as <c>&lt;name&gt;.png</c> next to the result and lists it as an artifact.</summary>
        public static IEnumerator Screenshot(StepContext context)
        {
            var name = Identifiers.FileSafe(Identifiers.Sanitize(context.String("name")));
            // Without a graphics device (a batchmode Editor with -nographics) no frame is ever rendered, so a wait for the end of the
            // frame would never return; the step ends BLOCKED at once instead of running into its timeout.
            if (!ScreenCaptureAvailable())
            {
                context.Blocked("screen capture is unavailable: no graphics device is rendering frames (batchmode without graphics)");
                yield break;
            }
            yield return new WaitForEndOfFrame();
            Texture2D texture = null;
            byte[] png = null;
            string failure = null;
            try
            {
                texture = ScreenCapture.CaptureScreenshotAsTexture();
                png = texture != null ? texture.EncodeToPNG() : null;
            }
            catch (Exception ex)
            {
                failure = ex.Message;
            }
            finally
            {
                if (texture != null)
                {
                    if (Application.isPlaying)
                        UnityEngine.Object.Destroy(texture);
                    else
                        UnityEngine.Object.DestroyImmediate(texture);
                }
            }
            if (png == null || png.Length == 0)
            {
                context.Fail("screen capture produced no image" + (failure != null ? ": " + failure : string.Empty));
                yield break;
            }
            Directory.CreateDirectory(context.Run.Paths.RunDirectory);
            // One file per iteration: a repeated plan must not overwrite a file an earlier artifact entry still hashes.
            var file = context.Phase == "run" ? $"{name}-i{context.Iteration + 1}-s{context.Index}" : $"{name}-setup-s{context.Index}";
            if (context.Iteration < 0)
                file = $"{name}-warmup{-context.Iteration}-s{context.Index}";
            // A screenshot inside a repeat step is taken once per round: the round is a part of the name, so no round overwrites an earlier one.
            file += (context.Round > 0 ? $"-r{context.Round}" : string.Empty) + ".png";
            File.WriteAllBytes(Path.Combine(context.Run.Paths.RunDirectory, file), png);
            context.Run.Artifacts.Add(new ResultArtifact { Kind = "screenshot", Path = file, Sha256 = Hashing.Sha256Hex(png) });
            context.Pass(file);
        }

        /// <summary><c>{ "pattern": "regex", "fromStart": false }</c> waits for a log line that matches, logged after the step began (or any of the latest lines when <c>fromStart</c>).</summary>
        public static IEnumerator LogExpect(StepContext context)
        {
            var pattern = context.String("pattern");
            Regex regex;
            try
            {
                regex = new Regex(pattern, RegexOptions.CultureInvariant);
            }
            catch (ArgumentException ex)
            {
                throw new StepArgumentException($"'pattern' is not a regular expression: {ex.Message}");
            }
            var after = context.Bool("fromStart", false) ? 0 : context.Run.Logs.Sequence;
            context.Note($"waiting for a log line matching /{pattern}/");
            string line;
            while (!context.Run.Logs.TryFind(regex, after, out line))
                yield return null;
            context.Pass(line);
        }

        /// <summary><c>{ "path": "a/b.txt", "text": "...", "root": "work", "overwrite": false }</c> writes a file below the work folder (or, with
        /// <c>"root": "persistent"</c>, below the persistent data path) and records it in the cleanup ledger. An existing file is refused unless <c>overwrite</c>;
        /// then it is backed up and restored by the cleanup.</summary>
        public static IEnumerator FilePut(StepContext context)
        {
            var root = context.String("root", "work");
            if (root != "work" && root != "persistent")
                throw new StepArgumentException("'root' must be 'work' or 'persistent'");
            var path = RunPaths.Below(root == "work" ? context.Run.Paths.Work : context.Run.Paths.PersistentDataPath, context.String("path"));
            var text = context.String("text", string.Empty);
            string backup = null;
            if (File.Exists(path))
            {
                if (!context.Bool("overwrite", false))
                {
                    context.Fail($"'{path}' exists; set \"overwrite\": true to replace it");
                    yield break;
                }
                var backups = Path.Combine(context.Run.Paths.RunDirectory, "backup");
                Directory.CreateDirectory(backups);
                backup = Path.Combine(backups, context.Run.Ledger.Entries.Count + ".bak");
                File.Copy(path, backup, true);
            }
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            context.Run.Ledger.Record(FileCleanupHandler.FileKind, path, backup);
            File.WriteAllText(path, text, new UTF8Encoding(false));
            context.Pass(path);
        }

        /// <summary><c>{ "exitCode": 0 }</c> asks the harness to quit after the result is written. Use it as the last step.</summary>
        public static IEnumerator AppQuit(StepContext context)
        {
            context.Run.QuitRequested = true;
            context.Run.QuitExitCode = (int)context.Number("exitCode", 0);
            yield break;
        }

        /// <summary><c>{ "action": "allow-notifications", "args": { } }</c> asks the host to do something only the OS can (a system dialog). The request
        /// goes into the heartbeat; the host answers with <c>reply-&lt;id&gt;.json</c> (<c>{"status": "PASS", "message": "..."}</c>) in the inbox.
        /// Refused inside an open probe window: external automation would distort the measurement.</summary>
        public static IEnumerator ExternalUi(StepContext context)
        {
            if (context.Run.Window != null)
            {
                context.Fail("external.ui is refused inside an open probe window");
                yield break;
            }
            var action = context.String("action");
            if (action.Length == 0)
                throw new StepArgumentException("'action' must not be empty");
            // The run id keeps a late reply of an earlier run from answering this one.
            var id = $"{context.Run.RunId}-ui-{context.Run.RequestCounter++}-{context.Index}";
            var request = new HostRequest { Id = id, Action = action };
            if (context.Args.TryGet("args", out var args))
            {
                if (args.Kind != JsonKind.Object)
                    throw new StepArgumentException("'args' must be an object");
                request.Args = args;
            }
            var reply = Path.Combine(context.Run.Paths.Inbox, "reply-" + id + ".json");
            context.Run.PendingRequest = request;
            context.Note($"waiting for the host to answer '{action}'");
            JsonValue document = null;
            try
            {
                // A reply the host is still writing does not parse or cannot be read yet: try again next frame, until the step times out.
                while (document == null)
                {
                    if (File.Exists(reply))
                    {
                        try
                        {
                            document = JsonValue.Parse(File.ReadAllText(reply));
                        }
                        catch (Exception ex) when (ex is JsonException || ex is IOException)
                        {
                            document = null;
                        }
                    }
                    if (document == null)
                        yield return null;
                }
            }
            finally
            {
                context.Run.PendingRequest = null;
            }
            File.Move(reply, reply + ".done-" + UtcFormat.Compact(context.Run.Clock.UtcNow));
            var status = document.TryGet("status", out var statusValue) && statusValue.Kind == JsonKind.String ? statusValue.AsString : null;
            if (status == null || Array.IndexOf(ValidationStatus.All, status) < 0)
                throw new StepArgumentException($"the host reply for '{id}' has no valid 'status'");
            var message = document.TryGet("message", out var messageValue) && messageValue.Kind == JsonKind.String ? messageValue.AsString : null;
            context.SetStatus(status, message ?? $"host answered {status}");
        }
    }
}
