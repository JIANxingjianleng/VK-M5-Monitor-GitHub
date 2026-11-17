# -*- coding: utf-8 -*-
"""把 Tk 窗口改成 per-pixel alpha 的分层窗口（Windows）。

为什么要这个：Tk 的 -transparentcolor 是"色键透明"，只认全透明/不透明，
半透明的抗锯齿像素会和色键混成黑色描边，圆环边缘就会有毛边。要让圆环
真正平滑，必须用 UpdateLayeredWindow 直接提交带 alpha 的 32 位位图。

用法：
    hwnd = toplevel_hwnd(win)      # 传 Tk 的 Toplevel
    改为分层样式
    push(win, w, h, rgba_bytes)    # 每次内容变化时推送新图

注意：一旦用了 UpdateLayeredWindow，该窗口上 Tk 自己画的东西不可见（内容
完全由推送的位图决定），所以文字要放在另一个窗口里。
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                  ctypes.POINTER(ctypes.c_void_p),
                                  wintypes.HANDLE, wintypes.DWORD]

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
BI_RGB = 0


user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL


def toplevel_hwnd(win):
    """Tk Toplevel 的真实顶层 HWND（winfo_id 给的是内层子窗口）"""
    inner = wintypes.HWND(win.winfo_id())
    parent = user32.GetParent(inner)
    return parent or inner


def make_layered(win, click_through=False):
    hwnd = toplevel_hwnd(win)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED
    if click_through:
        style |= WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    return hwnd


def premultiply_bgra(rgba, w, h):
    """RGBA -> 预乘 alpha 的 BGRA（UpdateLayeredWindow 要求）"""
    out = bytearray(len(rgba))
    for i in range(0, w * h * 4, 4):
        r = rgba[i]
        g = rgba[i + 1]
        b = rgba[i + 2]
        a = rgba[i + 3]
        if a == 255:
            out[i] = b
            out[i + 1] = g
            out[i + 2] = r
            out[i + 3] = 255
        elif a:
            out[i] = b * a // 255
            out[i + 1] = g * a // 255
            out[i + 2] = r * a // 255
            out[i + 3] = a
    return bytes(out)


def push(win, w, h, rgba):
    """把 RGBA 位图推到分层窗口上；窗口位置不变"""
    hwnd = toplevel_hwnd(win)
    bgra = premultiply_bgra(rgba, w, h)
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h                      # 负数 = 自上而下
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), BI_RGB,
                                  ctypes.byref(bits), None, 0)
    if not hbmp:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        return False
    ctypes.memmove(bits, bgra, w * h * 4)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    size = SIZE(w, h)
    src = POINT(0, 0)
    blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    ok = user32.UpdateLayeredWindow(hwnd, hdc_screen, None, ctypes.byref(size),
                                    hdc_mem, ctypes.byref(src), 0,
                                    ctypes.byref(blend), ULW_ALPHA)
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    return bool(ok)


def bring_above(win, other):
    """把 win 压到 other 之上：两个窗口都在 topmost 层时同样有效。

    Tk 的 lift(above) 在 overrideredirect + topmost 组合下不一定生效，
    这里直接调 SetWindowPos 改 Z 序。
    """
    hw = wintypes.HWND(toplevel_hwnd(win))
    ho = wintypes.HWND(toplevel_hwnd(other))
    return bool(user32.SetWindowPos(hw, ho, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
