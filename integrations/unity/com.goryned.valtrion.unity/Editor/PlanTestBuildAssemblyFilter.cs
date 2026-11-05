using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build;

namespace Valtrion.Unity.Editor
{
    /// <summary>Keeps the plan test helper <c>Valtrion.Unity.PlayModeTests</c> out of ordinary player builds. It references NUnit and
    /// <c>UnityEngine.TestRunner</c>, which the Test Framework strips from a player unless the build includes test assemblies (or the
    /// PlayMode test runner is enabled for players); the helper follows the same rule so a development player never ships it with
    /// missing references. A test player build keeps it.</summary>
    internal sealed class PlanTestBuildAssemblyFilter : IFilterBuildAssemblies
    {
        internal const string HelperFileName = "Valtrion.Unity.PlayModeTests.dll";

        public int callbackOrder => 0;

        public string[] OnFilterAssemblies(BuildOptions buildOptions, string[] assemblies)
            => Filter(buildOptions, PlayModeTestRunnerEnabled(), assemblies);

        // PlayerSettings.playModeTestRunnerEnabled is internal to the Editor (the Test Framework reads it through InternalsVisibleTo);
        // read it by reflection and treat a missing property as off, which keeps the helper out as the Test Framework keeps its own.
        static bool PlayModeTestRunnerEnabled()
        {
            var property = typeof(PlayerSettings).GetProperty("playModeTestRunnerEnabled",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);
            return property != null && property.PropertyType == typeof(bool) && (bool)property.GetValue(null);
        }

        internal static string[] Filter(BuildOptions buildOptions, bool playModeTestRunnerEnabled, string[] assemblies)
        {
            if (assemblies == null || (buildOptions & BuildOptions.IncludeTestAssemblies) == BuildOptions.IncludeTestAssemblies || playModeTestRunnerEnabled)
                return assemblies;
            return assemblies.Where(path => !string.Equals(Path.GetFileName(path), HelperFileName, StringComparison.OrdinalIgnoreCase)).ToArray();
        }
    }
}
