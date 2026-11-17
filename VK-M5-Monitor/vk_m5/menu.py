# -*- coding: utf-8 -*-
"""右键菜单（tk 实现）。"""

import ctypes
import os
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, byref, sizeof, c_int, c_void_p
from ctypes.wintypes import (DWORD, HICON, HINSTANCE, HMENU, HWND, LPCWSTR,
                             LPVOID, MSG, POINT, RECT, UINT, WCHAR, WPARAM,
                             LPARAM, BOOL)

from .config import (MENU_AUTO_CLOSE, MENU_PICK_DELAY,
                      MENU_POLL_CLICK, MENU_TICK)
from .paths import swallow
from .hooks import ClickAwayHook
from .win32 import user32

def _hide_win32(px, py, w, h, log=None):
    """把落在 (px,py,w,h) 这个矩形上的 Tk 顶层窗口隐藏并挪出屏幕。

    Tk 在 Windows 上，overrideredirect 的 Toplevel 会额外挂一层 wrapper 窗口，
    destroy() 之后这层 wrapper 有时仍被判为「可见」，于是菜单看起来没关掉。
    这里按矩形精确匹配，只处理 TkTopLevel，不会误伤别的窗口。
    """
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    target = (int(px), int(py), int(px) + int(w), int(py) + int(h))
    hit = []

    def _cb(hwnd, _lparam):
        try:
            buf = ctypes.create_unicode_buffer(64)
            u32.GetClassNameW(hwnd, buf, 64)
            if buf.value == "TkTopLevel":
                r = wintypes.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                if (r.left, r.top, r.right, r.bottom) == target:
                    u32.ShowWindow(hwnd, 0)
                    u32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0,
                                     0x0001 | 0x0004)  # SWP_NOSIZE | SWP_NOZORDER
                    hit.append(hwnd)
        except Exception as e:
            swallow("menu.hide_native", e)
        return True

    u32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                       wintypes.LPARAM)(_cb), 0)
    if log and hit:
        log("hid_win32 %d" % len(hit))
    return hit


def _menu_measure(rows, font, pad_x, base=150):
    """按文本宽度估菜单最小宽度（真实尺寸稍后由 _menu_tighten 收紧）"""
    width = base
    for it in rows:
        if it.get("sep"):
            continue
        width = max(width, font.measure(str(it.get("label", ""))) + pad_x * 2 + 26)
    return width


def _menu_tighten(top, box, width):
    """按控件真实请求尺寸收紧窗口，返回 (width, height)

    不用 item_h/sep_h 估算：估出来的每行高比 Label 真实高度大，差值全堆在
    菜单最下方 —— 那就是「右键菜单底部多出一截空白」。
    """
    height = 1
    try:
        top.update_idletasks()
    except Exception:
        pass  # 可忽略：幂等清理/UI 收尾
    try:
        need_h = int(box.winfo_reqheight()) + 2   # +2 = Toplevel 1px 边框×2
        need_w = int(box.winfo_reqwidth()) + 2
    except Exception:
        need_h, need_w = 0, 0
    if need_h > 1:
        height = need_h
    if need_w:
        width = max(width, need_w)
    return width, height


def _menu_place(top, x, y, width, height, sw=0, sh=0):
    """紧贴鼠标点弹出；放不下就翻到左/上，最后夹进工作区边界

    旧版只把坐标 clamp 在屏幕范围内：光标在屏幕底部（任务栏托盘图标）时，
    菜单被顶到屏幕里同一个高度，看起来就是「菜单位置固定、不跟鼠标」。
    """
    if not sw or not sh:
        try:
            sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
    mx, my = int(x), int(y)
    try:
        _pt = POINT()
        user32.GetCursorPos(byref(_pt))
        if _pt.x or _pt.y:
            mx, my = int(_pt.x), int(_pt.y)
    except Exception as e:
        swallow("menu.place.cursor", e)
    # 用工作区（扣掉任务栏）算边界，避免菜单压住任务栏或被任务栏遮住
    wx0, wy0, wx1, wy1 = 0, 0, sw, sh
    try:
        _rc = RECT()
        if user32.SystemParametersInfoW(0x0030, 0, byref(_rc), 0):
            if _rc.right - _rc.left > width and _rc.bottom - _rc.top > height:
                wx0, wy0, wx1, wy1 = _rc.left, _rc.top, _rc.right, _rc.bottom
    except Exception as e:
        swallow("menu.place.workarea", e)

    def _pick_axis(pos, size, lo, hi, bias=-6):
        """优先向右/下展开，放不下就翻到左/上，最后再夹进边界"""
        a = pos + bias
        if a + size > hi:
            a = pos - size - bias if pos - size - bias >= lo else hi - size
        return int(min(max(lo, a), max(lo, hi - size)))

    px = _pick_axis(mx, width, wx0, wx1, bias=-4)
    py = _pick_axis(my, height, wy0, wy1, bias=-10)
    return px, py


# ---------- 菜单配色（与浮窗主题不同：菜单用中性深灰）----------
MENU_BG = "#2f3136"
MENU_FG = "#e8eaed"
MENU_DIM = "#8b8f96"
MENU_HOT = "#3d6fb5"
MENU_SEP = "#4a4d53"


class TkMenu:
    """自绘右键菜单（原 popup_menu_tk 里的 12 个闭包收拢成类）。

    grab 的坑：Tk 的 grab_set 会把鼠标事件统统送到 grab 窗口本身，子控件收不到
    <Button-1>。所以这里**只**在 Toplevel 上绑定事件，再用 winfo_containing
    自己算点击落在哪一行，命中即执行、未命中即关闭。
    """

    PAD_X = 14
    SEP_H = 7

    def __init__(self, root, x, y, spec, on_close=None, owner=None):
        self.root = root
        # owner：菜单归属于哪个窗口（完整模式是主面板，简洁模式是圆盘文字层）。
        # 「失焦即关」和 Toplevel 的宿主都得用这个，而不是恒用 root ——
        # 简洁模式下 root 是 withdraw 的，用 root 判前台会让菜单刚弹出就自关。
        self.owner = owner if owner is not None else root
        self.x, self.y = int(x), int(y)
        self.spec = [dict(it) for it in spec]
        self.on_close = on_close
        self.top = None
        self.box = None
        self.hit = {}
        self.closed = False
        self.hot = None
        self.armed = False          # 菜单是否真的拿到过前台
        self.btn_down = False
        self.clickaway = None
        self.px = 0
        self.py = 0
        self.width = 1
        self.height = 1
        self._tk = None

    # ---------- 构建 ----------
    def build(self):
        """建窗口、布行、定位并启动检测，返回 Toplevel。"""
        import tkinter as tk
        from tkinter import font as tkfont
        self._tk = tk
        try:
            f = tkfont.Font(family="Microsoft YaHei UI", size=9)
        except Exception:
            f = tkfont.Font(size=9)

        self.width = _menu_measure(self.spec, f, self.PAD_X)
        # 高度不用 item_h/sep_h 估算：估出来的每行高比 Label 真实高度大，差值
        # 全堆在菜单最下方 —— 那就是用户看到的「右键菜单底部多出一截空白」。
        # 先建控件，再拿真实请求尺寸收紧窗口。
        top = self.top = tk.Toplevel(self.owner)
        top.withdraw()
        try:
            top.overrideredirect(True)
            top.attributes("-topmost", True)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        top.configure(bg=MENU_BG, highlightthickness=1,
                      highlightbackground="#5b5f66")

        box = self.box = tk.Frame(top, bg=MENU_BG)
        box.pack(fill="both", expand=True)

        self._build_rows(f)
        # ---------- 按控件真实尺寸收紧窗口 ----------
        self.width, self.height = _menu_tighten(top, box, self.width)
        # ---------- 定位：紧贴鼠标右键点弹出 ----------
        # 一律取当前真实光标坐标，并做四向翻转——右下放不下就翻到左/上，
        # 保证菜单始终贴着鼠标那一角弹出。
        self.px, self.py = _menu_place(top, self.x, self.y,
                                       self.width, self.height)
        top.geometry("%dx%d+%d+%d" % (self.width, self.height,
                                      self.px, self.py))
        self._bind()
        self._activate()
        return top

    def _build_rows(self, font):
        tk = self._tk
        for it in self.spec:
            if it.get("sep"):
                tk.Frame(self.box, bg=MENU_SEP, height=1).pack(
                    fill="x", pady=(self.SEP_H - 1) // 2)
                continue
            head = bool(it.get("header"))
            enabled = bool(it.get("enabled", True)) and not head
            lb = tk.Label(self.box, text=str(it.get("label", "")), font=font,
                          bg=MENU_BG, fg=(MENU_DIM if not enabled else MENU_FG),
                          anchor="w", padx=self.PAD_X, pady=0, bd=0, height=1)
            lb.pack(fill="x")
            if enabled:
                self.hit[lb] = it

    def _bind(self):
        top = self.top
        top.bind("<Motion>", self._on_motion, add="+")
        top.bind("<Button-1>", self._on_click, add="+")
        top.bind("<Button-3>", self._on_dismiss, add="+")
        top.bind("<Escape>", self._on_dismiss, add="+")
        top.bind("<FocusOut>", self._on_focus_out, add="+")

    def _activate(self):
        """显示菜单、抢焦点、装外部点击监听、起定时器"""
        # 逐个 try：v5 把 deiconify/lift/focus_force/grab_set 塞在同一个 try 里，
        # 前一步抛异常后面就不执行，菜单于是「点别处不关闭、要等兜底」。
        for step in (self.top.deiconify, self.top.lift):
            try:
                step()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        try:
            self.top.focus_force()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        # 不再 grab_set：Tk 的 grab 会把用户点别处的第一下吞给菜单（点桌面图标
        # 像没反应），而 grab 失败时又完全收不到外部点击。改为旁路低层钩子监听。
        try:
            ca = ClickAwayHook(
                lambda: (self.px, self.py,
                         self.px + self.width, self.py + self.height))
            self.clickaway = ca
            self._log("clickaway hook alive=%s" % (bool(ca.hhook),))
        except Exception as ex:
            self._log("clickaway_err %r" % (ex,))
        try:
            self.top.after(MENU_TICK, self._tick)
            self.top.after(MENU_POLL_CLICK, self._poll_click_away)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        # 兜底：所有检测都失效时也别把菜单一直挂屏幕上
        try:
            self.root.after(MENU_AUTO_CLOSE, self.close)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    # ---------- 生命周期 ----------
    def close(self):
        if self.closed:
            return
        self.closed = True
        ca, self.clickaway = self.clickaway, None
        if ca is not None:
            try:
                ca.destroy()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        # 先 withdraw 让窗口立刻不可见（destroy 万一失败也不会留下「残影」
        # ——之前的版本就是菜单内容卡在屏幕上不消失），随后再销毁窗口。
        try:
            self.top.grab_release()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        try:
            self.top.withdraw()
        except Exception as ex:
            self._log("withdraw_err %r" % (ex,))
        # 双保险：Tk 的 overrideredirect 窗口在 withdraw() 之后，win32 那一层
        # 偶尔仍处于可见状态（表现为菜单内容卡在屏幕上不消失），所以这里直接
        # 按「同一矩形」把菜单的 win32 窗口找出来，隐藏并挪到屏幕外。
        try:
            _hide_win32(self.px, self.py, self.width, self.height, self._log)
        except Exception as ex:
            self._log("swhide_err %r" % (ex,))
        try:
            self.top.destroy()
        except Exception as ex:
            self._log("destroy_err %r" % (ex,))
        # update() 而不是 update_idletasks()：窗口的销毁要走完整事件循环，
        # 只跑 idle 任务的话窗口会「幽灵」般地留在屏幕上，过一会儿才消失。
        try:
            self.root.update()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        self._log("closed")
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾

    def pick(self, it):
        act = it.get("action")
        self.close()
        if act is None:
            return
        try:
            self.root.after(MENU_PICK_DELAY, act)
        except Exception:
            try:
                act()
            except Exception as e:
                swallow("menu.pick.act", e)

    # ---------- 命中 / 高亮 ----------
    def _widget_at(self, xr, yr):
        try:
            w = self.top.winfo_containing(xr, yr)
        except Exception:
            return None
        while w is not None:
            if w in self.hit:
                return w
            if w is self.top or w is self.box:
                return None
            try:
                w = w.master
            except Exception:
                return None
        return None

    def _highlight(self, w):
        if self.hot is w:
            return
        old = self.hot
        self.hot = w
        if old is not None:
            try:
                old.configure(bg=MENU_BG, fg=MENU_FG)
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        if w is not None:
            try:
                w.configure(bg=MENU_HOT, fg="#ffffff")
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾

    # ---------- 事件回调 ----------
    def _on_motion(self, e):
        self._highlight(self._widget_at(e.x_root, e.y_root))

    def _on_click(self, e):
        w = self._widget_at(e.x_root, e.y_root)
        self._log("click x=%s y=%s hit=%s" % (e.x_root, e.y_root, w))
        if w is not None:
            self.pick(self.hit[w])
        else:
            self.close()

    def _on_dismiss(self, e=None):
        self.close()

    def _on_focus_out(self, e=None):
        if self.closed or not self.armed:
            return
        try:
            if self.top.focus_displayof() is None:
                self.close()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _fg_gone(self):
        """前台不再是菜单本身 -> 关闭（覆盖 Alt+Tab、点任务栏、点别的窗口）"""
        if self.closed:
            return
        try:
            u = ctypes.windll.user32
            fg = u.GetForegroundWindow()
            mh = u.GetAncestor(int(self.top.winfo_id()), 2) \
                or int(self.top.winfo_id())
            if not (fg and mh):
                return
            if fg == mh or u.GetAncestor(fg, 2) == mh:
                self.armed = True
            else:
                # 浮窗自己抢到前台时也要认定为「已激活」：菜单是
                # overrideredirect 窗口，弹出前主程序会 focus_force 浮窗，
                # 前台可能一直停在浮窗上，导致 armed 永远为 False、
                # 「点别处即关」整条逻辑失效（用户看到的就是菜单赖在屏幕上）。
                try:
                    rh = int(ctypes.windll.user32.GetAncestor(
                        int(self.owner.winfo_id()), 2)) \
                        or int(self.owner.winfo_id())
                except Exception:
                    rh = 0
                if rh and (fg == rh or u.GetAncestor(fg, 2) == rh):
                    self.armed = True
                elif self.armed and u.IsWindowVisible(fg):
                    self.close()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _poll_click_away(self):
        """兜底：轮询鼠标按键状态，按下点落在菜单外就关

        纯读取 GetAsyncKeyState / GetCursorPos，既不吞用户的点击，也不依赖
        低层钩子是否安装成功——之前「点了别处菜单不消失」就是因为钩子这条路
        在打包 exe 里偶发失效，而失焦检测又被浮窗抢前台挡住。
        """
        if self.closed:
            return
        u = ctypes.windll.user32
        try:
            down = bool((u.GetAsyncKeyState(0x01) & 0x8000) or
                        (u.GetAsyncKeyState(0x02) & 0x8000))
        except Exception:
            down = False
        if down and not self.btn_down:
            try:
                pt = POINT()
                u.GetCursorPos(ctypes.byref(pt))
                inside = ((self.px <= pt.x < self.px + self.width) and
                          (self.py <= pt.y < self.py + self.height))
            except Exception:
                inside = True
            if not inside:
                self._log("poll_click_away")
                self.close()
                return
        self.btn_down = down
        try:
            self.top.after(MENU_POLL_CLICK, self._poll_click_away)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _tick(self):
        if self.closed:
            return
        ca = self.clickaway
        if ca is not None and ca.hit:
            self.close()
            return
        self._fg_gone()
        if self.closed:
            return
        try:
            self.top.after(MENU_TICK, self._tick)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    # ---------- 日志 ----------
    def _log(self, msg):
        try:
            with open(os.path.join(os.environ.get("TEMP", "."), "vk_menu.log"),
                      "a", encoding="utf-8") as fh:
                fh.write("%.3f %s\n" % (time.time(), msg))
        except Exception:
            pass  # 可忽略：日志失败不影响菜单


def popup_menu_tk(root, x, y, spec, on_close=None, owner=None):
    """在 (x, y) 弹出自绘菜单（同一套 spec 结构），返回 Toplevel。"""
    return TkMenu(root, x, y, spec, on_close, owner).build()
