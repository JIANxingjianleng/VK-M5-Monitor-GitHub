# -*- coding: utf-8 -*-
"""全局钩子：滚轮钩子与点击外部关闭钩子（独立线程）。"""

import ctypes
import os
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, byref, sizeof, c_int, c_void_p
from ctypes.wintypes import (DWORD, HICON, HINSTANCE, HMENU, HWND, LPCWSTR,
                             LPVOID, MSG, POINT, RECT, UINT, WCHAR, WPARAM,
                             LPARAM, BOOL)

from .paths import swallow
from .win32 import HOOKPROC, MSLLHOOKSTRUCT, WH_MOUSE_LL, WM_APP_STOP, WM_MOUSEWHEEL, WM_MOUSE_DOWNS, kernel32, user32

def _hook_thread_main(hook):
    """低级鼠标钩子的线程体（ClickAwayHook / WheelHook 共用同一套模型）。

    装钩子、跑消息循环、退出前摘钩子。关键：UnhookWindowsHookEx 必须由安装
    钩子的线程调用，否则静默失败，旧钩子会一直活着——重挂之后两三条钩子同时
    派发滚轮，表现就是一个滚轮事件切好几页。
    """
    try:
        hook.tid = kernel32.GetCurrentThreadId()
        hook.hhook = user32.SetWindowsHookExW(WH_MOUSE_LL, hook._proc, None, 0)
    except Exception:
        hook.hhook = None
    hook.ready.set()
    if not hook.hhook:
        return
    msg = MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret <= 0 or msg.message == WM_APP_STOP:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    try:
        if hook.hhook:
            user32.UnhookWindowsHookEx(hook.hhook)
            hook.hhook = None
    except Exception:
        pass  # 可忽略：幂等清理/UI 收尾


class ClickAwayHook:
    """菜单显示期间的低层鼠标钩子：菜单矩形外发生「按下」就置位 hit。

    与 WheelHook 同一套线程模型：回调里不碰 Tk，主线程轮询 self.hit。
    关键点是**永远放行事件**（始终 CallNextHookEx），所以用户点别的窗口时
    那一下点击照常生效，不会像 grab 那样被菜单吞掉。
    """

    def __init__(self, rect_provider):
        self.rect_provider = rect_provider
        self.hhook = None
        self.tid = 0
        self.hit = 0.0          # 最近一次「点在外面」的时间戳（0=没有）
        self.ready = threading.Event()
        self._proc = HOOKPROC(self._callback)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.ready.wait(0.4)

    def alive(self):
        return bool(self.hhook)

    def _callback(self, ncode, wparam, lparam):
        try:
            if ncode == 0 and wparam in WM_MOUSE_DOWNS:
                info = MSLLHOOKSTRUCT.from_address(lparam)
                r = self.rect_provider()
                x, y = info.pt.x, info.pt.y
                if not (r and r[0] <= x < r[2] and r[1] <= y < r[3]):
                    self.hit = time.time()
        except Exception as e:
            swallow("hooks.mouse_cb", e)
        # 一律放行：不吞用户的点击，只做旁路观察
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _run(self):
        _hook_thread_main(self)

    def destroy(self):
        try:
            if self.tid:
                user32.PostThreadMessageW(self.tid, WM_APP_STOP, 0, 0)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        t = self._thread
        if t is not None and t.is_alive():
            t.join(0.3)
        self.hhook = None


class WheelHook:
    """独立线程内的低级鼠标钩子：光标落在窗口矩形内时把滚轮增量写入队列。

    Tk 主线程只读取队列，回调里不触碰任何 Tk 控件；线程自带消息循环，
    阻塞等待期间 GIL 由 ctypes 自动释放，不会卡住界面。
    """

    def __init__(self, rect_provider, sink):
        self.rect_provider = rect_provider
        self.sink = sink
        self.hhook = None
        self.tid = 0
        self.ready = threading.Event()
        self.last_ts = 0.0      # 最近一次命中窗口的滚轮时间戳（Tk 侧据此让位）
        self.last_event = 0.0   # 最近一次任意鼠标事件（活性心跳，用于判断钩子是否被系统摘掉）
        self._proc = HOOKPROC(self._callback)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.ready.wait(0.5)    # 只等半秒：钩子线程通常几十毫秒就绪，别阻塞 UI 启动

    def alive(self):
        return bool(self.hhook)

    def _callback(self, ncode, wparam, lparam):
        try:
            if ncode == 0:
                self.last_event = time.time()
                if wparam == WM_MOUSEWHEEL:
                    info = MSLLHOOKSTRUCT.from_address(lparam)
                    r = self.rect_provider()
                    if r and r[0] <= info.pt.x < r[2] and r[1] <= info.pt.y < r[3]:
                        delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                        if delta:
                            now = time.time()
                            self.last_ts = now
                            # 带上采集时刻：上层据此合并"同一物理滚轮被多条钩子链路重复派发"
                            self.sink.append((now, delta))
        except Exception as e:
            swallow("hooks.wheel_cb", e)
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _run(self):
        _hook_thread_main(self)

    def destroy(self):
        """请求钩子线程退出并等它摘完钩子（最多 0.4s），避免残留钩子重复派发"""
        try:
            if self.tid:
                user32.PostThreadMessageW(self.tid, WM_APP_STOP, 0, 0)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        t = self._thread
        if t is not None and t.is_alive():
            t.join(0.4)
        self.hhook = None
