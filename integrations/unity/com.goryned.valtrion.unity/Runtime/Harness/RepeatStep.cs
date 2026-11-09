using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Json;
using UnityEngine;

namespace Valtrion.Unity.Harness
{
    /// <summary>The <c>repeat</c> step: runs a list of steps a given number of times inside one step, so a plan can measure wear and the frequency of faults
    /// under constant repetition (for example arming and disarming a camera without recording) and still get a summary, not only "failed at step N".</summary>
    public static class RepeatStep
    {
        public static void RegisterAll(StepRegistry registry)
        {
            registry.Register("repeat", Repeat);
        }

        /// <summary><c>{ "count": 50, "steps": [ { "type": "arm", "timeoutSec": 20 } ], "continueOnFail": false, "prefix": "repeat", "roundMetric": "armCycleMs" }</c>
        /// runs <c>steps</c> (each with its own <c>type</c> and <c>timeoutSec</c>, as in a plan; <c>repeat</c> cannot be nested) <c>count</c> times in order. The
        /// <c>timeoutSec</c> of the <c>repeat</c> step bounds the whole loop. The metrics of the inner steps get one sample per round, so the result has their
        /// n, median, p90 and max over the rounds. The step itself reports <c>&lt;prefix&gt;.rounds</c> (rounds run) and <c>&lt;prefix&gt;.failedRounds</c>, and the
        /// duration of every round that ran to its end (a failed one included) as <c>roundMetric</c> when given (<c>prefix</c> defaults to <c>repeat</c>). A round whose step ends FAIL is a failed round: the rest
        /// of that round is skipped; the loop stops there, or goes on with the next round when <c>continueOnFail</c> is true, and the step ends FAIL naming the
        /// number of failed rounds and the first failure. A step that ends BLOCKED, NEEDS_PERMISSION or TIMEOUT stops the loop with that status.</summary>
        public static IEnumerator Repeat(StepContext context)
        {
            var count = Count(context);
            var children = ReadChildren(context);
            var prefix = Identifiers.Sanitize(context.String("prefix", "repeat"), "repeat");
            var roundMetric = context.Args.TryGet("roundMetric", out _) ? context.String("roundMetric") : null;
            var continueOnFail = context.Bool("continueOnFail", false);
            var registry = context.Run.Steps ?? throw new InvalidOperationException("the run has no step registry");
            long rounds = 0;
            long failedRounds = 0;
            string firstFailure = null;
            string stoppedStatus = null;
            string stoppedMessage = null;
            for (long round = 1; round <= count && stoppedStatus == null; round++)
            {
                context.Note($"round {round} of {count}");
                var roundStartMs = context.Clock.NowMs;
                var roundFailed = false;
                foreach (var child in children)
                {
                    var childContext = new StepContext(context.Run, child, context.Index, context.Phase, context.Iteration) { Round = round };
                    if (!registry.TryGet(child.Type, out var handler))
                    {
                        childContext.SetStatus(ValidationStatus.Blocked, $"step type '{child.Type}' is not registered in this build");
                    }
                    else
                    {
                        IEnumerator routine = null;
                        try
                        {
                            routine = handler(childContext);
                        }
                        catch (Exception ex)
                        {
                            childContext.SetStatus(ValidationStatus.Fail, Faults.Describe(ex));
                        }
                        var startMs = context.Clock.NowMs;
                        // The inner routine is disposed however this step ends, also when the runner disposes this iterator because the step timed out,
                        // the plan budget ran out or the run was aborted: the inner step's own cleanup (finally blocks) must run.
                        try
                        {
                            while (routine != null)
                            {
                                bool more;
                                try
                                {
                                    more = routine.MoveNext();
                                }
                                catch (Exception ex)
                                {
                                    childContext.SetStatus(ValidationStatus.Fail, Faults.Describe(ex));
                                    break;
                                }
                                if (!more)
                                    break;
                                if (context.Clock.NowMs - startMs > child.TimeoutSec * 1000.0)
                                {
                                    childContext.SetStatus(ValidationStatus.Timeout, $"step timeout of {child.TimeoutSec} s" + Suffix(childContext.Message));
                                    break;
                                }
                                var yielded = routine.Current;
                                if (yielded != null && !(yielded is WaitForEndOfFrame))
                                {
                                    childContext.SetStatus(ValidationStatus.Fail, $"step yielded {yielded.GetType().Name}; a step may yield only null or WaitForEndOfFrame so its timeout holds");
                                    break;
                                }
                                yield return yielded;
                            }
                        }
                        finally
                        {
                            Dispose(routine);
                        }
                    }
                    // At least one frame per inner step: a long repeat of instant steps must not freeze the player or outrun the budget.
                    yield return null;
                    if (childContext.Status == ValidationStatus.Pass)
                        continue;
                    var text = $"round {round}, {child.Type} {childContext.Status}" + Suffix(childContext.Message);
                    if (childContext.Status == ValidationStatus.Fail)
                    {
                        roundFailed = true;
                        firstFailure = firstFailure ?? text;
                        break;
                    }
                    stoppedStatus = childContext.Status;
                    stoppedMessage = text;
                    break;
                }
                rounds++;
                if (roundMetric != null && stoppedStatus == null)
                    context.AddMetric(roundMetric, "ms", context.Clock.NowMs - roundStartMs);
                if (roundFailed)
                {
                    failedRounds++;
                    if (!continueOnFail)
                        break;
                }
            }
            context.AddMetric(prefix + ".rounds", "count", rounds);
            context.AddMetric(prefix + ".failedRounds", "count", failedRounds);
            var summary = $"{rounds.ToString(CultureInfo.InvariantCulture)} of {count.ToString(CultureInfo.InvariantCulture)} rounds run, {failedRounds.ToString(CultureInfo.InvariantCulture)} failed";
            if (stoppedStatus != null)
                context.SetStatus(stoppedStatus, summary + "; " + stoppedMessage);
            else if (failedRounds > 0)
                context.Fail(summary + "; first failure: " + firstFailure);
            else
                context.Pass(summary);
        }

        static long Count(StepContext context)
        {
            var count = context.Number("count");
            if (count < 1 || count != Math.Floor(count) || count > int.MaxValue)
                throw new StepArgumentException("'count' must be a whole number of at least 1");
            return (long)count;
        }

        static List<PlanStep> ReadChildren(StepContext context)
        {
            if (!context.Args.TryGet("steps", out var steps) || steps.Kind != JsonKind.Array || steps.Count == 0)
                throw new StepArgumentException("argument 'steps' must be a non-empty array");
            var children = new List<PlanStep>();
            var index = 0;
            foreach (var node in steps.Items)
            {
                var where = $"steps[{index++}]";
                if (node.Kind != JsonKind.Object)
                    throw new StepArgumentException($"{where} must be an object with 'type' and 'timeoutSec'");
                if (!node.TryGet("type", out var type) || type.Kind != JsonKind.String || type.AsString.Length == 0)
                    throw new StepArgumentException($"{where} needs a 'type'");
                if (type.AsString == "repeat")
                    throw new StepArgumentException($"{where}: 'repeat' cannot be nested");
                if (!node.TryGet("timeoutSec", out var timeout) || timeout.Kind != JsonKind.Number || timeout.AsNumber < 1 || timeout.AsNumber != Math.Floor(timeout.AsNumber) || timeout.AsNumber > int.MaxValue)
                    throw new StepArgumentException($"{where} needs a whole 'timeoutSec' of at least 1");
                var args = JsonValue.NewObject();
                foreach (var member in node.Members)
                {
                    if (member.Key != "type" && member.Key != "timeoutSec")
                        args.Set(member.Key, member.Value);
                }
                children.Add(new PlanStep { Type = type.AsString, TimeoutSec = (int)timeout.AsNumber, Args = args });
            }
            return children;
        }

        static void Dispose(IEnumerator routine)
        {
            if (routine is IDisposable disposable)
            {
                try
                {
                    disposable.Dispose();
                }
                catch (Exception)
                {
                    // An inner step that cannot clean up must not hide the outcome of the round.
                }
            }
        }

        static string Suffix(string message) => string.IsNullOrEmpty(message) ? string.Empty : ": " + message;
    }
}
