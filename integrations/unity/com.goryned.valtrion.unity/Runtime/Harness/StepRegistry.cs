using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;

namespace Valtrion.Unity.Harness
{
    /// <summary>Marks a static method <c>IEnumerator Name(StepContext context)</c> as the handler of a plan step type.
    /// The method yields <c>null</c> or <c>WaitForEndOfFrame</c> to wait and sets its outcome on the context. In a stripped
    /// IL2CPP player the method must survive linking: put it in an assembly preserved by a <c>link.xml</c>, add
    /// <c>[UnityEngine.Scripting.Preserve]</c> to it, or register it explicitly with <see cref="StepRegistry.Register"/>.</summary>
    [AttributeUsage(AttributeTargets.Method, AllowMultiple = false, Inherited = false)]
    public sealed class ValidationStepAttribute : Attribute
    {
        public ValidationStepAttribute(string type)
        {
            Type = type;
        }

        public string Type { get; }
    }

    public delegate IEnumerator StepHandler(StepContext context);

    /// <summary>The step types a run can execute. Built-in steps are registered explicitly; project steps are found through
    /// <see cref="ValidationStepAttribute"/> or registered by code, which is the fallback that needs no reflection.</summary>
    public sealed class StepRegistry
    {
        readonly Dictionary<string, StepHandler> _handlers = new Dictionary<string, StepHandler>(StringComparer.Ordinal);

        public IEnumerable<string> Types => _handlers.Keys;

        /// <summary>Problems met while discovering steps (duplicates, wrong signatures); the runner reports them as unverified.</summary>
        public List<string> DiscoveryProblems { get; } = new List<string>();

        public static StepRegistry CreateDefault(bool discover)
        {
            var registry = new StepRegistry();
            BuiltInSteps.RegisterAll(registry);
            if (discover)
                registry.Discover();
            return registry;
        }

        public void Register(string type, StepHandler handler)
        {
            if (string.IsNullOrEmpty(type))
                throw new ArgumentException("a step type is required", nameof(type));
            if (handler == null)
                throw new ArgumentNullException(nameof(handler));
            if (_handlers.ContainsKey(type))
                throw new InvalidOperationException($"step type '{type}' is already registered");
            _handlers.Add(type, handler);
        }

        public bool TryGet(string type, out StepHandler handler) => _handlers.TryGetValue(type, out handler);

        /// <summary>The package's plan test helper; it holds no steps and references the Test Framework, so discovery never loads its types.</summary>
        internal const string PlanTestHelperAssembly = "Valtrion.Unity.PlayModeTests";

        /// <summary>Registers every <see cref="ValidationStepAttribute"/> method of the loaded assemblies except this one and the package's plan test helper.</summary>
        public int Discover()
        {
            var found = 0;
            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (assembly.IsDynamic || assembly == typeof(StepRegistry).Assembly || IsPlatformAssembly(assembly.GetName().Name)
                    || assembly.GetName().Name == PlanTestHelperAssembly)
                    continue;
                try
                {
                    found += Discover(assembly);
                }
                catch (Exception ex)
                {
                    DiscoveryProblems.Add($"assembly {assembly.GetName().Name} was skipped during step discovery: {ex.GetType().Name}: {ex.Message}");
                }
            }
            return found;
        }

        public int Discover(Assembly assembly)
        {
            var found = 0;
            Type[] types;
            try
            {
                types = assembly.GetTypes();
            }
            catch (ReflectionTypeLoadException ex)
            {
                types = Array.FindAll(ex.Types, type => type != null);
            }
            const BindingFlags Flags = BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
            foreach (var type in types)
            {
                foreach (var method in type.GetMethods(Flags))
                {
                    var attribute = (ValidationStepAttribute)Attribute.GetCustomAttribute(method, typeof(ValidationStepAttribute), false);
                    if (attribute == null)
                        continue;
                    var handler = (StepHandler)Delegate.CreateDelegate(typeof(StepHandler), method, false);
                    if (handler == null)
                    {
                        DiscoveryProblems.Add($"{type.FullName}.{method.Name} is not 'static IEnumerator (StepContext)', step '{attribute.Type}' was not registered");
                        continue;
                    }
                    if (_handlers.ContainsKey(attribute.Type))
                    {
                        DiscoveryProblems.Add($"step type '{attribute.Type}' of {type.FullName}.{method.Name} is already registered");
                        continue;
                    }
                    _handlers.Add(attribute.Type, handler);
                    found++;
                }
            }
            return found;
        }

        static bool IsPlatformAssembly(string name) =>
            name == "mscorlib" || name == "netstandard" || name.StartsWith("System", StringComparison.Ordinal) || name.StartsWith("Unity.", StringComparison.Ordinal) || name.StartsWith("UnityEngine", StringComparison.Ordinal) || name.StartsWith("UnityEditor", StringComparison.Ordinal)
            || name.StartsWith("Mono.", StringComparison.Ordinal) || name.StartsWith("nunit", StringComparison.OrdinalIgnoreCase) || name.StartsWith("Microsoft", StringComparison.Ordinal)
            || name.StartsWith("Newtonsoft", StringComparison.Ordinal) || name.StartsWith("ExCSS", StringComparison.Ordinal) || name.StartsWith("JetBrains", StringComparison.Ordinal);
    }

    /// <summary>Named conditions that <c>waitUntil</c> polls. The app (or a project step provider) registers them.</summary>
    public sealed class ConditionRegistry
    {
        public static readonly ConditionRegistry Shared = new ConditionRegistry();

        readonly Dictionary<string, Func<bool>> _conditions = new Dictionary<string, Func<bool>>(StringComparer.Ordinal);

        public void Register(string name, Func<bool> condition) => _conditions[name] = condition ?? throw new ArgumentNullException(nameof(condition));

        public bool Unregister(string name) => _conditions.Remove(name);

        public bool TryGet(string name, out Func<bool> condition) => _conditions.TryGetValue(name, out condition);
    }
}
