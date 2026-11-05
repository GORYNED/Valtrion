"""A Valtrion icon in the Windows notification area (the tray), with ctypes and no other package.

Click: open the dashboard. Right click: a menu with "Open dashboard", "Start with Windows" (a per-user entry in the Run key, off until the user
ticks it) and "Quit". Windows only; `Tray.run` blocks in the message loop of the calling thread until Quit."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import brandassets, remote

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Valtrion Dashboard"

WM_DESTROY, WM_CLOSE, WM_NULL, WM_LBUTTONUP, WM_RBUTTONUP, WM_COMMAND, WM_APP = 0x0002, 0x0010, 0x0000, 0x0202, 0x0205, 0x0111, 0x8000
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0, 8, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_BOTTOMALIGN = 0x0002, 0x0100, 0x0020
CALLBACK_MESSAGE = WM_APP + 1
ID_OPEN, ID_AUTOSTART, ID_QUIT = 1, 2, 3


def autostart_command() -> list[str]:
    """The command that starts the dashboard without a console window: `pythonw` of the interpreter that runs this CLI."""
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return [str(windowless if windowless.is_file() else python), "-I", "-c", remote.LAUNCHER, str(remote.CLI_ROOT), "dashboard", "--foreground"]


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(autostart_command()))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


class Tray:
    def __init__(self, tooltip: str, icon_file: Path, on_open: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self.tooltip = tooltip[:120]
        self.icon_file = icon_file
        self.on_open = on_open
        self.on_quit = on_quit
        self._hwnd = None

    def stop(self) -> None:
        """Ask the loop to end; callable from another thread."""
        if self._hwnd:
            import ctypes

            ctypes.WinDLL("user32").PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes

        if os.name != "nt":
            raise OSError("the tray icon exists on Windows only")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]

        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        instance = kernel32.GetModuleHandleW(None)
        small = user32.GetSystemMetrics(49)  # SM_CXSMICON
        icon = user32.LoadImageW(None, str(self.icon_file), 1, small, small, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)

        def notify(action: int) -> None:
            data.hWnd, data.uID, data.uFlags = self._hwnd, 1, NIF_MESSAGE | NIF_ICON | NIF_TIP
            data.uCallbackMessage, data.hIcon, data.szTip = CALLBACK_MESSAGE, icon, self.tooltip
            shell32.Shell_NotifyIconW(action, ctypes.byref(data))

        def show_menu(hwnd) -> None:
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open dashboard")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if autostart_enabled() else 0), ID_AUTOSTART, "Start with Windows")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)  # without it the menu does not close when the user clicks elsewhere
            choice = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN, point.x, point.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            user32.DestroyMenu(menu)
            if choice == ID_OPEN:
                self.on_open()
            elif choice == ID_AUTOSTART:
                try:
                    set_autostart(not autostart_enabled())
                except OSError:
                    pass
            elif choice == ID_QUIT:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        def window_proc(hwnd, message, wparam, lparam):
            if message == CALLBACK_MESSAGE:
                if lparam == WM_LBUTTONUP:
                    self.on_open()
                elif lparam == WM_RBUTTONUP:
                    show_menu(hwnd)
                return 0
            if message == taskbar_created:  # Explorer restarted: its notification area is empty again
                notify(NIM_ADD)
                return 0
            if message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                notify(NIM_DELETE)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        procedure = WNDPROC(window_proc)  # kept alive for as long as the window exists
        window_class = WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
        window_class.lpfnWndProc = procedure
        window_class.hInstance = instance
        window_class.lpszClassName = f"ValtrionTray{os.getpid()}"
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            raise OSError(ctypes.get_last_error(), "RegisterClassExW failed")
        self._hwnd = user32.CreateWindowExW(0, window_class.lpszClassName, "Valtrion", 0, 0, 0, 0, 0, None, None, instance, None)
        if not self._hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        notify(NIM_ADD)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._hwnd = None
        self.on_quit()


def write_icon(home: Path) -> Path:
    """The icon file the tray loads (written once; the brand asset is in the package)."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "dashboard-icon.ico"
    data = brandassets.icon_ico()
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)
    return path
