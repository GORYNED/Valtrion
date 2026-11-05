using System.Collections;
using System.IO;
using Valtrion.Unity.Contracts;
using Valtrion.Unity.Harness;
using NUnit.Framework;

namespace Valtrion.Unity.Testing
{
    /// <summary>Runs a plan inside a Unity Test Framework test, in Editor PlayMode or a player test build:
    /// <c>[UnityTest] public IEnumerator Smoke() { yield return PlanTest.Run("validation/device/plans/smoke.plan.json"); }</c>.
    /// The test fails unless the result is PASS; <c>result.json</c> and the NUnit XML (see <see cref="NUnitXmlResultWriter"/>) are written to the run folder.</summary>
    public static class PlanTest
    {
        /// <summary>The run folder of the latest plan test, or null.</summary>
        public static string LastRunDirectory { get; private set; }

        public static IEnumerator Run(string planPath) => Run(planPath, null);

        /// <summary>Runs the plan named by the same inputs a launched player reads (<c>-gdv-plan</c> / <c>GDV_PLAN</c> / intent extra, with
        /// <c>-gdv-out</c>/<c>GDV_OUT_ROOT</c> and <c>-gdv-run-id</c>/<c>GDV_RUN_ID</c>). The plan file is not renamed. Fails the test when no plan is given.</summary>
        public static IEnumerator RunFromEnvironment(RunOptions options = null)
        {
            Assert.IsTrue(ValidationHarness.TryStartFromInbox(null, options, false), "no plan given: set -gdv-plan, GDV_PLAN or the gdv-plan intent extra");
            var runner = ValidationHarness.Current;
            LastRunDirectory = runner.RunDirectory;
            while (!runner.Finished)
                yield return null;
            Assert.AreEqual(ValidationStatus.Pass, runner.Result.Status, $"plan '{runner.Result.Plan.Id}': {runner.Result.Reason} (result: {runner.ResultPath})");
        }

        public static IEnumerator Run(string planPath, RunOptions options)
        {
            var text = File.ReadAllText(planPath);
            var plan = PlanDocument.Parse(text);
            var runner = ValidationHarness.Start(plan, text, options);
            LastRunDirectory = runner.RunDirectory;
            while (!runner.Finished)
                yield return null;
            Assert.AreEqual(ValidationStatus.Pass, runner.Result.Status, $"plan '{plan.Id}': {runner.Result.Reason} (result: {runner.ResultPath})");
        }
    }
}
