using System;
using System.Reflection;
using Valtrion.Unity.Contracts;

namespace Valtrion.Unity.Harness
{
    /// <summary>Describes an exception thrown by a step so the result points at the place: the exception type, its message and the first
    /// stack frame. A bare "Object reference not set to an instance of an object" cannot be located; the full exception is logged as well.</summary>
    public static class Faults
    {
        const int MaxFrameLength = 300;

        /// <summary>The text of a step fault: <c>Type: message (at first.Frame())</c>; an argument error keeps its own wording.</summary>
        public static string Describe(Exception ex)
        {
            if (ex is StepArgumentException)
                return "invalid arguments: " + ex.Message;
            // A wrapper's own throw site says nothing (Invoke, a type initializer); the cause inside it is what to locate.
            var prefix = string.Empty;
            while ((ex is TargetInvocationException || ex is TypeInitializationException || ex is AggregateException) && ex.InnerException != null)
            {
                prefix += ex.GetType().Name + " -> ";
                ex = ex.InnerException;
            }
            return $"{prefix}{ex.GetType().Name}: {ex.Message}{FirstFrame(ex)}";
        }

        /// <summary>The first stack frame as <c> (at Type.Method(...))</c>, or an empty string when the exception has no stack trace.</summary>
        public static string FirstFrame(Exception ex)
        {
            string trace;
            try
            {
                trace = ex.StackTrace;
            }
            catch (Exception)
            {
                return string.Empty;
            }
            if (string.IsNullOrEmpty(trace))
                return string.Empty;
            foreach (var line in trace.Split('\n'))
            {
                var frame = line.Trim();
                if (frame.Length == 0)
                    continue;
                if (frame.StartsWith("at ", StringComparison.Ordinal))
                    frame = frame.Substring(3);
                if (frame.Length > MaxFrameLength)
                    frame = frame.Substring(0, MaxFrameLength) + "...";
                return $" (at {frame})";
            }
            return string.Empty;
        }
    }
}
