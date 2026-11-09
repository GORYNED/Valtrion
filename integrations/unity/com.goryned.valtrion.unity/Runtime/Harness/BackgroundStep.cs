using System;
using System.Collections;
using System.Globalization;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Harness
{
    /// <summary>The <c>app.background</c> step: asks the host to send the running app to the background, keep it there and bring it back. An app cannot do this to
    /// itself on every platform, so the step is a request to the host, like <c>external.ui</c>, which it uses: the host answers when the app is in the foreground again.</summary>
    public static class BackgroundStep
    {
        /// <summary>The action name the host receives in the request; a pipeline's <c>externalUi.command</c> (or a host of the caller's own) implements it.</summary>
        public const string Action = "app.background";

        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register(Action, Background);
        }

        /// <summary><c>{ "seconds": 5, "metric": "backgroundMs" }</c> puts the request <c>app.background</c> with <c>args.seconds</c> into the heartbeat and waits for the
        /// host's reply, exactly as <c>external.ui</c> does (state <c>waiting-host</c>; reply <c>reply-&lt;id&gt;.json</c> with <c>status</c> and <c>message</c>). The host
        /// moves the app to the background, waits that many seconds and brings the app back, and answers PASS when the app is in the foreground again. The step's
        /// <c>timeoutSec</c> must cover the time in the background, which the run clock counts. With <c>metric</c> the milliseconds from the request to the reply are
        /// reported (the time the app was away, including the host's work). Refused inside an open probe window: external automation would distort the measurement.
        /// Valtrion supplies no host action: how an app is sent to the background and back is the host's, and what the app does when it is paused is the project's to check.</summary>
        public static IEnumerator Background(StepContext context)
        {
            var seconds = context.Number("seconds");
            if (!(seconds > 0) || double.IsInfinity(seconds))
                throw new StepArgumentException("'seconds' must be greater than zero");
            var metric = context.Args.TryGet("metric", out _) ? context.String("metric") : null;
            if (context.Run.Window != null)
            {
                context.Fail(Action + " is refused inside an open probe window");
                yield break;
            }
            var request = new PlanStep
            {
                Type = "external.ui",
                TimeoutSec = context.Step.TimeoutSec,
                Args = JsonValue.NewObject().Set("action", JsonValue.From(Action)).Set("args", JsonValue.NewObject().Set("seconds", JsonValue.From(seconds))),
            };
            var inner = new StepContext(context.Run, request, context.Index, context.Phase, context.Iteration) { Round = context.Round };
            var start = context.Clock.NowMs;
            var routine = BuiltInSteps.ExternalUi(inner);
            try
            {
                while (routine.MoveNext())
                {
                    if (!string.IsNullOrEmpty(inner.Message))
                        context.Note(inner.Message);
                    yield return routine.Current;
                }
            }
            finally
            {
                (routine as IDisposable)?.Dispose();
            }
            if (inner.Status == ValidationStatus.Pass && metric != null)
                context.AddMetric(metric, "ms", context.Clock.NowMs - start);
            context.SetStatus(inner.Status, inner.Status == ValidationStatus.Pass ? $"the app went to the background for {seconds.ToString(CultureInfo.InvariantCulture)} s and came back: {inner.Message}" : inner.Message);
        }
    }
}
