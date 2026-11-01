using System.Collections;
using Valtrion.Unity.Testing;
using UnityEngine.TestTools;

namespace Valtrion.Examples
{
    public sealed class ValtrionSmokeTests
    {
        // Set GDV_PLAN, GDV_OUT_ROOT and GDV_RUN_ID before starting the Editor.
        [UnityTest]
        public IEnumerator RunsTheConfiguredPlan()
        {
            yield return PlanTest.RunFromEnvironment();
        }
    }
}
