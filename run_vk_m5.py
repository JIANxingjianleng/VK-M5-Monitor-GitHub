# -*- coding: utf-8 -*-
"""VK M5 v13.0 打包入口。

命令行参数语义由 vk_m5.cli.main 统一处理（argparse），
本文件只做 PyInstaller 的根脚本。
"""
import sys


def _probe(tag):
    """托盘注册环境探针（仅 VK_TRAY_TRACE=1 时生效），排查 NIM_ADD 被拒的时间点。"""
    import ctypes
    import ctypes.wintypes as _w
    import os as _os
    import time as _t
    if not _os.environ.get("VK_TRAY_TRACE"):
        return
    try:
        _u = ctypes.windll.user32
        _s = ctypes.windll.shell32
        _k = ctypes.windll.kernel32

        class _NID(ctypes.Structure):
            _fields_ = [("cbSize", _w.DWORD), ("hWnd", _w.HWND), ("uID", _w.UINT),
                        ("uFlags", _w.UINT), ("uCallbackMessage", _w.UINT),
                        ("hIcon", _w.HANDLE), ("szTip", _w.WCHAR * 128),
                        ("dwState", _w.DWORD), ("dwStateMask", _w.DWORD),
                        ("szInfo", _w.WCHAR * 256), ("uVersion", _w.UINT),
                        ("szInfoTitle", _w.WCHAR * 64), ("dwInfoFlags", _w.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", _w.HANDLE)]

        _hw = _u.CreateWindowExW(0, "STATIC", "probe", 0, 0, 0, 1, 1,
                                 None, None, None, None)
        _n = _NID()
        _n.cbSize = ctypes.sizeof(_NID)
        _n.hWnd = _hw
        _n.uID = 9993
        _n.uFlags = 1 | 2 | 4
        _n.uCallbackMessage = 0x0400 + 93
        _n.hIcon = _u.LoadIconW(None, ctypes.c_wchar_p(32512))
        _n.szTip = "probe"
        _rc = _s.Shell_NotifyIconW(0, ctypes.byref(_n))
        _err = _k.GetLastError()
        if _rc:
            _s.Shell_NotifyIconW(2, ctypes.byref(_n))
        _u.DestroyWindow(_hw)
        _base = (_os.path.dirname(_os.path.abspath(sys.executable))
                 if getattr(sys, "frozen", False) else _os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(_base, "tray_trace.txt"), "a", encoding="utf-8") as _f:
            _f.write("%s probe[%s] rc=%s err=%s\n" % (_t.strftime("%H:%M:%S"), tag, _rc, _err))
    except Exception as _e:
        pass


_probe("before-import")

from vk_m5.cli import main

if __name__ == "__main__":
    _probe("before-tk")
    sys.exit(main())
