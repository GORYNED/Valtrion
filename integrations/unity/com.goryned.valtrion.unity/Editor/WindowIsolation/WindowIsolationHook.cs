#if UNITY_EDITOR_OSX
using System;
using System.Runtime.InteropServices;
using UnityEditor;
using UnityEngine;

namespace Valtrion.Unity.Editor.WindowIsolation
{
    /// <summary>Keeps the windows that tests open (an <c>EditorWindow</c>, a utility window, a popup) off the user's screen on macOS.
    /// It acts only in a batch-mode Editor (<c>-batchmode</c>) and only while the environment variable <c>GDV_ISOLATE_WINDOWS=1</c> is
    /// set, which <c>valtrion unity run</c> does for the Editor it supervises; an interactive Editor is never touched, even when the
    /// variable is set by mistake (a warning says so). It then (1) sets the activation policy of the Editor process to "prohibited", so
    /// the process can never become the active application and take the keyboard from another one, and (2) makes every window of the
    /// process fully transparent and deaf to the mouse the moment it is ordered onto the screen (<c>-[NSWindow orderWindow:relativeTo:]</c>,
    /// the call the usual ways of showing a window end in) and again on every Editor update, which also catches a window of a class that
    /// overrides that call without calling the original. The windows are still created, laid out and drawn; they are only never visible
    /// or reachable. The hook is taken off again before a domain reload and on quit, because the callback lives in this assembly.
    /// Anything unexpected while the hook is installed makes it do nothing rather than fail a test.</summary>
    [InitializeOnLoad]
    internal static class WindowIsolationHook
    {
        internal const string EnvironmentVariable = "GDV_ISOLATE_WINDOWS";

        const string Objc = "/usr/lib/libobjc.A.dylib";
        const string SystemLib = "/usr/lib/libSystem.B.dylib";
        const long NSApplicationActivationPolicyProhibited = 2;
        const long NSWindowOut = 0;

        [DllImport(Objc)] static extern IntPtr objc_getClass(string name);
        [DllImport(Objc)] static extern IntPtr sel_registerName(string name);
        [DllImport(Objc)] static extern IntPtr class_getInstanceMethod(IntPtr cls, IntPtr selector);
        [DllImport(Objc)] static extern IntPtr class_replaceMethod(IntPtr cls, IntPtr selector, IntPtr implementation, string types);
        [DllImport(Objc)] static extern IntPtr method_getImplementation(IntPtr method);
        [DllImport(SystemLib)] static extern int pthread_main_np();
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr Send(IntPtr target, IntPtr selector);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr SendLong(IntPtr target, IntPtr selector, long value);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr SendDouble(IntPtr target, IntPtr selector, double value);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr SendBool(IntPtr target, IntPtr selector, sbyte value);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern ulong SendCount(IntPtr target, IntPtr selector);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern double SendReturningDouble(IntPtr target, IntPtr selector);

        delegate void OrderWindow(IntPtr window, IntPtr selector, long place, long relativeTo);

        static IntPtr s_application, s_windowClass, s_orderSelector, s_originalImplementation, s_ourImplementation;
        static IntPtr s_activationPolicy, s_setActivationPolicy, s_setAlpha, s_setIgnoresMouse, s_alphaValue, s_windows, s_count, s_objectAtIndex;
        static OrderWindow s_original, s_replacement;
        static bool s_hooked, s_policyChecked;

        static WindowIsolationHook()
        {
            if (Environment.GetEnvironmentVariable(EnvironmentVariable) != "1")
                return;
            if (!Application.isBatchMode)
            {
                Debug.LogWarning("Valtrion: " + EnvironmentVariable + " is set but this Editor is not in batch mode; window isolation is ignored.");
                return;
            }
            try
            {
                Enable();
            }
            catch (Exception error)
            {
                try { Unhook(); } catch (Exception) { /* nothing more to undo */ }
                Debug.LogWarning("Valtrion: window isolation is not available here (" + error.Message + "); test windows may show on the screen.");
            }
        }

        static void Enable()
        {
            s_setActivationPolicy = sel_registerName("setActivationPolicy:");
            s_activationPolicy = sel_registerName("activationPolicy");
            s_setAlpha = sel_registerName("setAlphaValue:");
            s_setIgnoresMouse = sel_registerName("setIgnoresMouseEvents:");
            s_alphaValue = sel_registerName("alphaValue");
            s_windows = sel_registerName("windows");
            s_count = sel_registerName("count");
            s_objectAtIndex = sel_registerName("objectAtIndex:");

            s_application = Send(objc_getClass("NSApplication"), sel_registerName("sharedApplication"));
            s_windowClass = objc_getClass("NSWindow");
            s_orderSelector = sel_registerName("orderWindow:relativeTo:");
            if (s_application == IntPtr.Zero || s_windowClass == IntPtr.Zero)
                throw new InvalidOperationException("NSApplication or NSWindow was not found");
            IntPtr method = class_getInstanceMethod(s_windowClass, s_orderSelector);
            if (method == IntPtr.Zero)
                throw new InvalidOperationException("-[NSWindow orderWindow:relativeTo:] was not found");
            s_originalImplementation = method_getImplementation(method);
            if (s_originalImplementation == IntPtr.Zero)
                throw new InvalidOperationException("-[NSWindow orderWindow:relativeTo:] has no implementation");

            s_original = Marshal.GetDelegateForFunctionPointer<OrderWindow>(s_originalImplementation);
            s_replacement = HideThenOrder;
            s_ourImplementation = Marshal.GetFunctionPointerForDelegate(s_replacement);
            class_replaceMethod(s_windowClass, s_orderSelector, s_ourImplementation, "v@:qq");
            s_hooked = true;

            AssemblyReloadEvents.beforeAssemblyReload += Unhook;
            EditorApplication.quitting += Unhook;
            EditorApplication.update += HideAll;

            // Last, once the hook is in: the policy is what keeps the process from taking the focus. The Editor may not have finished
            // starting AppKit yet, so the result is checked on the first update, when a refusal is reported once.
            Prohibit();
        }

        static void Unhook()
        {
            EditorApplication.update -= HideAll;
            AssemblyReloadEvents.beforeAssemblyReload -= Unhook;
            EditorApplication.quitting -= Unhook;
            if (!s_hooked)
                return;
            s_hooked = false;
            // Put the original back only when ours is still the one in place: another hook installed after ours must not be lost.
            IntPtr method = class_getInstanceMethod(s_windowClass, s_orderSelector);
            if (method != IntPtr.Zero && method_getImplementation(method) == s_ourImplementation)
                class_replaceMethod(s_windowClass, s_orderSelector, s_originalImplementation, "v@:qq");
            else
                Debug.LogWarning("Valtrion: another hook replaced -[NSWindow orderWindow:relativeTo:] after window isolation; it is left as it is, " +
                                 "and if it calls the method it replaced, the Editor may crash after the next domain reload.");
        }

        // Sets the policy and reads it back: the BOOL result of the setter alone does not tell what the process ends up with.
        static bool Prohibit()
        {
            if ((long)Send(s_application, s_activationPolicy) != NSApplicationActivationPolicyProhibited)
                SendLong(s_application, s_setActivationPolicy, NSApplicationActivationPolicyProhibited);
            return (long)Send(s_application, s_activationPolicy) == NSApplicationActivationPolicyProhibited;
        }

        static void Hide(IntPtr window)
        {
            if (window == IntPtr.Zero)
                return;
            SendDouble(window, s_setAlpha, 0.0);
            SendBool(window, s_setIgnoresMouse, 1);
        }

        // Runs for every window that is ordered in front of or behind another; an exception must never reach native code.
        static void HideThenOrder(IntPtr window, IntPtr selector, long place, long relativeTo)
        {
            try
            {
                // AppKit windows are not thread-safe; a call from another thread is left to the pass on the main thread (HideAll).
                if (s_hooked && place != NSWindowOut && pthread_main_np() != 0)
                    Hide(window);
            }
            catch (Exception)
            {
                // The window stays as it is; HideAll tries again on the next update.
            }
            s_original(window, selector, place, relativeTo);
        }

        // The Editor changes the policy and the transparency of its windows itself now and then, and some windows are shown without
        // passing through the call above: take them back.
        static void HideAll()
        {
            try
            {
                bool prohibited = Prohibit();
                if (!s_policyChecked)
                {
                    s_policyChecked = true;
                    if (!prohibited)
                        Debug.LogWarning("Valtrion: the Editor process did not take the 'prohibited' activation policy; windows are still hidden, but it may take the focus.");
                }
                IntPtr windows = Send(s_application, s_windows);
                ulong count = SendCount(windows, s_count);
                for (ulong index = 0; index < count; index++)
                {
                    IntPtr window = SendLong(windows, s_objectAtIndex, (long)index);
                    if (SendReturningDouble(window, s_alphaValue) != 0.0)
                        Hide(window);
                }
            }
            catch (Exception error)
            {
                Debug.LogWarning("Valtrion: window isolation stopped: " + error.Message);
                EditorApplication.update -= HideAll;
            }
        }
    }
}
#endif
