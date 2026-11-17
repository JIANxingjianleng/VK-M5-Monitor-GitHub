# -*- coding: utf-8 -*-
"""命令行自检：托盘/连接/菜单/档位/采集基线。"""

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser

import tkinter as tk
import tkinter.font as tkfont

from .app import App
from .config import DPI_FAMILIES, DPI_MAX, DPI_MIN, DPI_STEP, HUB_URL
from .device import list_devs, probe_link
from .paths import swallow
from .reader import Reader

def dpidump(seconds=30):
    """诊断：等鼠标被唤醒后，打印 profile 与各命令族/层的原始回包"""
    r = Reader()
    t0 = time.time()
    while time.time() - t0 < seconds:
        st = r.status()
        if st and st.get("online"):
            print("status:", st)
            prof = r.get_profile()
            print("profile:", prof)
            for fam in DPI_FAMILIES:
                for layer in ([prof] if prof is not None else []) + [0, 1]:
                    if layer is None:
                        continue
                    p = r._request_read([fam["get"], layer])
                    head = (" ".join("%02X" % b for b in p[:40])) if p else "None"
                    print("cmd %02X layer %s len %s : %s" %
                          (fam["get"], layer, len(p) if p else 0, head))
            print("dpi_read ->", r.dpi_read(), "family=%s layer=%s" %
                  (hex(r.family["get"]) if r.family else None, r.layer))
            break
        time.sleep(0.2)
    else:
        print("超时：%.0f 秒内鼠标一直休眠（请期间晃动鼠标）" % seconds)
    r.close()
    return 0


def selftest_tray():
    """只读验证：GUID 注册模式 + 托盘图标矩形（hr==0 表示在任务栏可见区）。
    结果写入 exe 同目录 tray_probe.txt（打包后无控制台，靠文件看结果）"""
    import threading as _th
    pre = []
    # App 创建之前：先打印 GUID 选择策略，再用一个与主候选无关的诊断 GUID
    # 探一次 shell 通路（避免干扰真实注册）
    try:
        import ctypes as _c0
        from ctypes import wintypes as _w0
        from . import tray as _v0
        pre.append("guid_order=%s" % (_v0._guid_order(),))
        pre.append("guid_cfg=%r" % (_v0._load_guid_choice(),))
        _g = _v0._parse_guid("11111111-2222-3333-4444-555555555555")
        pre.append("diag_guid=11111111-2222-3333-4444-555555555555")
        _u32 = _c0.WinDLL("user32", use_last_error=True)
        _u32.CreateWindowExW.restype = _w0.HWND
        _u32.CreateWindowExW.argtypes = [
            _w0.DWORD, _w0.LPCWSTR, _w0.LPCWSTR, _w0.DWORD,
            _c0.c_int, _c0.c_int, _c0.c_int, _c0.c_int,
            _w0.HWND, _w0.HMENU, _w0.HINSTANCE, _c0.c_void_p]
        _u32.DestroyWindow.argtypes = [_w0.HWND]
        _hw = _u32.CreateWindowExW(0, "STATIC", "pre", 0, 0, 0, 0, 0,
                                   None, None, None, None)
        _sh = _c0.WinDLL("shell32", use_last_error=True)
        _sh.Shell_NotifyIconW.argtypes = [_w0.DWORD, _c0.POINTER(_v0.NOTIFYICONDATAW)]
        _sh.Shell_NotifyIconW.restype = _w0.BOOL
        _n = _v0.NOTIFYICONDATAW()
        _n.cbSize = _c0.sizeof(_v0.NOTIFYICONDATAW)
        _n.hWnd = _hw
        _n.uID = 9
        _n.guidItem = _g
        _n.uFlags = 0x27
        _n.uCallbackMessage = 0x0520
        _n.hIcon = _u32.LoadIconW(None, _w0.LPCWSTR(32512))
        _n.szTip = "pre"
        for _i in range(3):
            _c0.set_last_error(0)
            _ok = _sh.Shell_NotifyIconW(0, _c0.byref(_n))
            pre.append("pre_add%d=%s err=%s" % (_i, bool(_ok), _c0.get_last_error()))
            if _ok:
                _sh.Shell_NotifyIconW(2, _c0.byref(_n))
                break
            _sh.Shell_NotifyIconW(2, _c0.byref(_n))
            time.sleep(0.3)
        _u32.DestroyWindow(_hw)
    except Exception as _e:
        pre.append("pre_exc=%r" % (_e,))
    app = App()
    lines = []

    def work():
        try:
            t = app.tray
            lines.append("use_guid=%s last_error=%s"
                         % (getattr(t, "_use_guid", None), getattr(t, "last_error", None)))
            # ---- 独立诊断：确认打包后的模块内容与 shell 真实错误码 ----
            import ctypes as _ct
            from ctypes import wintypes as _wt
            from . import tray as _vt
            lines.append("mod=%s" % getattr(_vt, "__file__", "?"))
            lines.append("TRAY_GUID=%s" % getattr(_vt, "TRAY_GUID", "NONE"))
            lines.append("fields=%s" % [f[0] for f in _vt.NOTIFYICONDATAW._fields_])
            lines.append("struct_size=%s" % _ct.sizeof(_vt.NOTIFYICONDATAW))
            sh = _ct.WinDLL("shell32", use_last_error=True)
            sh.Shell_NotifyIconW.argtypes = [_wt.DWORD, _ct.POINTER(_vt.NOTIFYICONDATAW)]
            sh.Shell_NotifyIconW.restype = _wt.BOOL
            nid = _vt.NOTIFYICONDATAW()
            nid.cbSize = _ct.sizeof(_vt.NOTIFYICONDATAW)
            nid.hWnd = t.hwnd
            nid.uID = 2
            nid.guidItem = _vt._parse_guid("11111111-2222-3333-4444-555555555555")
            nid.uFlags = 0x01 | 0x02 | 0x04 | 0x20
            nid.uCallbackMessage = 0x0400 + 0x123
            nid.hIcon = t.hicon
            nid.szTip = "diag"
            _ct.set_last_error(0)
            ok = sh.Shell_NotifyIconW(0, _ct.byref(nid))
            lines.append("diag_guid_add=%s err=%s" % (bool(ok), _ct.get_last_error()))
            time.sleep(0.2)
            _ct.set_last_error(0)
            sh.Shell_NotifyIconW(2, _ct.byref(nid))
            lines.append("diag_cleanup_err=%s" % _ct.get_last_error())
            # 主图标：同 GUID 再手动试一次（排除残留占用）
            nid2 = _vt.NOTIFYICONDATAW()
            nid2.cbSize = _ct.sizeof(_vt.NOTIFYICONDATAW)
            nid2.hWnd = t.hwnd
            nid2.uID = 3
            nid2.guidItem = _vt._parse_guid(t.tray_guid or _vt.TRAY_GUID)
            nid2.uFlags = 0x01 | 0x02 | 0x04 | 0x20
            nid2.uCallbackMessage = 0x0400 + 0x124
            nid2.hIcon = t.hicon
            nid2.szTip = "diag2"
            _ct.set_last_error(0)
            ok2 = sh.Shell_NotifyIconW(0, _ct.byref(nid2))
            lines.append("diag_main_guid_add=%s err=%s" % (bool(ok2), _ct.get_last_error()))
            if ok2:
                time.sleep(0.2)
                sh.Shell_NotifyIconW(2, _ct.byref(nid2))
                lines.append("diag_main_cleanup done")
            gi = getattr(t.nid, "guidItem", None)
            if gi is not None:
                lines.append("tray_guid=%s guidItem=%08X-%04X-%04X" % (getattr(t, "tray_guid", "?"), gi.Data1, gi.Data2, gi.Data3))
            for i in range(5):
                time.sleep(0.4)
                hr, rc = t.icon_rect()
                lines.append("try%d hr=%s rect=(%s,%s,%s,%s)"
                             % (i, hr, rc.left, rc.top, rc.right, rc.bottom))
        except Exception as e:
            lines.append("ERROR %r" % (e,))
        try:
            base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "tray_probe.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(pre + lines) + "\n")
        except Exception as e:
            swallow("selftest.tray_probe", e)
        try:
            app.root.after(100, app.quit)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    _th.Thread(target=work, daemon=True).start()
    app.run()
    return 0


def selftest():
    r = Reader()
    st = r.battery()
    print("battery=%s online=%s type=%s" % (st["battery"], st["online"], st["type"]))
    d = r.dpi_read()
    if d:
        cur, vals = d
        print("dpi family=%s layer=%s current=%s list=%s" %
              (hex(r.family["get"]), r.layer, cur, vals))
    else:
        print("dpi: no response（鼠标休眠或命令不匹配）")
    r.close()
    return 0


def selftest_settest():
    """无副作用验证写入通道：把 DPI 档位写回当前档位，再读回比对"""
    r = Reader()
    d = r.dpi_read()
    if not d:
        print("dpi: no response")
        return 1
    cur, vals = d
    print("read cur=%s vals=%s layer=%s family=%s" % (cur, vals, r.layer, hex(r.family["get"])))
    ok, msg = r.dpi_set(cur)
    print("write(cur) accepted:", ok, msg)
    time.sleep(0.4)
    d2 = r.dpi_read()
    print("reread:", d2)
    r.close()
    return 0


def selftest_set(idx):
    r = Reader()
    print("before:", r.dpi_read())
    ok, msg = r.dpi_set(idx)
    print("set", idx, ok, msg)
    time.sleep(0.3)
    print("after:", r.dpi_read())
    r.close()
    return 0


def selftest_menu():
    """自动验证右键菜单能否弹出

    v13.0 起菜单改为 Tk 自绘（不再是 win32 原生 #32768 菜单），所以检测方式
    也随之改为：点击右键前后对比 TkTopLevel 窗口集合，新增的那个即菜单窗口，
    再校验它是否贴着光标。
    """
    import threading
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    u32.FindWindowW.restype = wintypes.HWND
    u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    result = {"menu": False, "hr": None, "rect": None}
    before = set()

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTunion(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]

    def tk_tops():
        """当前所有 TkTopLevel 窗口 -> {hwnd: rect}"""
        out = {}
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lp):
            try:
                buf = ctypes.create_unicode_buffer(64)
                u32.GetClassNameW(hwnd, buf, 64)
                if buf.value == "TkTopLevel" and u32.IsWindowVisible(hwnd):
                    r = wintypes.RECT()
                    u32.GetWindowRect(hwnd, ctypes.byref(r))
                    out[int(hwnd)] = (r.left, r.top, r.right, r.bottom)
            except Exception as e:
                swallow("selftest.rects", e)
            return True

        u32.EnumWindows(proc(_cb), 0)
        return out

    def click_right():
        inp = (_INPUT * 2)()
        for i, flag in enumerate((0x0008, 0x0010)):      # DOWN, UP
            inp[i].type = 0
            inp[i].u.mi.dwFlags = flag
            inp[i].u.mi.time = 0
        u32.SendInput(2, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    app = App()

    def fire():
        hr, rc = app.tray.icon_rect()
        result["hr"], result["rect"] = hr, (rc.left, rc.top, rc.right, rc.bottom)
        cx, cy = (rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2
        if hr != 0 or cx <= 0 or cy <= 0:
            print("图标不在可见托盘区（可能被折叠到溢出面板）hr=%s rect=%s" %
                  (hr, result["rect"]))
        before.update(tk_tops().keys())
        print("click at %s,%s 已有 Tk 窗口 %s" % (cx, cy, sorted(before)))
        u32.SetCursorPos(cx, cy)
        time.sleep(0.25)
        click_right()

    def _report(hit, native, tag):
        print("MENU", "OK" if (hit or native) else "FAIL",
              "kind=%s hwnd=%s" % (tag, hit[0] if hit else native))
        if hit:
            l, t, r, b = hit[1]
            print("菜单窗口位置 left=%s top=%s right=%s bottom=%s 尺寸 %dx%d"
                  % (l, t, r, b, r - l, b - t))
            pt = wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            print("光标位置 x=%s y=%s" % (pt.x, pt.y))
            far = abs(l - pt.x) > 400 or abs(t - pt.y) > 400
            print("坐标校验", "FAIL(菜单不在光标附近)" if far else "OK")

    def check():
        now = tk_tops()
        new = {k: v for k, v in now.items() if k not in before}
        # 兼容：万一走的是原生菜单，也认
        native = u32.FindWindowW("#32768", None)
        hit = None
        for k, v in new.items():
            w, h = v[2] - v[0], v[3] - v[1]
            if 20 <= w <= 900 and 10 <= h <= 900:      # 菜单不可能比这更大
                hit = (k, v)
                break
        if hit or native:
            result["menu"] = True
            _report(hit, native, "tk-popup" if hit else "native")
            _finish()
            return
        # SendInput 在打包/非前台场景下可能被系统忽略，回退到进程内直接触发，
        # 这样仍能验证「菜单窗口能建出来且贴光标」这条核心链路。
        print("真实右键未唤起菜单（SendInput 可能被忽略），回退进程内触发")
        result["menu"] = False
        try:
            app.root.after(0, lambda: app._show_menu(2800, 1200))
        except Exception as ex:
            print("回退触发失败 %r" % (ex,))
            _finish()
            return
        try:
            app.root.after(1500, check2)
        except Exception:
            _finish()

    def check2():
        now = tk_tops()
        new = {k: v for k, v in now.items() if k not in before}
        native = u32.FindWindowW("#32768", None)
        hit = None
        for k, v in new.items():
            w, h = v[2] - v[0], v[3] - v[1]
            if 20 <= w <= 900 and 10 <= h <= 900:
                hit = (k, v)
                break
        result["menu"] = bool(hit or native)
        _report(hit, native, "tk-popup-direct" if hit else "-")
        _finish()

    def _finish():
        try:
            app.root.after(400, app.quit)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    threading.Thread(target=lambda: (time.sleep(1.6), fire()), daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(3.0), check()), daemon=True).start()
    app.run()
    return 0 if result["menu"] else 1


def selftest_link():
    """诊断三模识别：打印设备清单 + probe_link + read_all"""
    print("HUB_URL:", HUB_URL)
    print("DPI 规则: %d~%d 步进 %d" % (DPI_MIN, DPI_MAX, DPI_STEP))
    for d in list_devs():
        print("dev pid=%04X usage_page=%04X bt=%s product=%r" % (
            d["pid"], d.get("usage_page") or 0, d["bt"], d.get("product", "")))
    print("probe_link ->", probe_link())
    r = Reader()
    got = r.read_all()
    print("read_all(首次) ->", got)
    if not got["online"]:
        # 蓝牙 GATT 首次建连约 10s：读_all 不等，这里替它等一等再看结果
        print("等待后台蓝牙读数…")
        for _ in range(14):
            time.sleep(1.0)
            if r._ble_pct is not None:
                break
        print("read_all(等蓝牙后) ->", r.read_all())
    r.close()
    return 0
