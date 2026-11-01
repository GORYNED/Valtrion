using System.IO;
using System.Text;
using Valtrion.Unity.Harness;
using Valtrion.Unity.Testing;
using NUnit.Framework.Interfaces;
using UnityEngine.TestRunner;

[assembly: TestRunCallback(typeof(NUnitXmlResultWriter))]

namespace Valtrion.Unity.Testing
{
    /// <summary>Writes the NUnit XML of a test run next to the <c>result.json</c> of the plan that ran in it (<c>nunit-results.xml</c>). A test run
    /// without a plan test writes nothing.</summary>
    public sealed class NUnitXmlResultWriter : ITestRunCallback
    {
        public void RunStarted(ITest testsToRun)
        {
        }

        public void TestStarted(ITest test)
        {
        }

        public void TestFinished(ITestResult result)
        {
        }

        public void RunFinished(ITestResult testResults)
        {
            var directory = PlanTest.LastRunDirectory;
            if (string.IsNullOrEmpty(directory) || !Directory.Exists(directory))
                return;
            AtomicFile.WriteAllText(Path.Combine(directory, "nunit-results.xml"), testResults.ToXml(true).OuterXml);
        }
    }
}
