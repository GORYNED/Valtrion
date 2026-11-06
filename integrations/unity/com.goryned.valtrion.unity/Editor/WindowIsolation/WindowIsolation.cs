#if UNITY_EDITOR_OSX
using System;
using System.Runtime.InteropServices;
using UnityEditor;
using UnityEngine;

namespace Valtrion.Unity.Editor.WindowIsolation
{
    /// <summary>Keeps the windows that tests open (an <c>EditorWindow</c>, a utility window, a popup) off the user's screen on macOS.
    /// While <c>valtrion unity run</c> supervises the Editor it sets the environment variable <c>GDV_ISOLATE_WINDOWS=1</c>; this class then
    /// (1) sets the activation policy of the Editor process to "prohibited", so the process can never become the active application and
    /// take the keyboard from another one, and (2) makes every window of the process fully transparent and deaf to the mouse the moment it
    /// is ordered onto the screen (<c>-[NSWindow orderWindow:relativeTo:]</c> is the one call all ways of showing a window pass through)
    /// and again on every Editor update. The windows are still created, laid out and drawn by the Editor, so the tests that use them run as
    /// before; they are only never visible or reachable. Without the variable, and on other systems, nothing happens. Nothing here changes a
    /// test or its result. The hook is taken off again before a domain reload, because the callback lives in this assembly.</summary>
    [InitializeOnLoad]
    internal static class WindowIsolation
    {
        internal const string EnvironmentVariable = "GDV_ISOLATE_WINDOWS";

        const string Objc = "/usr/lib/libobjc.A.dylib";
        const long NSApplicationActivationPolicyProhibited = 2;
        const long NSWindowOut = 0;

        [DllImport(Objc)] static extern IntPtr objc_getClass(string name);
        [DllImport(Objc)] static extern IntPtr sel_registerName(string name);
        [DllImport(Objc)] static extern IntPtr class_getInstanceMethod(IntPtr cls, IntPtr selector);
        [DllImport(Objc)] static extern IntPtr class_replaceMethod(IntPtr cls, IntPtr selector, IntPtr implementation, string types);
        [DllImport(Objc)] static extern IntPtr method_getImplementation(IntPtr method);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr Send(IntPtr target, IntPtr selector);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr SendLong(IntPtr target, IntPtr selector, long value);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr SendDouble(IntPtr target, IntPtr selector, double value);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern IntPtr SendBool(IntPtr target, IntPtr selector, sbyte value);
        [DllImport(Objc, EntryPoint = "objc_msgSend")] static extern ulong SendCount(IntPtr target, IntPtr selector);

        delegate void OrderWindow(IntPtr window, IntPtr selector, long place, long relativeTo);

        static IntPtr s_application, s_windowClass, s_orderSelector, s_originalImplementation;
        static OrderWindow s_original, s_replacement;
        static bool s_hooked;

        static WindowIsolation()
        {
            if (Environment.GetEnvironmentVariable(EnvironmentVariable) != "1")
                return;
            try
            {
                Enable();
            }
            catch (Exception error) when (error is DllNotFoundException || error is EntryPointNotFoundException || error is MarshalDirectiveException)
            {
                Debug.LogWarning("Valtrion: window isolation is not available here (" + error.Message + "); test windows may show on the screen.");
            }
        }

        static void Enable()
        {
            s_application = Send(objc_getClass("NSApplication"), sel_registerName("sharedApplication"));
            Prohibit();

            s_windowClass = objc_getClass("NSWindow");
            s_orderSelector = sel_registerName("orderWindow:relativeTo:");
            s_originalImplementation = method_getImplementation(class_getInstanceMethod(s_windowClass, s_orderSelector));
            s_original = Marshal.GetDelegateForFunctionPointer<OrderWindow>(s_originalImplementation);
            s_replacement = HideThenOrder;
            class_replaceMethod(s_windowClass, s_orderSelector, Marshal.GetFunctionPointerForDelegate(s_replacement), "v@:qq");
            s_hooked = true;

            AssemblyReloadEvents.beforeAssemblyReload += Unhook;
            EditorApplication.quitting += Unhook;
            EditorApplication.update += HideAll;
        }

        static void Unhook()
        {
            EditorApplication.update -= HideAll;
            if (!s_hooked)
                return;
            s_hooked = false;
            class_replaceMethod(s_windowClass, s_orderSelector, s_originalImplementation, "v@:qq");
        }

        static void Prohibit() => SendLong(s_application, sel_registerName("setActivationPolicy:"), NSApplicationActivationPolicyProhibited);

        static void Hide(IntPtr window)
        {
            SendDouble(window, sel_registerName("setAlphaValue:"), 0.0);
            SendBool(window, sel_registerName("setIgnoresMouseEvents:"), 1);
        }

        // Runs for every window that is ordered in front of or behind another; an exception must never reach native code.
        static void HideThenOrder(IntPtr window, IntPtr selector, long place, long relativeTo)
        {
            try
            {
                if (place != NSWindowOut)
                    Hide(window);
            }
            catch (Exception error)
            {
                Debug.LogWarning("Valtrion: window isolation could not hide a window: " + error.Message);
            }
            s_original(window, selector, place, relativeTo);
        }

        // The Editor changes the policy and the transparency of its windows itself now and then; take them back.
        static void HideAll()
        {
            Prohibit();
            IntPtr windows = Send(s_application, sel_registerName("windows"));
            ulong count = SendCount(windows, sel_registerName("count"));
            IntPtr item = sel_registerName("objectAtIndex:");
            for (ulong index = 0; index < count; index++)
                Hide(SendLong(windows, item, (long)index));
        }
    }
}
#endif
