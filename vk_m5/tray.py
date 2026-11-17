# -*- coding: utf-8 -*-
"""托盘图标：GUID 身份、图标绘制、Shell_NotifyIcon 事件泵。"""

import ctypes
import os
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, byref, sizeof, c_int, c_void_p
from ctypes.wintypes import (DWORD, HICON, HINSTANCE, HMENU, HWND, LPCWSTR,
                             LPVOID, MSG, POINT, RECT, UINT, WCHAR, WPARAM,
                             LPARAM, BOOL)

from .paths import swallow
from .win32 import ANTIALIASED_QUALITY, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CLIP_DEFAULT_PRECIS, DEFAULT_CHARSET, DEFAULT_PITCH, DIB_RGB_COLORS, DT_CENTER, DT_NOPREFIX, DT_RIGHT, DT_SINGLELINE, DT_VCENTER, FW_BOLD, GUID, ICONINFO, NIF_GUID, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NIM_MODIFY, NIM_SETVERSION, NIN_KEYSELECT, NIN_SELECT, NOTIFYICONDATAW, NOTIFYICONIDENTIFIER, NOTIFYICON_VERSION_4, OUT_DEFAULT_PRECIS, PM_REMOVE, SM_CXSMICON, TRANSPARENT, TRAY_GUID, TRAY_GUID_CANDIDATES, WM_CONTEXTMENU, WM_LBUTTONDBLCLK, WM_LBUTTONUP, WM_RBUTTONUP, WM_TRAY, WNDCLASSW, WNDPROC, WS_EX_TOOLWINDOW, WS_POPUP, _guid_cfg_path, _guid_order, _load_guid_choice, _parse_guid, _save_guid_choice, gdi32, kernel32, shell32, user32

TRAY_GUID_ITEM = _parse_guid(TRAY_GUID)


def _trace(msg):
    """托盘诊断日志：只在 VK_TRAY_TRACE=1 时写 tray_trace.txt

    打包版（windowed）没有可见报错，托盘初始化在哪一步断掉只能靠这个文件。
    """
    if not os.environ.get("VK_TRAY_TRACE"):
        return
    try:
        import sys as _sys
        base = (os.path.dirname(os.path.abspath(_sys.executable))
                if getattr(_sys, "frozen", False) else
                os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "tray_trace.txt"), "a",
                  encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _draw_text(hdc, text, height, colorref, rect, align):
    """在 DIB 上画一行文字（抗锯齿灰阶，不写 alpha）"""
    font = gdi32.CreateFontW(-int(height), 0, 0, 0, FW_BOLD, 0, 0, 0,
                             DEFAULT_CHARSET, OUT_DEFAULT_PRECIS,
                             CLIP_DEFAULT_PRECIS, ANTIALIASED_QUALITY,
                             DEFAULT_PITCH, "Segoe UI")
    old = gdi32.SelectObject(hdc, font)
    gdi32.SetBkMode(hdc, TRANSPARENT)
    gdi32.SetTextColor(hdc, colorref & 0xFFFFFF)     # COLORREF 0x00BBGGRR
    rc = RECT(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    user32.DrawTextW(hdc, text, -1, byref(rc),
                     DT_SINGLELINE | DT_NOPREFIX | DT_VCENTER | align)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(font)


def _icon_size():
    """按系统通知区小图标实际像素绘制，避免系统缩放导致糊字"""
    try:
        s = int(user32.GetSystemMetrics(SM_CXSMICON) or 0)
    except Exception:
        s = 0
    if s < 16:
        s = 16
    if s > 64:
        s = 64
    return s


_C_GREEN = 0x007AD133   # COLORREF BBGGRR
_C_YELLOW = 0x004DD2E8
_C_RED = 0x005252E0
_C_BLUE = 0x00FFA34A
_C_AMBER = 0x004DD2E8
_C_WHITE = 0x00F5F2F2
_C_DIM = 0x0099998D
_C_TRACK = 0x003F3434

_MODE_COLOR = {"2.4g": _C_GREEN, "bt": _C_BLUE, "wired": _C_AMBER}
_BOLT = ((0.62, 0.00), (0.30, 0.54), (0.47, 0.54),
         (0.40, 1.00), (0.72, 0.46), (0.55, 0.46))

try:
    gdi32.Polygon.argtypes = [c_void_p, POINTER(POINT), c_int]
    gdi32.Polygon.restype = c_int
    gdi32.Rectangle.argtypes = [c_void_p, c_int, c_int, c_int, c_int]
    gdi32.Rectangle.restype = c_int
    gdi32.GetStockObject.restype = c_void_p
    gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
    gdi32.SelectObject.restype = c_void_p
except Exception:
    pass


def _fill_rect(hdc, x0, y0, x1, y1, cref):
    brush = gdi32.CreateSolidBrush(cref)
    old_b = gdi32.SelectObject(hdc, brush)
    old_p = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))  # NULL_PEN
    gdi32.Rectangle(hdc, int(x0), int(y0), int(x1), int(y1))
    gdi32.SelectObject(hdc, old_p)
    gdi32.SelectObject(hdc, old_b)
    gdi32.DeleteObject(brush)


def _fill_poly(hdc, coords, cref):
    if not coords:
        return
    brush = gdi32.CreateSolidBrush(cref)
    old_b = gdi32.SelectObject(hdc, brush)
    old_p = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))
    arr = (POINT * len(coords))(*[POINT(int(x), int(y)) for x, y in coords])
    gdi32.Polygon(hdc, arr, len(coords))
    gdi32.SelectObject(hdc, old_p)
    gdi32.SelectObject(hdc, old_b)
    gdi32.DeleteObject(brush)


def _digit_color(p):
    if p is None:
        return _C_DIM
    if p <= 15:
        return _C_RED
    if p <= 30:
        return _C_YELLOW
    return _C_WHITE


def _bar_color(p):
    if p is None:
        return _C_TRACK
    if p <= 15:
        return _C_RED
    if p <= 30:
        return _C_YELLOW
    return _C_GREEN


def _bolt_coords(size):
    x0, y0 = size * 0.27, size * 0.24
    x1, y1 = size * 0.73, size * 0.74
    return [(x0 + px * (x1 - x0), y0 + py * (y1 - y0)) for px, py in _BOLT]


def _make_icon(pct=None, mode=None, charging=False, save_path=None):
    """任务栏图标（A4）：顶部状态条 + 大数字 + 底部电量条 -> HICON

    v22：去掉 VK —— 16px 下把面积全给电量数字。
      · 顶部 2px：2.4G 绿 / 蓝牙蓝 / 有线（充电）琥珀；
      · 底部 2px：电量条，<=15% 红、<=30% 黄、其余绿；
      · 中间大数字：<=30% 黄、<=15% 红、其余白；
      · 充电：绿色闪电 + 满格绿条 + 琥珀顶条。
    """
    disp = _icon_size()
    ss = 2 if disp <= 24 else 1
    size = disp * ss
    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size          # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits = c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, byref(bmi), DIB_RGB_COLORS,
                                  byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, hbmp)

    # 1) 深色圆角徽章底（RGB(0x1c,0x1c,0x22)）
    rad = max(2, int(round(size * 0.22)))
    try:
        rgn = gdi32.CreateRoundRectRgn(0, 0, size + 1, size + 1, rad, rad)
        brush = gdi32.CreateSolidBrush(0x00221C1C)      # COLORREF 0x00BBGGRR
        gdi32.FillRgn(hdc, rgn, brush)
        gdi32.DeleteObject(brush)
        gdi32.DeleteObject(rgn)
    except Exception:
        pass  # 可忽略：幂等清理/UI 收尾

    # 2) 顶部状态条：2.4G 绿 / 蓝牙蓝 / 有线琥珀（充电也是琥珀）
    strip = _MODE_COLOR.get(mode or "")
    if strip is None and charging:
        strip = _C_AMBER
    if strip is not None:
        _fill_rect(hdc, size * 0.10, size * 0.06,
                   size * 0.90, size * 0.17, strip)

    # 3) 底部电量条
    if pct is not None or charging:
        bx0, bx1 = size * 0.12, size * 0.88
        by0, by1 = size * 0.80, size * 0.93
        _fill_rect(hdc, bx0, by0, bx1, by1, _C_TRACK)
        if charging:
            _fill_rect(hdc, bx0, by0, bx1, by1, _C_GREEN)
        elif pct and pct > 0:
            _fill_rect(hdc, bx0, by0,
                       bx0 + (bx1 - bx0) * min(100, pct) / 100.0,
                       by1, _bar_color(pct))

    # 4) 中间：充电画闪电，否则画大数字
    if charging:
        _fill_poly(hdc, _bolt_coords(size), _C_GREEN)
    else:
        txt = str(pct) if pct is not None else "--"
        h = size * (0.62 if len(txt) <= 2 else 0.46)
        _draw_text(hdc, txt, h, _digit_color(pct),
                   (0, size * 0.18, size, size * 0.78), DT_CENTER)

    # 4) 圆角外置透明，徽章内全不透明（避免缩放出黑边/灰边）
    raw = ctypes.string_at(bits, size * size * 4)
    if save_path:
        try:
            from PIL import Image
            im = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
            im.convert("RGB").resize((size * 6, size * 6), Image.NEAREST).save(save_path)
        except Exception as e:
            swallow("tray.icon_dump", e)
    buf = bytearray(raw)
    for y in range(size):
        for x in range(size):
            o = (y * size + x) * 4
            buf[o + 3] = 255 if _in_round_rect(x, y, size, rad) else 0
    ctypes.memmove(bits, bytes(buf), len(buf))

    hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    ii = ICONINFO(True, 0, 0, hbm_mask, hbmp)
    hicon = user32.CreateIconIndirect(byref(ii))

    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteObject(hbm_mask)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, hdc_screen)
    return hicon


def _in_round_rect(x, y, size, rad):
    """判断像素是否在圆角矩形内（用于生成 alpha）"""
    r = rad / 2.0
    cx = r if x < r else (size - r if x > size - r else None)
    cy = r if y < r else (size - r if y > size - r else None)
    if cx is None or cy is None:
        return True
    dx, dy = x + 0.5 - cx, y + 0.5 - cy
    return dx * dx + dy * dy <= r * r

class Tray:
    """任务栏通知区域图标"""

    _instance = None
    _wndproc_ref = None

    def __init__(self, tip="VK M5", class_name="VKTrayWnd"):
        _trace("init-start tip=%r" % (tip,))
        # 诊断：先用最朴素的 STATIC 窗口探一次注册，区分"环境不让注册"和"自己的窗口有问题"
        if os.environ.get("VK_TRAY_TRACE"):
            try:
                _ph = user32.CreateWindowExW(0, "STATIC", "vkprobe", 0, 0, 0, 1, 1,
                                             None, None, None, None)
                _pn = NOTIFYICONDATAW()
                _pn.cbSize = sizeof(NOTIFYICONDATAW)
                _pn.hWnd = _ph
                _pn.uID = 9996
                _pn.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
                _pn.uCallbackMessage = 0x0400 + 96
                _pn.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))
                _pn.szTip = "vkprobe"
                _prc = shell32.Shell_NotifyIconW(NIM_ADD, byref(_pn))
                _trace("probe add rc=%s lastError=%s hwnd=%s" % (_prc, kernel32.GetLastError(), _ph))
                if _prc:
                    shell32.Shell_NotifyIconW(NIM_DELETE, byref(_pn))
                user32.DestroyWindow(_ph)
            except Exception as _e:
                _trace("probe failed: %r" % (_e,))
        self.events = []
        self.hicon = None
        self._wndproc_ref = WNDPROC(self._wndproc)

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = class_name
        user32.RegisterClassW(byref(wc))

        # 隐藏顶层 popup 窗口（1x1，不激活、不显示），可作弹出菜单 owner
        self.hwnd = user32.CreateWindowExW(WS_EX_TOOLWINDOW, class_name, tip,
                                           WS_POPUP, 0, 0, 1, 1,
                                           None, None, wc.hInstance, None)
        if not self.hwnd:
            raise RuntimeError("创建托盘消息窗口失败")
        self.wnd = self.hwnd
        _trace("window ok hwnd=%s" % (self.hwnd,))

        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = sizeof(NOTIFYICONDATAW)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uCallbackMessage = WM_TRAY
        self.hicon = _make_icon(None, None, False)
        _trace("icon built hicon=%s" % (self.hicon,))
        self.nid.hIcon = self.hicon
        self.nid.szTip = tip[:127]
        self._prev_icon = None
        # v12.3：优先按固定 GUID 注册，图标身份不再跟随 exe 路径
        self._use_guid = True
        self.last_error = 0
        self.tray_guid = ""
        self.guid_item = GUID()
        self._base_flags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        ok = False
        for _g in _guid_order():
            self.nid.guidItem = _parse_guid(_g)
            self.nid.uFlags = self._base_flags | NIF_GUID
            # 上一份被任务管理器/强杀结束时，explorer 里会留下同 GUID 的僵尸记录，
            # 这时 NIM_ADD 会直接失败（图标就是不出现）。所以先删同 GUID 再添加。
            _trace("try guid=%s" % _g)
            shell32.Shell_NotifyIconW(NIM_DELETE, byref(self.nid))
            _trace("deleted, adding")
            _rc = shell32.Shell_NotifyIconW(NIM_ADD, byref(self.nid))
            _trace("add rc=%s" % _rc)
            if _rc:
                self.tray_guid = _g
                self.guid_item = _parse_guid(_g)
                _save_guid_choice(_g)
                ok = True
                break
        if ok:
            # V4 协议：Win11 上单击/右键语义与系统一致
            self.nid.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, byref(self.nid))
        else:
            # 兜底：所有候选 GUID 都被占用时，退回旧的 uID 方式
            self._use_guid = False
            self.nid.guidItem = GUID()
            self.nid.uFlags = self._base_flags
            shell32.Shell_NotifyIconW(NIM_DELETE, byref(self.nid))
            _rc = shell32.Shell_NotifyIconW(NIM_ADD, byref(self.nid))
            _trace("fallback add rc=%s lastError=%s" % (_rc, kernel32.GetLastError()))
            if not _rc:
                # explorer 刚重启时偶发不接受注册，隔一下再试一次
                time.sleep(0.6)
                _rc = shell32.Shell_NotifyIconW(NIM_ADD, byref(self.nid))
                _trace("fallback retry rc=%s lastError=%s" % (_rc, kernel32.GetLastError()))
                if not _rc:
                    _h = user32.LoadIconW(None, ctypes.c_wchar_p(32512))
                    self.nid.hIcon = _h
                    _rc2 = shell32.Shell_NotifyIconW(NIM_ADD, byref(self.nid))
                    _ws = user32.GetProcessWindowStation()
                    _buf = ctypes.create_unicode_buffer(128)
                    _need = DWORD()
                    ctypes.windll.user32.GetUserObjectInformationW(_ws, 2, _buf, 256, byref(_need))
                    _trace("std-icon probe rc=%s winsta=%r" % (_rc2, _buf.value))
                    raise RuntimeError("添加托盘图标失败")

        # 诊断开关：设置环境变量 VK_TRAY_TRACE=1 时才记录托盘注册情况
        _trace("registered pid=%s uid=%s use_guid=%s guid=%s hwnd=%s" % (
            os.getpid(), self.nid.uID, self._use_guid,
            self.tray_guid or "-", self.hwnd))


    # ---------- 回调 ----------
    def _push(self, kind, x=None, y=None):
        """事件入队（右键做 0.45s 去重：Win11 可能连发 DOWN/UP/CONTEXTMENU）"""
        now = time.time()
        if kind == "right":
            if now - getattr(self, "_last_right", 0.0) < 0.45:
                return
            self._last_right = now
            # 关键：各版本 Windows 的 lParam 语义不一致（可能是 uID、可能是坐标），
            # 直接解析会把菜单弹到 (1,517) 这种角落，看起来就是"右键没反应"。
            # 统一改用当前光标屏幕坐标。
            try:
                cx, cy = self.cursor_pos()
            except Exception:
                cx = cy = None
            if cx is None or cy is None or cx < 0 or cy < 0:
                x = y = None
            else:
                x, y = cx, cy
        self.events.append((kind, x, y))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = lparam & 0xFFFF
            if ev == WM_CONTEXTMENU:
                self._push("right")
            elif ev == WM_RBUTTONUP:
                self._push("right")
            elif ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT, NIN_KEYSELECT):
                self._push("left")
            return 0
        return user32.DefWindowProcW(HWND(hwnd), msg, wparam, lparam)

    # ---------- 对外 ----------
    def set_icon(self, pct=None, mode=None, charging=False):
        try:
            new = _make_icon(pct, mode, charging)
        except Exception:
            return
        old = self._prev_icon
        self._prev_icon = self.hicon
        self.hicon = new
        self.nid.hIcon = new
        self.nid.uFlags = NIF_ICON | (NIF_GUID if self._use_guid else 0)
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(self.nid))
        self.nid.uFlags = self._base_flags
        # 延后一帧销毁上个句柄：explorer 异步渲染，立刻 DestroyIcon 会让
        # 托盘图标出现短暂空白（看起来像图标消失）
        if old:
            user32.DestroyIcon(old)

    def set_tip(self, tip):
        self.nid.szTip = tip[:127]
        self.nid.uFlags = NIF_TIP | (NIF_GUID if self._use_guid else 0)
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(self.nid))
        self.nid.uFlags = self._base_flags

    def poll_events(self):
        """只泵托盘窗口自己的消息，避免和 tkinter 的消息循环抢消息"""
        msg = MSG()
        while True:
            # PM_REMOVE=1：只取发往 tray 窗口的消息
            if not user32.PeekMessageW(byref(msg), self.hwnd, 0, 0, PM_REMOVE):
                break
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))
        ev, self.events = self.events, []
        return ev

    def cursor_pos(self):
        p = POINT()
        user32.GetCursorPos(byref(p))
        return p.x, p.y


    def icon_rect(self):
        """查询托盘图标矩形，返回 (hr, rect)；hr==0 表示图标在任务栏可见区"""
        nid_id = NOTIFYICONIDENTIFIER()
        nid_id.cbSize = sizeof(NOTIFYICONIDENTIFIER)
        nid_id.hWnd = self.hwnd
        nid_id.uID = 1
        if self._use_guid:
            nid_id.guidItem = self.guid_item
        rc = RECT()
        hr = shell32.Shell_NotifyIconGetRect(byref(nid_id), byref(rc))
        return hr, rc

    def destroy(self):
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, byref(self.nid))
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        for h in (self.hicon, getattr(self, "_prev_icon", None)):
            if h:
                user32.DestroyIcon(h)
        self.hicon = None
        self._prev_icon = None
