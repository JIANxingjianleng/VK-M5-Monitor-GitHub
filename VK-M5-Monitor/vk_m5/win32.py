# -*- coding: utf-8 -*-
"""Win32 ctypes 声明与结构体（全部集中，不再散落业务文件）。"""

import ctypes
import os
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, byref, sizeof, c_int, c_void_p
from ctypes.wintypes import (DWORD, HICON, HINSTANCE, HMENU, HWND, LPCWSTR,
                             LPVOID, MSG, POINT, RECT, UINT, WCHAR, WPARAM,
                             LPARAM, BOOL)

from .paths import swallow

HBRUSH = HCURSOR = HDC = HGDIOBJ = c_void_p

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_RBUTTONDOWN = 0x0204
WM_CONTEXTMENU = 0x007B
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_GUID = 0x20
NOTIFYICON_VERSION_4 = 4

# v12.3 固定托盘图标 GUID：让 Windows 按 GUID 而不是 exe 完整路径识别本图标。
# 之前不带 GUID 时，图标身份跟随 exe 路径（NotifyIconSettings\<路径哈希>），
# 每换一个目录/版本/副本启动都是一个"新图标"，默认被折叠进溢出区，
# 手动拖出来后换路径再启动又会隐藏。锁定 GUID 后显示状态跨路径保持一致。
#
# 注意：GUID 一旦被某个可执行文件注册过，会被 Explorer 记进 NotifyIconSettings，
# 之后再从别的路径用同一 GUID 注册，Shell_NotifyIcon 会回 E_FAIL。所以这里准备
# 多个候选，按序尝试，并把最终可用的那个记到 %APPDATA%\VK_M5\tray_guid.txt，
# 保证本机每次启动都用同一个 GUID（否则又会变成"每次都是新图标"）。
TRAY_GUID_CANDIDATES = (
    "2F7B4D91-8C63-4A57-B1E2-9D3A6C5F8470",
    "6E1A9C34-5D82-4B7F-9A31-C7E4B2D80615",
    "9C5D2E71-4A38-4F96-8B12-D3E7A5C96428",
)
TRAY_GUID = TRAY_GUID_CANDIDATES[0]

DIB_RGB_COLORS = 0
BI_RGB = 0
# 菜单关闭后的强制重绘标志
RDW_INVALIDATE = 0x0001
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080
RDW_NOERASE = 0x0020
TRANSPARENT = 1
DT_CENTER = 0x01
DT_VCENTER = 0x04
DT_SINGLELINE = 0x20
DT_NOPREFIX = 0x0800
FW_BOLD = 700
CLEARTYPE_QUALITY = 5
DEFAULT_CHARSET = 1
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
DEFAULT_PITCH = 0

SM_CXSMICON = 49
SIZE = 32

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
SW_SHOWNA = 8


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _parse_guid(text):
    """把 'XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX' 解析为 GUID 结构"""
    g = GUID()
    if not text:
        return g
    parts = text.strip().strip("{}").split("-")
    if len(parts) != 5:
        return g
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    tail = bytes.fromhex(parts[3] + parts[4])
    for i in range(8):
        g.Data4[i] = tail[i] if i < len(tail) else 0
    return g


TRAY_GUID_ITEM = _parse_guid(TRAY_GUID)


def _guid_cfg_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VK_M5", "tray_guid.txt")


def _load_guid_choice():
    """读取本机上次注册成功的 GUID（保证每次启动身份一致）"""
    try:
        with open(_guid_cfg_path(), "r", encoding="utf-8") as f:
            g = f.read().strip()
        if g in TRAY_GUID_CANDIDATES:
            return g
    except Exception as e:
        swallow("win32.guid.load", e)
    return ""


def _save_guid_choice(g):
    try:
        p = _guid_cfg_path()
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(g)
    except Exception as e:
        swallow("win32.guid.save", e)


def _guid_order():
    """优先复用记录过的 GUID，其余候选按序补全"""
    pref = _load_guid_choice()
    order = [pref] if pref else []
    for g in TRAY_GUID_CANDIDATES:
        if g not in order:
            order.append(g)
    return order


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT),
                ("uFlags", UINT), ("uCallbackMessage", UINT), ("hIcon", HICON),
                ("szTip", WCHAR * 128), ("dwState", DWORD), ("dwStateMask", DWORD),
                ("szInfo", WCHAR * 256), ("uVersion", UINT),
                ("szInfoTitle", WCHAR * 64), ("dwInfoFlags", DWORD),
                ("guidItem", GUID), ("hBalloonIcon", HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", DWORD),
                ("biClrImportant", DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", BOOL), ("xHotspot", DWORD), ("yHotspot", DWORD),
                ("hbmMask", c_void_p), ("hbmColor", c_void_p)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", UINT), ("lpfnWndProc", c_void_p),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", HINSTANCE), ("hIcon", HICON),
                ("hCursor", HCURSOR), ("hbrBackground", HBRUSH),
                ("lpszMenuName", LPCWSTR), ("lpszClassName", LPCWSTR)]


WNDPROC = WINFUNCTYPE(LPARAM, HWND, UINT, WPARAM, LPARAM)

user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LPARAM
user32.CreateWindowExW.restype = HWND
user32.CreateWindowExW.argtypes = [DWORD, LPCWSTR, LPCWSTR, DWORD, c_int, c_int,
                                   c_int, c_int, HWND, HMENU, HINSTANCE, LPVOID]
user32.RegisterClassW.argtypes = [POINTER(WNDCLASSW)]
shell32.Shell_NotifyIconW.argtypes = [DWORD, POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = BOOL
user32.CreateIconIndirect.argtypes = [POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = HICON
user32.DestroyIcon.argtypes = [HICON]
user32.GetCursorPos.argtypes = [POINTER(POINT)]
user32.PeekMessageW.argtypes = [POINTER(MSG), HWND, UINT, UINT, UINT]
user32.TranslateMessage.argtypes = [POINTER(MSG)]
user32.DispatchMessageW.argtypes = [POINTER(MSG)]
user32.DispatchMessageW.restype = LPARAM
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]
kernel32.GetModuleHandleW.restype = c_void_p
kernel32.GetModuleHandleW.argtypes = [LPCWSTR]

gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateDIBSection.restype = c_void_p
gdi32.CreateDIBSection.argtypes = [HDC, POINTER(BITMAPINFO), UINT,
                                   POINTER(c_void_p), c_void_p, DWORD]
gdi32.CreateBitmap.restype = c_void_p
gdi32.CreateBitmap.argtypes = [c_int, c_int, UINT, UINT, c_void_p]
gdi32.CreateFontW.restype = c_void_p
gdi32.CreateFontW.argtypes = [c_int, c_int, c_int, c_int, c_int, DWORD, DWORD,
                              DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPCWSTR]
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SetBkMode.argtypes = [HDC, c_int]
gdi32.SetTextColor.argtypes = [HDC, DWORD]
user32.DrawTextW.argtypes = [HDC, LPCWSTR, c_int, POINTER(RECT), UINT]
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]
gdi32.CreateRoundRectRgn.restype = c_void_p
gdi32.CreateRoundRectRgn.argtypes = [c_int, c_int, c_int, c_int, c_int, c_int]
gdi32.CreateSolidBrush.restype = c_void_p
gdi32.CreateSolidBrush.argtypes = [DWORD]
gdi32.FillRgn.argtypes = [HDC, c_void_p, c_void_p]

PM_REMOVE = 0x0001


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT),
                ("guidItem", GUID)]


shell32.Shell_NotifyIconGetRect.argtypes = [POINTER(NOTIFYICONIDENTIFIER),
                                            POINTER(RECT)]
shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long


ANTIALIASED_QUALITY = 4
DT_RIGHT = 0x0002
DEFAULT_FG = 0x00FFFFFF     # COLORREF 白
DARK_FG = 0x00000000        # COLORREF 黑

user32.GetAncestor.argtypes = [HWND, UINT]
user32.GetAncestor.restype = HWND
user32.GetForegroundWindow.restype = HWND
user32.ShowWindow.argtypes = [HWND, c_int]
user32.ShowWindow.restype = BOOL
user32.SetWindowPos.argtypes = [HWND, HWND, c_int, c_int, c_int, c_int, UINT]
user32.SetWindowPos.restype = BOOL

# ---------------- 全局滚轮监听（悬浮窗未激活时也能滚轮翻页） ----------------
WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
WM_APP_STOP = 0x8000 + 1     # 自定义：让钩子线程自己摘钩子后退出


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", DWORD), ("flags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ctypes.c_size_t)]


# 注意：MSG 必须用 ctypes.wintypes.MSG（文件顶部已导入）。
# 曾在此自定义同名 MSG 结构，导致 PeekMessageW 的 argtypes（POINTER(wintypes.MSG)）
# 与实际传入的实例类型不匹配，poll_events 每次调用都抛 ArgumentError，
# 托盘右键/左键事件全部失效 —— 不要再自定义 MSG。


HOOKPROC = WINFUNCTYPE(c_int, c_int, WPARAM, LPARAM)

user32.SetWindowsHookExW.argtypes = [c_int, HOOKPROC, HINSTANCE, DWORD]
user32.SetWindowsHookExW.restype = c_void_p
user32.CallNextHookEx.argtypes = [c_void_p, c_int, WPARAM, LPARAM]
user32.CallNextHookEx.restype = LPARAM
user32.UnhookWindowsHookEx.argtypes = [c_void_p]
user32.UnhookWindowsHookEx.restype = BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT]
user32.GetMessageW.restype = c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LPARAM
user32.PostThreadMessageW.argtypes = [DWORD, UINT, WPARAM, LPARAM]
kernel32.GetCurrentThreadId.restype = DWORD


# 菜单期间用的「点别处即关」钩子
WM_MOUSE_DOWNS = (0x0201, 0x0204, 0x0207, 0x020B,   # L/R/M/X 键按下
                  0x00A1, 0x00A4, 0x00A7)            # 非客户区按下（标题栏等）
