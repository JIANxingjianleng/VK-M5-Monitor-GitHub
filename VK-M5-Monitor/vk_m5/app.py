# -*- coding: utf-8 -*-
"""App 装配层：后台采集线程 + 界面消费 + 交互入口。"""

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

from .ble import ble_link_status, bt_link_probe, bt_pnp_warm
from .config import ACCENT, BAD, BG, BT_LINK_WATCH, BT_PENDING_POLL, BT_POLL_BASE, BT_POLL_MAX, BT_WAIT_HINT, CELL_OFF, DEAD_CONFIRM, DEAD_TTL, DIM, DPI_LEVELS, dpi_tiers, FAKE_TTL, FG, FONT, HUB_URL, LINK_PROBE, LINK_PROBE_DEEP, LINK_PROBE_MISS, LINK_WATCH, NOFRAME_FAST_POLL, PAGE_BATT, PAGE_DPI, POLL_OFFLINE, POLL_ONLINE, STILL_POLL, STACK_H, USB_BT_GRACE, USB_MISS_BT, VENDOR_PAGES, VID, WARN, WIN_H, WIN_W
from .device import _ACT_CACHE, _handshake, _hid, _usb_alive, _usb_ctrl_exists, deep_probe, probe_link
from . import devices
from .paths import CACHE_PATH, _dbg, swallow
from .reader import Reader, warm_text_ok
from .theme import battery_cells, battery_color, color_rgb

# v21：有线充电态电池里的细长闪电（按内框比例）
_CHARGE_BOLT = ((0.62, 0.00), (0.30, 0.54), (0.47, 0.54),
                (0.40, 1.00), (0.72, 0.46), (0.55, 0.46))

# v17：2.4G 侧记不到设备身份（接收器无序列号、私有帧无编号），唯一能当「这份
# 读数还是不是当前这只鼠标的」用的就是时间——鼠标静止时读数本来就会沿用，
# 但隔了半天再开机还摆着昨天的数，那就是上一只鼠标的残值了。超过这个时长
# 就当作「没有可用的缓存电量」，等第一轮真实采集。
USB_BATT_TTL = 6 * 3600

class App:
    def __init__(self):
        _boot = time.time()
        self.reader = Reader()
        # v13.6：启动 3 秒后（等界面画完）在后台把 BLE 地址枚举热起来。首次
        # 走 WinRT 要 48 秒，全是运行时初次加载的开销；等用户真把鼠标拨到蓝牙
        # 时地址已就绪，就不必在那一刻现等。
        def _warm_bt():
            time.sleep(3.0)
            try:
                bt_pnp_warm()
            except Exception as e:
                swallow("app.warm_bt", e)
        try:
            threading.Thread(target=_warm_bt, daemon=True,
                             name="warm-bt").start()
        except Exception as e:
            swallow("app.warm_bt.spawn", e)
        self.last_battery = None
        self.online = False
        self.err = ""
        self.dpis = []
        self.dpi_idx = None
        self._dpi_busy = False      # 调档进行中（后台线程在跑，防重复点击）
        self.hidden = False
        self.tray = None
        self.page = PAGE_BATT
        self.feedback = ""
        self.feedback_until = 0.0
        self._moved = False
        self._wheel_ts = 0.0
        self._wheel_acc = 0
        self._wheel_unit = 0        # 一格滚动的 delta 基准（自适应，0=未定）
        self._wheel_smooth = False  # 是否为平滑滚动（触控板/无级滚轮）
        self._last_cursor = None
        self._wake_ts = 0.0
        self.hook = None
        self._cached = {}
        # v16：缓存里的电量属于哪台设备（"usb" / "bt:<12位地址>"）。同型号鼠标
        # VID/PID 一样，只有蓝牙地址能区分；对不上就说明换了鼠标，旧读数作废。
        self._cached_batt_key = ""
        # v17：这份电量的采集时刻，用来判断 2.4G 侧的缓存还新不新鲜
        self._cached_batt_ts = 0.0
        # v21：有线充电态的呼吸动画（「充电中」三个字的明暗循环）
        self._charge_phase = 0.0
        self._charge_after = None
        self._menu_open = False     # 自绘菜单是否在屏幕上（简洁模式重绘时用来避让）
        # v14：简洁模式（小圆盘）。simple 为懒创建 —— 只用完整面板的用户
        # 永远不会为圆盘付出建窗口/画位图的成本。
        self.compact = False
        self.simple = None
        self._simple_pos = None
        # 连接方式：wired 有线 / 2.4g 无线 / unknown；override 为手动强制指定
        self.link = "unknown"
        self.link_pid = None
        self.link_pids = []
        # 当前鼠标型号名：按 PID 反查（A955 / VK M5），标题与托盘 tooltip 都跟它走
        self.dev_name = devices.DEFAULT["name"]
        self._src = ""
        self._batt_note = ""    # 电量来源的补充说明（"cache"=蓝牙读的是系统缓存）
        # v12.0：静止沿用。鼠标暂时不动时设备不上报，读数沿用上一次真实采集，
        # 界面不能因此报「未收到回包」；尾行改成标明读数的时刻。
        self._stale = False
        self._stale_ts = 0.0
        self._still = False
        self._bt_unk = False    # v12.1：蓝牙链路状态待确认（既非蓝牙也非静止）
        self._nosig = 0.0       # v12.1：两侧都没证据已经持续多久（秒）
        self._ch = True
        # 后台采集线程共享数据
        self._data = None
        self._data_lock = threading.Lock()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._last_data = None
        self._last_write_ts = 0.0   # 最近一次读到有效电量的时刻（判断过渡期用）
        self._boot_ts = _boot       # 窗口建立时刻：启动宽限从此刻算
        self._got_data = False      # 是否已经跑完过一轮「真的读到东西」的采集
        # v11.8：冷启动「暖机」判定。启动瞬间常常是先通一下又断（GATT 建连、
        # 接收器刚上电），只看 _got_data 会在这一瞬间就报「未收到回包」——实机
        # 反馈「打开软件就看到未响应」多半是这里。改成：在线状态持续 3 秒以上
        # 才算暖机完成，暖机没完成之前一律写「识别中…」。
        self._first_ok_ts = 0.0
        self._warm_done = False

        self.root = tk.Tk()
        # 先藏起来：Tk 默认窗口是"先建后配"，不藏会在屏幕上闪一个灰白空框
        try:
            self.root.withdraw()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        self.root.title(self.dev_name)
        # overrideredirect 会触发一次窗口 map：不先把窗口挪到屏幕外并置全透明，
        # 启动瞬间屏幕中央会闪一个默认大小（200x200）的灰色空框 —— 这就是"打开
        # 软件时多出来的那个框"。这里先 1x1 藏到屏幕外、alpha=0，等尺寸位置都
        # 排好、真正要显示时再恢复透明度。
        try:
            self.root.geometry("1x1+-4000+-4000")
            self.root.attributes("-alpha", 0.0)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.win_w = self._calc_width()
        _dbg("boot tk_ready t=%.2f" % (time.time() - _boot))
        self._build()
        _dbg("boot built t=%.2f" % (time.time() - _boot))
        self._place()
        _dbg("boot placed t=%.2f" % (time.time() - _boot))
        # 尺寸/位置都排好了再露面，避免启动瞬间闪出默认大小的空窗口
        try:
            self.root.attributes("-alpha", 0.96)
            self.root.deiconify()
            self.root.update_idletasks()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        # 钩子抢在托盘/缓存之前挂：托盘初始化要画图标、注册系统托盘，主线程会被
        # 占住一两秒，期间窗口已经显示但滚轮完全没反应，用户感知就是“刚打开时
        # 滚动不灵”。钩子只起线程，放这里几乎零成本。
        self._install_hook()
        self._init_tray()
        self._load_cache()
        # v16：开机先认一遍缓存里那份电量是不是当前这只鼠标的
        self.root.after(900, self._audit_batt_owner)
        if self.compact:
            # v14：上次退出时停在简洁模式，启动就直接落成圆盘形态
            self.root.after(80, lambda: self._set_compact(True, save=False))
        self._start_worker()
        self.root.after(60, self._pump)
        self.root.after(120, self._tick)

    # ---------- UI ----------
    def _calc_width(self):
        """按最长文案实测宽度，避免文字显示不全"""
        try:
            f8 = tkfont.Font(family=FONT, size=8)
            f9 = tkfont.Font(family=FONT, size=9)
            f10 = tkfont.Font(family=FONT, size=10, weight="bold")
            f21 = tkfont.Font(family=FONT, size=21, weight="bold")
            cands = [
                # 连接模式现在显示在右上角（原「在线」位），高度行按最长的
                # 「2.4G 已连接」算宽度；底部只留时间与电量口径说明。
                f10.measure("VK M5") + 20 + f8.measure("2.4G 已连接"),
                f21.measure("100%") + 9 + 60,
                f21.measure("3200") + 6 + f9.measure("DPI"),
                f8.measure("已对齐 2.4G/蓝牙 · 19:01:01 更新"),
                f8.measure("蓝牙缓存 · 19:01:01 更新"),
                f8.measure("蓝牙直读 · 19:01:01 更新"),
                f8.measure("HID 直读 · 19:01:01 更新"),
                f8.measure("19:01:01 更新 · 电量固定满格"),
                f8.measure("设备直读 · 19:01:01 更新"),
                f8.measure("第 5/6 档 · 点击或滚轮循环切换"),
                f8.measure("移动鼠标唤醒后自动更新"),
            ]
            need = int(max(cands)) + 48
        except Exception:
            need = WIN_W
        return max(WIN_W, need)

    def _build(self):
        wrap = tk.Frame(self.root, bg=BG, highlightthickness=1,
                        highlightbackground="#2c2c33",
                        width=self.win_w, height=WIN_H)
        wrap.pack(fill="both", expand=True)
        wrap.pack_propagate(False)
        self.wrap = wrap

        head = tk.Frame(wrap, bg=BG, height=24)
        head.pack(fill="x", padx=11, pady=(6, 0))
        head.pack_propagate(False)
        self.name_lbl = tk.Label(head, text=self.dev_name, bg=BG, fg=FG,
                                 font=(FONT, 10, "bold"))
        self.name_lbl.pack(side="left")
        self.state_lbl = tk.Label(head, text="…", bg=BG, fg=DIM,
                                  font=(FONT, 8))
        self.state_lbl.pack(side="right")

        stack = tk.Frame(wrap, bg=BG, width=self.win_w - 22, height=STACK_H)
        stack.pack(padx=11, pady=(2, 0))
        stack.pack_propagate(False)
        self.stack = stack

        # --- 第 1 页：电量 ---
        p1 = tk.Frame(stack, bg=BG)
        p1.place(x=0, y=0, relwidth=1, relheight=1)
        self.page_batt = p1
        row = tk.Frame(p1, bg=BG)
        row.pack(anchor="w", pady=(4, 0))
        self.pct_lbl = tk.Label(row, text="--%", bg=BG, fg=DIM,
                                font=(FONT, 21, "bold"))
        self.pct_lbl.pack(side="left")
        self.batt_cv = tk.Canvas(row, width=56, height=26, bg=BG,
                                 highlightthickness=0)
        self.batt_cv.pack(side="left", padx=(9, 0), pady=(3, 0))
        self.tip_lbl = tk.Label(p1, text="", bg=BG, fg=DIM,
                                font=(FONT, 8), anchor="w", justify="left")
        self.tip_lbl.pack(anchor="w", fill="x", pady=(3, 0))

        # --- 第 2 页：DPI ---
        p2 = tk.Frame(stack, bg=BG)
        p2.place(x=0, y=0, relwidth=1, relheight=1)
        self.page_dpi = p2
        row2 = tk.Frame(p2, bg=BG)
        row2.pack(anchor="w", pady=(4, 0))
        self.dpi_lbl = tk.Label(row2, text="--", bg=BG, fg=FG,
                                font=(FONT, 21, "bold"), cursor="hand2")
        self.dpi_lbl.pack(side="left")
        tk.Label(row2, text="DPI", bg=BG, fg=DIM,
                 font=(FONT, 9)).pack(side="left", padx=(4, 0), pady=(9, 0))
        self.dpi_sub = tk.Label(p2, text="", bg=BG, fg=DIM,
                                font=(FONT, 8), anchor="w", justify="left")
        self.dpi_sub.pack(anchor="w", fill="x", pady=(3, 0))

        # --- 页指示 ---
        self.dot_cv = tk.Canvas(wrap, width=44, height=10, bg=BG,
                                highlightthickness=0)
        self.dot_cv.pack(pady=(2, 5))

        self._bind_all(self.root)

        # --- 刷新按钮：贴在「在线」状态正下方（右上角） ---
        # 用 Label 不用 Button：Button 的点击靠 class binding 触发，若在实例上
        # 拦事件就会连 command 一起拦掉；用 Label 自己控事件，才能在吃掉
        # ButtonRelease 的同时不影响点击，也避免带动窗口拖拽 / DPI 页误切档
        # （浮窗的 ButtonRelease 绑了「点一下切下一档」）。
        self.refresh_btn = tk.Label(
            wrap, text="\u21bb", bg=BG, fg=DIM,
            font=(FONT, 10, "bold"), cursor="hand2",
            bd=0, padx=0, pady=0)
        self.refresh_btn.place(relx=1.0, x=-9, y=30, anchor="ne",
                               width=21, height=21)
        self.refresh_btn.bind("<Button-1>", lambda e: "break")
        self.refresh_btn.bind("<ButtonRelease-1>", self._refresh_click)
        self.refresh_btn.bind("<Enter>", lambda e: self.refresh_btn.configure(
            fg=ACCENT, bg="#26262e"))
        self.refresh_btn.bind("<Leave>", lambda e: self.refresh_btn.configure(
            fg=DIM, bg=BG))

        self._draw_dots()
        self.page_batt.lift()

    def _refresh_click(self, e=None):
        self.refresh_btn.configure(fg=DIM, bg=BG)
        # 标记成「已拖动」：防止下一次 ButtonRelease 掉到窗口上时被当成
        # 「点浮窗切下一档」，在 DPI 页点刷新会连着改档位。
        self._moved = True
        self.refresh()
        return "break"

    def _bind_all(self, w):
        for c in w.winfo_children():
            self._bind_all(c)
        w.bind("<Button-3>", self._popup)
        w.bind("<Button-1>", self._drag_start)
        w.bind("<B1-Motion>", self._drag_move)
        w.bind("<ButtonRelease-1>", self._click_release)
        w.bind("<MouseWheel>", self._wheel)

    def _draw_dots(self):
        c = self.dot_cv
        c.delete("all")
        for i in range(2):
            x = 14 + i * 14
            on = (i == self.page)
            c.create_oval(x, 2, x + 7, 9,
                          fill=(ACCENT if on else "#3a3a44"), outline="")

    def _place(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = self.win_w
        self.root.geometry(f"{w}x{WIN_H}+{sw - w - 30}+{sh - WIN_H - 90}")

    # ---------- 托盘 ----------
    def _init_tray(self):
        try:
            from .tray import Tray
            self.tray = Tray(self.dev_name)
        except Exception as ex:
            self.tray = None
            self.err = f"托盘不可用: {ex}"
            # 打包版没有可见报错，托盘建不起来时把原因落到文件里
            try:
                import os as _os
                import traceback as _tb
                base = _os.path.join(_os.environ.get("APPDATA", "."), "VK_M5")
                _os.makedirs(base, exist_ok=True)
                with open(_os.path.join(base, "tray_error.log"), "w",
                          encoding="utf-8") as _f:
                    _f.write(_tb.format_exc())
            except Exception:
                pass

    # ---------- 滚轮（全局钩子 + 窗口内事件） ----------
    def _install_hook(self):
        """装全局滚轮钩子

        滚轮消息只投给焦点窗口，浮窗没被点过时 Tk 收不到，所以必须靠全局钩子。
        挂钩子要等窗口真正 mapped 再挂（窗口没显示时挂上去也命不中），但一旦
        mapped 就立刻挂，配合挂载后的预热事件，启动第一秒就能滚。挂载失败或被
        系统摘掉由 _poll_wheel 里的活性巡检自动重挂。
        """
        self._rect = None
        self._hwnd = 0
        self._wheel_queue = []
        self._hook_on = False
        self._wheel_last = 0.0
        self._hook_boot_ts = time.time()
        # 同步挂：启动时主线程还要忙一秒多（建控件、画界面、装托盘），
        # 这段窗口已经显示了却滚不动。钩子挂载本身只起线程、不耗时，直接同步挂。
        self._attach_hook()
        if not self._hwnd:
            self.root.after(60, self._attach_hook_when_ready)
        # 12ms 一轮：滚轮帧要尽快消费掉才跟手（20ms 已有轻微延迟感，80ms 更明显）
        self.root.after(12, self._poll_wheel)

    def _rect_for_hook(self):
        """钩子线程用的命中矩形：缓存优先，缓存还没算出来就用缓存的 hwnd 现取

        窗口句柄在挂载时于主线程取好，回调线程里只做纯 Win32 调用，不碰 Tk。
        """
        r = self._rect
        if r is not None:
            return r
        h = getattr(self, "_hwnd", 0)
        if not h:
            return None
        try:
            import ctypes
            from ctypes import wintypes
            rc = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(h, ctypes.byref(rc)):
                return (rc.left, rc.top, rc.right, rc.bottom)
        except Exception as e:
            swallow("app._rect_for_hook", e)
        return None

    def _attach_hook_when_ready(self):
        """窗口一 mapped 就挂钩子，最多等 3 秒窗口仍未显示则照样挂"""
        try:
            ready = not (self.hidden or not self.root.winfo_viewable())
        except Exception:
            ready = False
        if ready or time.time() - getattr(self, "_hook_boot_ts", 0.0) > 3.0:
            self._attach_hook()
            return
        try:
            self.root.after(60, self._attach_hook_when_ready)
        except Exception:
            self._attach_hook()

    def _prime_hook(self):
        """挂载后送一个 0 位移的鼠标移动，催促系统把钩子链路真正激活

        SetWindowsHookEx 返回非空只代表注册成功，回调链路往往要等第一个真实
        鼠标消息才建立；启动初期“第一下滚轮没反应”就是卡在这个空窗期。
        """
        try:
            import ctypes
            u = ctypes.windll.user32
            u.mouse_event(0x0001, 0, 0, 0, 0)     # MOUSEEVENTF_MOVE，位移 0
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _attach_hook(self):
        try:
            if getattr(self, "hook", None) is not None:
                self.hook.destroy()
                self.hook = None
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        # 取窗口真实句柄（Tk 的 winfo_id 是子窗，要往上取到顶层）
        try:
            import ctypes
            self._hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2) or 0
        except Exception:
            self._hwnd = 0
        _t_attach = time.time()
        try:
            from .hooks import WheelHook
            self.hook = WheelHook(self._rect_for_hook, self._wheel_queue)
            if not self.hook.alive():
                self.hook = None
        except Exception:
            self.hook = None
        _t2 = time.time()
        # 立刻算一次命中矩形：启动初期主线程忙，_poll_wheel 的 80ms 回调可能被
        # 推迟一两秒，这期间 _rect 若还是 None，钩子会把滚轮全部丢掉。
        self._sync_rect()
        _t3 = time.time()
        self._prime_hook()
        _dbg("attach_hook t_hook=%.3f t_rect=%.3f t_prime=%.3f" % (
            _t2 - _t_attach, _t3 - _t2, time.time() - _t3))
        del self._wheel_queue[:]
        self._wheel_acc = 0
        self._wheel_ts = 0.0
        self._hook_on = bool(self.hook)
        _dbg("attach_hook hook_on=%s took=%.3fs" % (self._hook_on, time.time() - _t_attach))

    def _sync_rect(self):
        """窗口矩形用 Win32 GetWindowRect 取

        钩子回调里的坐标是屏幕物理像素；Tk 的 winfo_rootx 在非 DPI 感知进程
        里是逻辑像素，高缩放比下两者对不上，命中区就会时不时偏移 —— 表现为
        “滚轮时灵时不灵”。统一用物理坐标，两边口径一致。
        """
        try:
            if self.hidden:
                self._rect = None
                return
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.root.winfo_id(), 2)   # GA_ROOT
            # 用 IsWindowVisible 而不是 Tk 的 winfo_viewable：后者要等主线程跑到
            # idle 才返回真值，启动瞬间刚 place 完还没 map，rect 会算成 None，
            # 钩子就把这期间的滚轮全丢了 —— 这就是“刚打开第一下滚动没反应”。
            if hwnd and u.IsWindowVisible(hwnd):
                rc = wintypes.RECT()
                if u.GetWindowRect(hwnd, ctypes.byref(rc)):
                    self._rect = (rc.left, rc.top, rc.right, rc.bottom)
                    return
        except Exception as e:
            swallow("app._sync_rect", e)
        try:
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            self._rect = (x, y, x + self.root.winfo_width(), y + self.root.winfo_height())
        except Exception:
            self._rect = None

    def _poll_wheel(self):
        self._sync_rect()
        _now = time.time()
        if _now - getattr(self, "_dbg_ts", 0.0) > 2.0:
            self._dbg_ts = _now
            _alive = None
            if self.hook is not None:
                try:
                    _alive = self.hook._thread.is_alive()
                except Exception:
                    _alive = None
            _ev_ago = (_now - getattr(self.hook, "last_event", 0.0)) if self.hook else -1.0
            _dbg("poll rect=%s hook_on=%s alive=%s ev_ago=%.1f q=%d page=%d" % (
                self._rect, self._hook_on, _alive, _ev_ago,
                len(self._wheel_queue), self.page))
        # 鼠标一动设备就会被唤醒：趁这时立刻刷新一次，休眠状态下也能马上读到数
        try:
            pos = (self.root.winfo_pointerx(), self.root.winfo_pointery())
        except Exception:
            pos = None
        if pos is not None and self._last_cursor is not None and pos != self._last_cursor:
            now = time.time()
            if now - self._wake_ts > 1.5 and not self._eff_online():
                self._wake_ts = now
                self._kick.set()
        self._last_cursor = pos
        if self._wheel_queue:
            frames = self._wheel_queue[:]
            del self._wheel_queue[:]
            # 主线程忙（启动初始化、重绘）时帧会积压：只补最近的 2 帧，避免
            # 界面恢复后一次性连跳好几页，看起来像“切页乱跳”。
            if len(frames) > 2:
                frames = frames[-2:]
                _dbg("wheel backlog collapsed to 2 frames")
            prev_ts = 0.0
            prev_d = 0
            for i, item in enumerate(frames):
                ts, d = item if isinstance(item, tuple) else (0.0, item)
                # 同一个物理滚轮可能被多条钩子链路同时派发（旧钩子没摘干净时），
                # 15ms 内同向的重复帧合并成一次，别让一格滚轮连跳两页
                if ts and prev_ts and (ts - prev_ts) < 0.010 and (d > 0) == (prev_d > 0):
                    prev_ts, prev_d = ts, d
                    continue
                prev_ts, prev_d = ts, d
                if i:
                    self._wheel_ts = 0.0    # 同一批内的帧不参与去重
                # 传真实 delta：原来一律压成 ±120，"一格 100/80" 的鼠标就得滚
                # 一格多一点才翻页，手感发涩。幅度交给 _wheel_by_delta 自适应。
                self._wheel_by_delta(int(d), src="hook")
        # 钩子活性巡检：主线程长时间持 GIL 会让系统静默摘掉低级钩子，
        # 表现为“用一阵子滚轮突然没反应”，这里发现死了就自动重挂。
        if self._hook_on and self.hook is not None:
            now2 = time.time()
            # 钩子线程若已退出（系统摘钩子、回调抛异常），句柄看着还在但永不
            # 再派发事件：这种"假活"必须先查出来立刻重挂
            try:
                _thr_alive = self.hook._thread.is_alive()
            except Exception:
                _thr_alive = True
            if not _thr_alive:
                _dbg("hook thread dead -> reattach")
                self._attach_hook()
            if now2 - getattr(self, "_hook_check_ts", 0.0) > 5.0:
                ev = getattr(self.hook, "last_event", 0.0)
                prev_cur = getattr(self, "_hook_chk_cur", None)
                prev_ev = getattr(self, "_hook_chk_ev", 0.0)
                self._hook_check_ts = now2
                self._hook_chk_cur = pos
                self._hook_chk_ev = ev
                if (prev_cur is not None and pos is not None and pos != prev_cur
                        and ev <= prev_ev):
                    _dbg("hook dead (cursor moved, no event) -> reattach")
                    self._attach_hook()
        try:
            # 12ms 一轮：滚轮帧要尽快消费掉才跟手（20ms 已有轻微延迟感，80ms 更明显）
            self.root.after(12, self._poll_wheel)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _wheel_by_delta(self, delta, ctrl=None, src="tk"):
        if getattr(self, "_menu_open", False):
            return                      # 菜单展开时不响应滚轮，避免误切页/误改档位
        if ctrl is None:
            try:
                import ctypes as _ct
                ctrl = bool(_ct.windll.user32.GetKeyState(0x11) & 0x8000)
            except Exception:
                ctrl = False
        if ctrl:
            # Ctrl+滚轮：DPI 档位循环
            now = time.time()
            if now - self._wheel_ts < 0.04:
                return
            self._wheel_ts = now
            if self.dpis and self.dpi_idx is not None:
                step = -1 if delta > 0 else 1
                n = dpi_tiers(self.dpis)
                if n:
                    self.set_dpi((min(self.dpi_idx, n - 1) + step) % n)
            return
        now = time.time()
        # 同一次滚动只认一帧（Tk 事件与全局钩子可能同刻到达）。窗口压到 10ms：
        # 双通道重复帧仍是同刻到达会被吞掉，但快速连滚的真实帧不再被吞
        # （原来 20ms 窗口会丢掉第二格，手感就是"滚了没反应"；6ms 再放宽一档）。
        if now - self._wheel_ts < 0.006:
            return
        self._wheel_ts = now
        gap = now - self._wheel_last
        # 手停超过 0.35 秒就清残余量，并重新判定这一轮滚动的"一格"基准
        if gap > 0.35:
            self._wheel_acc = 0
            self._wheel_unit = 0
            self._wheel_smooth = False
        # 帧间隔极短 = 平滑滚动（触控板/无级滚轮），这类细分帧按 120 累计；
        # 离散滚轮一帧就是一格，用这帧的 delta 当基准 —— 否则一格 100/80 的
        # 鼠标要滚一格多一点才翻页，用户感受就是"不跟手"。
        if gap < 0.03:
            self._wheel_smooth = True
        self._wheel_last = now
        d = abs(delta)
        if not self._wheel_smooth:
            self._wheel_unit = min(120, d) if d else 120
        unit = self._wheel_unit or 120
        # 无极循环翻页：累计滚动量，每满一格切一页；上滚/下滚都能切，两页永不到头
        self._wheel_acc += delta
        steps = int(self._wheel_acc / unit)     # 快速连滚可按格数连切
        _dbg("wheel src=%s delta=%d acc=%.0f steps=%d" % (src, delta, self._wheel_acc, steps))
        if steps:
            self._wheel_acc -= steps * unit
            for _ in range(min(abs(steps), 6)):
                self._cycle_page()
        # 长时间不滚就清掉残余量，避免半格残留累积成误切
        if abs(self._wheel_acc) > 600:
            self._wheel_acc = 0

    def _cycle_page(self):
        """两页无缝循环：电量 -> DPI -> 电量 ..."""
        target = PAGE_DPI if self.page == PAGE_BATT else PAGE_BATT
        _dbg("cycle %s->%s acc=%.0f" % (self.page, target, self._wheel_acc))
        self.show_page(target)

    # ---------- 本地缓存（休眠时保留上次读数） ----------
    def _load_cache(self):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                self._cached = json.load(f) or {}
        except Exception:
            self._cached = {}
        b = self._cached.get("battery")
        # v16：先记下这份电量属于哪台设备，供第一轮采集比对
        ck = self._cached.get("batt_key")
        self._cached_batt_key = ck if isinstance(ck, str) else ""
        # v17：这份电量是什么时候读到的。2.4G 侧只有时间能证明它还新鲜。
        cts = self._cached.get("batt_ts")
        self._cached_batt_ts = float(cts) if isinstance(cts, (int, float)) else 0.0
        # v19：不再把磁盘上的旧电量显示到界面。蓝牙 GATT 的 94 会被写进
        # state.json，下次启动先摆出来，用户看到的就是「一直 94」，真正读到的
        # 100 反而要等好几秒才替换。现在电量只认当场读取，旧值留在文件里做记录。
        if isinstance(b, int):
            _dbg("cache: battery %s not preloaded (v19)" % b)
        dpis = self._cached.get("dpis")
        if isinstance(dpis, list) and dpis and all(isinstance(v, int) for v in dpis):
            self.dpis = dpis
            idx = self._cached.get("dpi_idx")
            self.dpi_idx = idx if isinstance(idx, int) and 0 <= idx < len(dpis) else 0
        # v14：简洁模式开关与圆盘位置跟随缓存，重启后停在用户上次的用法上
        self.compact = bool(self._cached.get("compact"))
        sp = self._cached.get("simple_pos")
        if isinstance(sp, (list, tuple)) and len(sp) == 2 and all(isinstance(v, int) for v in sp):
            self._simple_pos = (int(sp[0]), int(sp[1]))

    def _audit_batt_owner(self):
        """v16：启动时认一遍缓存里那份电量到底属于哪只鼠标

        鼠标静止时两条通道都可能拿不到新帧，采集层按设计「沿用上次读数」。
        换过鼠标的话，这份「上次」其实属于上一只，界面就会顶着旧数字不动 ——
        正是用户反馈的那个现象。这里拿系统当前真实的蓝牙配对地址跟缓存里记
        的归属对一遍，对不上就当场作废，等真实读数回来。
        """
        ck = self._cached_batt_key
        if not ck.startswith("bt:") or self.last_battery is None:
            return
        try:
            cur = self.reader._bt_addr_id(force=True)
        except Exception as e:
            swallow("app.audit_addr", e)
            cur = None
        if not cur:
            return              # 取不到地址就没法判断，宁可留着也别乱丢读数
        if ("bt:%012x" % int(cur)) == ck:
            return
        _dbg("cache: battery belonged to %s, current paired %s"
             % (ck, "bt:%012x" % int(cur)))
        self.last_battery = None
        self._cached_batt_key = ""
        self._cached["batt_key"] = ""
        self._save_cache()
        # v17：作废必须当场反映到界面。只清内存不重画，浮窗会一直顶着上一只
        # 鼠标的读数不动 —— 那正是要修的那个现象。
        try:
            self._render()
        except Exception as e:
            swallow("app.audit_render", e)
        self.feedback = "检测到新鼠标，正在读取电量…"
        self.feedback_until = time.time() + 5.0
        try:
            self.root.after(5080, self._fade_feedback)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        try:
            self.reader.ble_warm()
        except Exception as e:
            swallow("app.audit_warm", e)

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"battery": self.last_battery, "dpis": self.dpis,
                           "batt_key": self._cached_batt_key,
                           "batt_ts": self._last_write_ts or 0.0,
                           "dpi_idx": self.dpi_idx,
                           "compact": bool(self.compact),
                           "simple_pos": list(self._simple_pos) if self._simple_pos else None},
                          f)
        except Exception as e:
            swallow("app._save_cache", e)

    # ---------- v14：简洁模式（小圆盘） ----------
    def _ensure_simple(self):
        """懒创建圆盘；位置优先用缓存，没有就落在右下角"""
        if self.simple is not None:
            return self.simple
        from .simple import SimpleOverlay
        self.simple = SimpleOverlay(self.root, on_refresh=self.refresh,
                                    on_expand=self._expand_from_simple,
                                    on_move=self._save_simple_pos,
                                    on_menu=self._simple_menu)
        if self._simple_pos:
            try:
                self.simple.move(self._simple_pos[0], self._simple_pos[1])
            except Exception as e:
                swallow("app.simple.move", e)
        return self.simple

    def _save_simple_pos(self, pos):
        try:
            self._simple_pos = (int(pos[0]), int(pos[1]))
        except Exception:
            return
        self._save_cache()

    def _set_compact(self, on, save=True):
        """切换简洁模式：圆盘在、主面板退场（反之亦然）"""
        on = bool(on)
        if on == self.compact and self.simple is not None:
            if on:
                self.simple.show()
            return
        if on:
            self._ensure_simple()
            self.compact = True
            # 主面板让位：withdraw 之后滚轮钩子的命中矩形清空，避免圆盘上
            # 的滚轮误触发主面板的 DPI 调节
            self.hidden = True
            try:
                self.root.withdraw()
            except Exception as e:
                swallow("app.compact.withdraw", e)
            self._rect = None
            try:
                self.simple.show()
            except Exception as e:
                swallow("app.compact.show", e)
            self._sync_simple()
            self._update_tray()
        else:
            self.compact = False
            if self.simple is not None:
                try:
                    self.simple.hide()
                except Exception as e:
                    swallow("app.compact.hide", e)
            self.show_window()
            self._render()
            self._update_tray()
        if save:
            self._save_cache()

    def _expand_from_simple(self):
        """圆盘双击 / 右键「展开完整面板」"""
        self._set_compact(False)

    def _simple_menu(self, x=None, y=None):
        """简洁模式右键：直接复用完整模式的同一套菜单（同源同风格）"""
        if x is None or y is None:
            try:
                x = self.simple.win.winfo_pointerx()
                y = self.simple.win.winfo_pointery()
            except Exception:
                x, y = self.tray.cursor_pos() if self.tray else (0, 0)
        self._show_menu(int(x), int(y))

    def _sync_simple(self, pct=None, online=None):
        """把状态喂给圆盘

        数值走的是与完整面板同一套 _eff_pct() / _eff_online()，不另算一份，
        所以两个界面的电量天然一致；圆盘只是换了个画法。
        """
        if self.simple is None or not self.compact:
            return
        if pct is None:
            pct = self._eff_pct()
        if online is None:
            online = self._eff_online()
        dpi = self.dpis[self.dpi_idx] if (self.dpis and self.dpi_idx is not None) else None
        try:
            self.simple.set(pct=pct, dpi=dpi, link=self.link, online=online,
                            pending=bool(getattr(self, "bt_pending", False)),
                            still=bool(getattr(self, "_still", False)),
                            charging=(self.link == "wired"))
        except Exception as e:
            swallow("app.simple.set", e)

    def _compact_visible(self):
        if self.simple is None:
            return False
        try:
            return bool(self.simple.win.winfo_viewable())
        except Exception:
            return False

    def _pump(self):
        if self.tray:
            for ev in self.tray.poll_events():
                if isinstance(ev, (tuple, list)):
                    kind = ev[0]
                    x = ev[1] if len(ev) > 1 else None
                    y = ev[2] if len(ev) > 2 else None
                else:
                    kind, x, y = ev, None, None
                if kind == "left":
                    self.toggle_window()
                elif kind == "right":
                    self._show_menu(x, y)
        try:
            self.root.after(60, self._pump)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _eff_pct(self):
        """有效电量：只认设备/系统真的报上来的数

        v18：去掉「有线固定满格」。有线供电时 F7 回包的电量字段实测是 0，
        那不代表 100%，只代表设备没报电量；写死 100% 会让人以为电池是满的。
        没有有效读数就显示「--」，尾行说明当前是「有线供电」。
        """
        if self.link == "wired":
            # v21：有线态不显示百分比，数字区改成「充电中」+ 呼吸动画
            return None
        b = self.last_battery
        return b if (isinstance(b, int) and 0 < b <= 100) else None

    def _eff_online(self):
        return self.online or self.link == "wired"

    def _link_text(self):
        if self.link == "wired":
            return "有线连接"
        if self.link == "2.4g":
            return "2.4G 已连接"
        if self.link == "bt":
            return "蓝牙已连接"
        return "已连接"

    def _mode_text(self):
        """右上角（原「在线」位）显示的连接模式，三种模式统一成「X 已连接」"""
        if self.link == "wired":
            return "有线已连接"
        if self.link == "2.4g":
            return "2.4G 已连接"
        if self.link == "bt":
            return "蓝牙已连接"
        return "已连接"

    def _sync_dev_name(self):
        """跟随当前鼠标切换界面上的型号名（VK M5 / A955）

        PID 认出来的型号才算数；认不出（陌生固件、切换过渡期）就保持上一次
        认出的名字，不要因为一次读不到 PID 就把标题打回默认值乱跳。
        """
        nm = devices.name_for_pid(self.link_pid)
        if not nm:
            for _p in (self.link_pids or []):
                nm = devices.name_for_pid(_p)
                if nm:
                    break
        if not nm or nm == self.dev_name:
            return
        self.dev_name = nm
        # 换了鼠标，上一只的档位表立刻作废：不清的话，A955 会先顶着 VK M5 的
        # 6 档显示一秒（缓存里的旧表），看着像识别错了设备。
        self.dpis = []
        self.dpi_idx = None
        for setter in (lambda: self.root.title(nm),
                       lambda: self.name_lbl.config(text=nm)):
            try:
                setter()
            except Exception:
                pass  # 可忽略：窗口尚未建好/已销毁
        self._update_tray()

    def _update_tray(self):
        if not self.tray:
            return
        pct = self._eff_pct()
        charging = (self.link == "wired")
        self.tray.set_icon(pct, self.link, charging)
        dpi = self.dpis[self.dpi_idx] if (self.dpis and self.dpi_idx is not None) else None
        tip = self.dev_name + " · " + ("充电中" if charging
                                       else (f"{pct}%" if pct is not None else "--"))
        if dpi:
            tip += f" · {dpi} DPI"
        tip += " · " + ("有线连接" if self.link == "wired" else
                        ("2.4G" if self.link == "2.4g" else
                         ("蓝牙" if self.link == "bt" else "未知连接")))
        if not self._eff_online():
            if self._still:
                # v13.8：静止沿用是明确结论（通道在、鼠标没动），别再缀「识别中」，
                # 否则重启后托盘一直写着识别中，用户以为没连上。
                tip += " · 静止"
            elif (warm_text_ok(time.time(), getattr(self, "_boot_ts", 0.0),
                               getattr(self, "_warm_done", False))
                    or getattr(self, "_ch", True)):
                tip += " · 识别中"
            else:
                tip += " · 未响应"
        if self.link_pid:
            tip += f" · PID {self.link_pid:04X}"
        self.tray.set_tip(tip)

    def hide_to_tray(self):
        self.hidden = True
        if self.compact and self.simple is not None:
            self.simple.hide()
        self.root.withdraw()
        self._update_tray()

    def show_window(self):
        self.hidden = False
        if self.compact:
            # 简洁模式下「显示」= 让圆盘回来，主面板继续留着不弹
            self.root.withdraw()
            if self.simple is not None:
                self.simple.show()
            self._update_tray()
            return
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def toggle_window(self):
        if self.compact:
            if self._compact_visible():
                self.hidden = True
                self.simple.hide()
            else:
                self.hidden = False
                self.simple.show()
            self._update_tray()
            return
        if self.hidden or not self.root.winfo_viewable():
            self.show_window()
        else:
            self.hide_to_tray()

    # ---------- 右键菜单（原生，点外部即关闭） ----------
    def _toast(self, msg, secs=2.5):
        self.feedback = msg
        self.feedback_until = time.time() + secs
        self._render()
        # 到点补一次重绘让提示准点消失。只用一次性 after，不常驻定时器，空闲零开销。
        try:
            self.root.after(int(secs * 1000) + 80, self._fade_feedback)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _fade_feedback(self):
        if self.feedback and time.time() >= self.feedback_until:
            self.feedback = ""
            self._render()

    def open_hub(self):
        """打开 VK Hub 官网：驱动下载、DPI 档位规则都以官网为准

        浏览器启动（尤其 Chrome 冷启动）会阻塞数秒，若在 Tk 主线程里做，
        主线程无法处理消息，刚关掉的右键菜单就会留在屏幕上不消失。
        因此这里丢到后台线程，UI 只负责提示。
        """
        def _work():
            for opener in (lambda: webbrowser.open(HUB_URL),
                           lambda: os.startfile(HUB_URL)):
                try:
                    opener()
                    self.root.after(0, lambda: self._toast("已打开 VK Hub 官网"))
                    return
                except Exception as e:
                    swallow("app._open_hub", e)
                    continue
            self.root.after(0, lambda: self._toast(
                "浏览器打开失败，请手动访问 " + HUB_URL, 4.0))
        threading.Thread(target=_work, daemon=True).start()

    def relink(self):
        """强制重新识别连接方式（清掉判活缓存 + 重新枚举/握手）"""
        try:
            _ACT_CACHE.update(ts=0.0, val=None)      # 判活结果作废
        except Exception as e:
            swallow("app.relink.act_cache", e)
        try:
            self.reader.invalidate_link()            # 丢弃锁定的控制口路径
        except Exception as e:
            swallow("app.relink.invalidate_link", e)
        try:
            bt_link_probe()      # 顺手让蓝牙链路状态刷一次（后台约 0.2 秒出结果）
        except Exception as e:
            swallow("app.relink.bt_link", e)
        self.feedback = "正在重新识别连接…"
        self.feedback_until = time.time() + 2.0
        self._render()

        def _kick_soon():
            # v13.6：等 0.25 秒再重采 —— 否则这一轮可能正好读到刷新前的链路
            # 旧值，用户点「重新识别」拿到的还是上一秒的结论，得再点一次。
            time.sleep(0.25)
            self._kick.set()

        threading.Thread(target=_kick_soon, daemon=True).start()

    def _dpi_menu_items(self):
        """右键菜单里的档位区：**纯显示，点不动**

        旧版本把每个数值做成可点项，动作还是「改当前档位的 DPI 值」，鼠标
        一碰就把当前档位改掉了 —— 这就是用户说的「当前档位也参与了档位调节」。
        现在这一段统一 enabled=False：只标出各基础档位和当前档，点它不生效；
        真正调档走浮窗点击 / Ctrl+滚轮。
        """
        items = []
        vals = list(self.dpis or [])
        if not vals:
            items.append({"label": "未读到档位（点浮窗刷新）", "enabled": False})
            return items
        cur = self.dpi_idx
        shown = dpi_tiers(vals)
        for i in range(shown):
            items.append({"label": f"{'● ' if i == cur else '· '}{i + 1} 档 · {vals[i]}",
                          "enabled": False})
        if cur is not None and cur >= shown:
            # 设备停在超出可见档的档位时如实标出来，但不进循环
            items.append({"label": f"● 设备当前第 {cur + 1} 档 · {vals[cur]}",
                          "enabled": False})
        items.append({"label": "档位仅显示 · 浮窗点击 / Ctrl+滚轮 切换",
                      "enabled": False})
        return items

    def _link_menu_items(self):
        """连接方式区：**纯显示，全部不可点**

        用户要求：连接方式不允许在菜单里手动改，必须按实际连接自动识别。
        因此这里只负责把「自动识别 + 当前实际通道」如实标出来，不带任何
        action；想让它重新判一遍，用「立即刷新」或菜单里的「重新识别连接」。
        """
        cur = self.link
        items = [{"label": "自动识别 · 按实际连接",
                  "enabled": False}]
        for mode, label in (("wired", "有线连接"), ("2.4g", "2.4G 无线"),
                            ("bt", "蓝牙")):
            mark = "● " if cur == mode else "○ "
            items.append({"label": mark + label + ("（当前）" if cur == mode else ""),
                          "enabled": False})
        if cur == "unknown":
            items.append({"label": "暂未识别到连接通道", "enabled": False})
        if self.link_pid:
            items.append({"label": f"当前 PID {self.link_pid:04X}",
                          "enabled": False})
        return items

    def _show_menu(self, x=None, y=None):
        if x is None or y is None:
            if self.tray:
                x, y = self.tray.cursor_pos()
            else:
                x, y = self.root.winfo_pointerx(), self.root.winfo_pointery()
        # 简洁模式下主面板是 withdraw 的，可见性以圆盘为准，否则菜单会错显「显示浮窗」
        if self.compact:
            shown = self._compact_visible()
        else:
            shown = not (self.hidden or not self.root.winfo_viewable())
        items = [
            {"label": "立即刷新", "action": self.refresh},
            {"label": "重新识别连接", "action": self.relink},
            # v14：简洁模式开关。延后一跳再切，让原生菜单先关干净
            {"label": ("切回完整面板" if self.compact else "切到简洁模式"),
             "action": lambda: self.root.after(120, self._set_compact,
                                              not self.compact)},
            {"label": "切到 DPI 页" if self.page == PAGE_BATT else "切到电量页",
             "action": self.toggle_page},
            {"label": "隐藏到任务栏" if shown else "显示浮窗",
             "action": self.toggle_window},
            {"sep": True},
            {"label": "DPI 档位", "header": True},
        ]
        items += self._dpi_menu_items()
        items += [{"sep": True}, {"label": "连接方式", "header": True}]
        items += self._link_menu_items()
        items += [{"sep": True},
                  # 打开浏览器会短暂阻塞主线程并抢前台：直接执行会让原生菜单
                  # 停在屏幕上不消失，延后一跳再开，菜单先关干净
                  {"label": "打开 VK Hub 官网",
                   "action": lambda: self.root.after(200, self.open_hub)},
                  {"label": "退出", "action": self.quit}]
        spec = []
        for it in items:
            if it.get("header"):
                spec.append({"label": it.get("label", ""), "enabled": False})
            else:
                spec.append(it)
        try:
            # 菜单是 Tk 自绘的 overrideredirect Toplevel（menu.TkMenu）：浮窗缩到
            # 托盘（root 已隐藏）时它会拿不到前台焦点，被「失焦即关」逻辑立刻关掉，
            # 表现为右键无菜单，所以下面先把焦点抢回浮窗。
            from .menu import popup_menu_tk
            # 简洁模式下 root 是 withdraw 的，抢它的焦点没有意义（窗口不可见
            # 就抢不到前台），必须抢圆盘文字层的焦点，否则菜单刚弹就被判失焦关掉。
            owner = None
            if self.compact and self.simple is not None:
                owner = self.simple.win
            anchor = owner if owner is not None else self.root
            # 弹菜单前先让宿主窗口自己拿到输入焦点：否则菜单窗口无法被激活，
            # 用户弹出的**第一下点击**只会被系统拿去激活菜单（点了像没反应）。
            try:
                anchor.lift()
                anchor.focus_force()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
            self._menu_open = True
            # 菜单在屏幕期间别让圆盘的定时刷新把两层提到菜单上面去
            if self.simple is not None:
                try:
                    self.simple.set_topmost_hold(True)
                except Exception:
                    pass  # 可忽略：幂等清理/UI 收尾
            popup_menu_tk(self.root, int(x), int(y), spec,
                          on_close=self._menu_closed, owner=owner)
        except Exception as ex:
            self.feedback = f"菜单异常: {str(ex)[:18]}"
            self.feedback_until = time.time() + 2.5
            self._render()

    def _menu_closed(self):
        self._menu_open = False
        if self.simple is not None:
            try:
                self.simple.set_topmost_hold(False)
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾

    def _popup(self, e):
        self._show_menu(e.x_root, e.y_root)

    # ---------- 翻页 / 交互 ----------
    def show_page(self, page):
        page = PAGE_DPI if page else PAGE_BATT
        if page == self.page:
            return
        self.page = page
        (self.page_dpi if page == PAGE_DPI else self.page_batt).lift()
        self._draw_dots()
        self._render()

    def toggle_page(self):
        self.show_page(PAGE_DPI if self.page == PAGE_BATT else PAGE_BATT)

    def _wheel(self, e):
        # 钩子已经把这次滚动收走了（钩子回调一定先于进程内 Tk 事件发生），
        # Tk 这份直接让位；否则同一次滚动会被处理两遍，一格跳两页。
        h = self.hook
        if self._hook_on and h is not None and time.time() - h.last_ts < 0.3:
            return "break"
        self._wheel_by_delta(e.delta, bool(e.state & 0x0004))
        return "break"

    def _drag_start(self, e):
        self._moved = False
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        nx, ny = e.x_root - self._dx, e.y_root - self._dy
        if abs(nx - self.root.winfo_x()) > 2 or abs(ny - self.root.winfo_y()) > 2:
            self._moved = True
        self.root.geometry(f"+{nx}+{ny}")

    def _click_release(self, e):
        if self.page == PAGE_DPI and not self._moved:
            self._next_dpi()

    def _next_dpi(self, e=None):
        if not self.dpis or self.dpi_idx is None:
            return "break"
        # 只在设备真实档位内循环；尾部与当前档重复的档由 dpi_tiers 截掉
        n = dpi_tiers(self.dpis)
        cur = min(self.dpi_idx, n - 1)
        self.set_dpi((cur + 1) % n)
        return "break"

    def set_dpi(self, index):
        """切换档位（只认 5 档范围，不拿「在线位」当门槛）

        两点关键改动：
        1) 旧版本有 `有线 and not online -> 不能调` 的闸门，而这个 online 位
           来自设备回包、并不准（鼠标在用也报 0），于是调档被自己拦住，用户
           看到的就是「显示休眠、无法改动」。现在只有蓝牙（硬件不响应私有帧）
           才拦，其余通道一律真写一次再说。
        2) 读表+写入+回读要走 HID 往返，可能 1~3 秒；放主线程会让浮窗假死，
           手感就是「点了没反应」。改后台线程执行，完成后回主线程刷新。
        """
        if not self.dpis or self.dpi_idx is None:
            return
        n = dpi_tiers(self.dpis)
        if index < 0 or index >= n or index == self.dpi_idx:
            return
        if self._dpi_busy:
            self._toast("正在切换，请稍候…", 1.2)
            return
        self._dpi_busy = True
        want = self.dpis[index]
        self._toast("正在切换档位…", 6)
        def _work():
            ok, msg = False, ""
            try:
                ok, msg = self.reader.dpi_set(index)
            except Exception as ex:
                ok, msg = False, "异常 " + str(ex)[:16]
            try:
                self.root.after(0, lambda: self._dpi_done(index, want, ok, msg))
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        threading.Thread(target=_work, daemon=True).start()

    def _dpi_done(self, index, want, ok, msg):
        self._dpi_busy = False
        if ok:
            self.dpi_idx = index
            self.reader.invalidate_dpi()     # 让后台按真机状态重读复核
            self._kick.set()
            self._toast(f"已切到第 {index + 1} 档 · {want} DPI")
        else:
            self._toast("切换失败：" + (msg or "未知原因"), 3.5)

    # ---------- 数据（后台线程采集，主线程只渲染，避免界面卡死） ----------
    def _start_worker(self):
        # 蓝牙 GATT 首次建连较慢（约 10s），启动就预热一次，别等采集线程撞上
        self.reader.on_ble_done = self._kick.set
        self.reader.ble_warm()
        t = threading.Thread(target=self._worker_loop, daemon=True)
        t.start()

    def _collect_round(self):
        """跑一轮采集，返回 (data, sig, static_sig)。

        只负责「读一次」，不判断该等多久、也不碰共享槽位；异常在这里咽掉并
        记进 data["err"]，好让主循环永远活着（采集线程挂掉 = 界面永远不动）。
        """
        data = {"ok": False, "err": "", "battery": None, "online": False,
                "resp": False, "type": None, "dpi_idx": None, "dpis": None,
                "link": "unknown", "pid": None, "pids": [], "source": "",
                "batt_note": "", "stale": False, "stale_ts": 0.0,
                "dev_changed": False, "batt_key": ""}
        try:
            pids = []
            for dd in _hid().enumerate(VID, 0):
                if dd.get("usage_page") in VENDOR_PAGES:
                    p = dd.get("product_id")
                    if p and p not in pids:
                        pids.append(p)
            data["pids"] = pids
        except Exception as e:
            swallow("app._collect_round.pids", e)
        try:
            got = self.reader.read_all()
            data.update(ok=bool(got.get("resp")), battery=got["battery"],
                        online=got["online"], resp=got.get("resp", False),
                        type=got["type"],
                        dpi_idx=got["dpi_idx"], dpis=got["dpis"],
                        link=got["mode"], pid=got["pid"], source=got["source"],
                        batt_note=got.get("batt_note", ""),
                        stale=bool(got.get("stale")),
                        stale_ts=got.get("stale_ts", 0.0),
                        dev_changed=bool(got.get("dev_changed")),
                        batt_key=got.get("batt_key") or "",
                        still=bool(got.get("still")),
                        bt_unk=bool(got.get("bt_unk")),
                        ch=bool(got.get("ch", True)),
                        bt_pending=bool(got.get("bt_pending")),
                        bt_check=bool(got.get("bt_check")))
            sig = (got["mode"], got["source"])
            static_sig = got.get("static") or (got["mode"], got["pid"])
        except Exception as ex:
            data["err"] = str(ex)
            sig = ("unknown", "")
            static_sig = probe_link()[:2]
            try:
                self.reader._drop_dev()   # 只丢句柄，别 join 蓝牙线程（会卡 6s）
            except Exception as e:
                swallow("app._collect_round.drop_dev", e)
        return data, sig, static_sig

    def _next_delay(self, data):
        """按这一轮的结论决定下次多久后再采。"""
        if not (data["ok"] or data["online"] or data.get("bt_pending")) \
                and getattr(self.reader, "_cold", False):
            # v13.6：刚打开程序、还一帧都没拿到时别按离线档慢慢等。冷启动
            # 多半发生在用户刚动过鼠标（点托盘、看浮窗）那一刻，一秒多一次
            # 重试才能在第一下操作里把读数抓上来，而不是先挂几秒「识别中」。
            return 1.2
        if data["ok"] or data["online"]:
            if data["source"] == "gatt":
                # 蓝牙电量以分钟级变化：读数不变就指数退避（8→12s 封顶），
                # 一旦读数变了立刻回到 8s，兼顾低占用与灵敏度
                cur_b = data.get("battery")
                if cur_b is not None and cur_b == getattr(self, "_bt_prev_batt", None):
                    self._bt_idle = min(getattr(self, "_bt_idle", 0) + 1, 4)
                else:
                    self._bt_idle = 0
                self._bt_prev_batt = cur_b
                return min(BT_POLL_BASE * (2 ** self._bt_idle), BT_POLL_MAX)
            return POLL_ONLINE
        if data.get("still"):
            # v13.8：静止沿用期间别按离线档 3 秒等。用户抱怨「切模式慢」多半是
            # 停在这里：鼠标一停就进入静止，轮间隔 3 秒 + 快探 1.5 秒一次、要连续
            # 两次不符才打断，拨档到蓝牙要 3 秒以上才认出来。静止时 F7 帧本来就读
            # 得到（不触发深探），单轮只有 0.16 秒，2 秒一轮的占用可以接受，
            # 换来切换 2 秒左右跟上。
            return STILL_POLL
        if data.get("bt_pending"):
            # 正在等蓝牙 GATT 建连：这时候界面写的是「蓝牙连接中」，
            # 必须勤刷几次，不能按离线那档 3 秒慢慢等
            return BT_PENDING_POLL
        # v13.9：F7 连帧都读不到（拨档切到蓝牙的过渡期 / 接收器被拔）。这段
        # 窗口里界面还停在「未响应」，按离线档 3 秒一轮会让蓝牙判定多磨两轮，
        # 换成 1.2 秒一轮；无帧持续太久（真离线、鼠标关机）就放回离线档，
        # 免得一直高频空转。
        _nf = getattr(self.reader, "_noframe_n", 0)
        if 1 <= _nf <= 6:
            return NOFRAME_FAST_POLL
        return POLL_OFFLINE

    def _wait_for_change(self, deadline, static_sig, cur_is_usb):
        """等到 deadline；期间发现通道变了就提前返回（三模免刷新切换的关键）。

        设备树签名不变时，实际连接也可能变（拔线、鼠标切 2.4G/蓝牙，节点都还
        挂着），但 read_all 每轮都会重新握手判活，所以这里不再定时强制重判，
        只负责「签名一变就立刻打断等待」快速响应插拔。补一步快探：接收器一直
        插着、只把鼠标拨到 2.4G 时签名根本不变，光等就成了「拨完开关好几秒
        （蓝牙下最长 12 秒）才认出来」。等待期间顺手对 USB 通道做一次 F7 快探
        （几十毫秒、不碰 DPI），结果与当前通道连续两次不符就立刻重采，模式切换
        2~3 秒内跟上。
        """
        probe_due = time.time() + LINK_PROBE
        probe_bad = 0
        # v13.6：蓝牙链路快查。拨档切到蓝牙后，系统得先把 BLE HID 节点枚举出来
        # 设备树签名才会变，那段空窗里旧逻辑只能干等采集退避（蓝牙档最长 12 秒）
        # 走完才重判。这里每 BT_LINK_WATCH 秒读一次链路缓存（bt_link_probe 自带
        # 缓存 + 后台刷新，不阻塞本循环），0↔1 一变立刻重采。
        bt_prev = None
        bt_due = time.time() + BT_LINK_WATCH
        while not self._stop.is_set():
            left = deadline - time.time()
            if left <= 0:
                break
            if self._kick.wait(min(LINK_WATCH, max(left, 0.05))):
                self._kick.clear()
                break
            _nowb = time.time()
            if _nowb >= bt_due:
                bt_due = _nowb + BT_LINK_WATCH
                try:
                    _blk = bt_link_probe()
                    _blv = _blk[0] if _blk else None
                except Exception:
                    _blv = None
                if _blv is not None:
                    if bt_prev is not None and _blv != bt_prev:
                        _dbg("link probe bt: %s -> %s, recollect"
                             % (bt_prev, _blv))
                        break
                    bt_prev = _blv
            # 轻量巡检：只比设备树里静态通道有没有变（不跟鼠标通信）
            try:
                cur = probe_link()[:2]
            except Exception:
                cur = static_sig
            if cur != static_sig:
                try:
                    self.reader.invalidate_link()
                except Exception as e:
                    swallow("app._wait_for_change.invalidate_link", e)
                break
            now_p = time.time()
            if now_p < probe_due:
                continue
            probe_due = now_p + LINK_PROBE
            p_pb = None
            try:
                p_pb = self.reader._path
                st_p = _handshake(p_pb) if p_pb else None
                usb_live = bool(st_p and _usb_alive(st_p))
            except Exception:
                usb_live = cur_is_usb     # 快探自身出错：维持现状，不要误跳
            if not p_pb:
                # 连控制口路径都没锁上：这帧不足以说明「USB 不通」，
                # 不能据此打断等待（否则一进程序就漂成蓝牙）
                usb_live = cur_is_usb
            elif usb_live:
                # v11.7：已认定这条是空置接收器（F7 假在线、私有帧读不到）时，
                # 快探别再被同一帧旧缓存骗成「USB 在线」，否则采集线程会被无谓
                # 地反复打断重采（每 6 秒一次）。
                try:
                    _fts = getattr(self.reader, "_fake_ts", 0.0)
                    if _fts and time.time() - _fts < FAKE_TTL:
                        usb_live = False
                except Exception as e:
                    swallow("app._wait_for_change.fake_ts", e)
            if not usb_live and cur_is_usb:
                # v11.6：F7 状态帧的 can_read / online 位在 2.4G 下会假死报 0
                # （鼠标明明在线）。快探只有这一帧，仅凭它判「USB 不通」并打断
                # 等待，界面就会周期性跳到蓝牙。所以判死必须再用私有帧复核一次，
                # 能通就按仍在 USB 算。
                try:
                    usb_live = bool(deep_probe(p_pb, timeout=LINK_PROBE_DEEP))
                except Exception:
                    usb_live = True
                if usb_live:
                    try:
                        self.reader.note_f7_soft()
                    except Exception as e:
                        swallow("app._wait_for_change.note_f7_soft", e)
            _dbg("link probe usb_live=%s cur=%s bad=%d"
                 % (usb_live, cur_is_usb, probe_bad))
            if usb_live == cur_is_usb:
                probe_bad = 0
                continue
            probe_bad += 1
            if probe_bad < LINK_PROBE_MISS:
                continue
            if not usb_live:
                # 已经过私有帧复核、确认不通，才算真切走。抬计数还要满足 grace：
                # 距上次真实在线不足 USB_BT_GRACE 时一律不抬，免得快探变成绕过
                # 门禁的后门。
                try:
                    rd = self.reader
                    idle = (time.time() - rd._usb_ok_ts
                            if rd._usb_ok_ts else 1e9)
                    if idle >= USB_BT_GRACE:
                        rd._usb_dead_ts = time.time()
                        rd._usb_dead_n = max(
                            getattr(rd, "_usb_dead_n", 0) + 1, DEAD_CONFIRM)
                        rd._usb_miss = max(rd._usb_miss, USB_MISS_BT)
                except Exception as e:
                    swallow("app._wait_for_change.usb_dead", e)
            break

    def _worker_loop(self):
        """后台循环：连接方式 + 电量 + DPI，结果写入共享槽位

        低占用设计：在线时 30s 采一次、休眠 6s 一次、蓝牙电量缓存 15s、
        档位表 90s 才重读一次；两次采集之间的等待被切成 LINK_WATCH 小段，
        每段只做一次设备枚举（不跟鼠标通信）——一旦发现 PID 变了（插拔线、
        拨到 2.4G 或蓝牙），立刻打断等待、马上重采，实现三模免刷新切换。
        """
        last_sig = None
        while not self._stop.is_set():
            data, sig, static_sig = self._collect_round()
            if last_sig is not None and sig != last_sig:
                # 模式变了：旧模式的档位表不能带过来
                self.reader._dpi_vals = None
                self.reader._dpi_ts = 0.0
            last_sig = sig
            with self._data_lock:
                self._data = data
            delay = self._next_delay(data)
            self._wait_for_change(time.time() + delay, static_sig,
                                  sig[1] == "usb")


    def _tick(self):
        """主线程 150ms 消费一次采集结果（非阻塞）"""
        with self._data_lock:
            data = self._data
        if data is not None and data is not self._last_data:
            self._last_data = data
            self._apply_data(data)
        try:
            self.root.after(150, self._tick)
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾

    def _bt_switch_ok(self):
        """界面能不能把「建连中」写成蓝牙（v11.6 显示层护栏）

        判据与采集层一致：必须已经有「私有帧复核连续读不到」且未过期的硬证据，
        或者设备树里压根没有 USB/2.4G 控制口。只凭 F7 状态位假死报 0 就算出的
        「蓝牙」，一律不写进界面 —— 否则 2.4G 用着用着主界面会突然跳成「蓝牙已连接」。
        """
        try:
            rd = self.reader
            # v13.7：采集层本轮的 F7 帧分类。帧自证在线（含「online=1 但私有帧
            # 不可读」的可疑帧，实测就是 2.4G 静止）时，USB 侧并没有死，界面
            # 不许翻成蓝牙 —— 实机反馈的「用的 2.4G 却显示蓝牙连接」正是这一条。
            _f7_pos = bool(getattr(rd, "_f7_on", False)
                           or getattr(rd, "_f7_suspect", False))
            try:
                _fresh = bool(rd._ble_fresh_ok(time.time()))
            except Exception:
                _fresh = False
            if self.link == "bt":
                # 已经确认切到蓝牙：保持，别在两种写法之间来回跳。
                # v13.7：但「上一轮是蓝牙」不能拿来自己给自己背书 —— 误判一旦
                # 写进 self.link 就会一直锁住，除非 F7 帧的正面证据已经消失。
                if not _f7_pos or _fresh:
                    return True
            # v13.6：蓝牙链路新鲜为 0 = 这台机器现在根本没连蓝牙，界面一律
            # 不许写「蓝牙已连接」。实机反馈的「用的 2.4G 却显示蓝牙连接」
            # 就是漏了这一道。
            try:
                _bl = bt_link_probe()
                if _bl and _bl[1] and _bl[0] == 0:
                    return False
            except Exception as e:
                swallow("app.bt_switch_ok.link", e)
            if _f7_pos and not _fresh:
                return False
            need = 1 if getattr(rd, "_cold", False) else DEAD_CONFIRM
            if getattr(rd, "_usb_dead_n", 0) >= need:
                ts = getattr(rd, "_usb_dead_ts", 0.0)
                if ts and time.time() - ts <= DEAD_TTL:
                    return True
            return not _usb_ctrl_exists()
        except Exception:
            return True

    def _apply_data(self, d):
        self.err = d["err"]
        prev_link = self.link
        # 在线 = 本轮真的跟设备通上话了：USB/2.4G 看回包，蓝牙看 GATT 是否读到
        # 电量（蓝牙不走私有帧，resp 恒为 False，旧判据会把蓝牙一律算成不在线，
        # 于是蓝牙模式的电量永远写不进 last_battery，界面一直停在切换前的读数）
        bt_ok = (d.get("source") == "gatt" and d["battery"] is not None)
        self.online = bool(d.get("resp") or bt_ok or (d["online"] and d["ok"]))
        if self.online:
            self._got_data = True
            # v11.8：在线满 3 秒才算暖机完成（见 __init__ 里的说明）
            _now_ok = time.time()
            if not self._first_ok_ts:
                self._first_ok_ts = _now_ok
            if _now_ok - self._first_ok_ts >= 3.0:
                self._warm_done = True
        self._src = d.get("source") or ""
        self._batt_note = d.get("batt_note") or ""
        # v12.0：静止沿用（鼠标没动、设备不上报，读数来自上一次真实采集）
        self._stale = bool(d.get("stale"))
        self._stale_ts = d.get("stale_ts") or 0.0
        self._still = bool(d.get("still"))
        self._bt_unk = bool(d.get("bt_unk"))
        self._nosig = float(d.get("nosig") or 0.0)
        self._ch = bool(d.get("ch", True))
        # 蓝牙通道正在建连（USB 侧已连续失败 + 设备树确有蓝牙节点），别当成 USB 掉线
        self.bt_pending = bool(d.get("bt_pending"))
        # v13.4：等蓝牙建连超过 BT_CHECK_HINT 就把提示换成可操作的做法
        self._bt_check = bool(d.get("bt_check"))
        # ---- v16：电量归属（换了鼠标就必须重新读，不能顶着上一只的数）----
        _key = d.get("batt_key") or ""
        _swapped = bool(d.get("dev_changed"))
        if (not _swapped and _key and self._cached_batt_key
                and _key != self._cached_batt_key):
            # 刚重启时进程里还没有上一台的记忆，只能靠 state.json 记的归属来认。
            # v17：不再要求「两边都是蓝牙地址」—— 2.4G 侧认不出设备，通道一变
            # （usb <-> bt）就等于换了台设备，旧读数一样不可信。
            _swapped = True
        if _swapped:
            self.last_battery = None
            self._batt_note = ""
            self.feedback = "检测到新鼠标，正在读取电量…"
            self.feedback_until = time.time() + 3.0
            try:
                self.root.after(3080, self._fade_feedback)
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        if _key and self._cached_batt_key != _key:
            self._cached_batt_key = _key
            self._cached["batt_key"] = _key
            self._save_cache()
        auto = d["link"]
        if (self.bt_pending and auto in ("wired", "2.4g", "unknown")
                and self._bt_switch_ok()):
            # 建连中：浮窗/托盘/菜单统一按蓝牙显示，否则浮窗说「蓝牙连接中」、
            # 托盘tooltip 又说「2.4G」，用户看到的就是识别乱跳。
            # 前提是采集层已拿到「私有帧复核不通」的硬证据（见 _bt_switch_ok）。
            auto = "bt"
        self.link_pid = d["pid"]
        self.link_pids = d["pids"]
        self._sync_dev_name()
        # v18：有线不再固定 100%，只显示设备真的报上来的电量
        # 连接方式完全按实际连接（自动识别），不再接受任何手动指定值
        self.link = auto
        if self.link != prev_link:
            # 蓝牙通道要等 GATT 建连（首次约 10s），这期间 USB 侧什么都读不到。
            # 若上一刻还有效读数，把它当过渡期：不清电量、不弹“已切换”，否则
            # 开机/切换瞬间界面会先闪一下“未连接 --”再跳回真实电量。
            transient = bool(not self.online and prev_link != "unknown"
                             and self.last_battery is not None
                             and time.time() - self._last_write_ts < 90)
            # 模式变了：电量/档位表都换成新通道的读数，避免显示上一模式的残留
            self.dpis = []
            self.dpi_idx = None
            if prev_link != "unknown" and not transient:
                self.last_battery = None
                self.feedback = "已自动切换到 " + self._link_text()
                self.feedback_until = time.time() + 3.0
                try:
                    self.root.after(3080, self._fade_feedback)
                except Exception:
                    pass  # 可忽略：幂等清理/UI 收尾
        if d["ok"] or bt_ok:
            # 蓝牙的读数来自 GATT（source=gatt），必须一并写进 last_battery，
            # 否则蓝牙模式永远显示切换前那一次 USB 电量
            # 0/超范围当无效：有线回包的电量字段恒为 0，写进去会把缓存污染成 0%，
            # 下次打开程序先看到「0% / 未响应」，像鼠标坏了。保留上一次有效读数。
            _b = d["battery"]
            if isinstance(_b, int) and 0 < _b <= 100:
                self.last_battery = _b
                self._last_write_ts = time.time()
        if self.last_battery is not None and self.last_battery != self._cached.get("battery"):
            self._cached["battery"] = self.last_battery
            self._save_cache()
        if d["dpis"]:
            if (d["dpi_idx"], d["dpis"]) != (self.dpi_idx, self.dpis):
                self.dpi_idx, self.dpis = d["dpi_idx"], d["dpis"]
                self._save_cache()
        self._render()
        self._update_tray()

    def refresh(self):
        """立即触发一次后台采集"""
        # 刷新必须失效档位缓存，否则「点刷新」读的还是那份 90 秒前的旧表
        try:
            self.reader.invalidate_dpi()
        except Exception as e:
            swallow("app.refresh.invalidate_dpi", e)
        # 连接方式也一并重判：用户点刷新时最想确认的就是「到底连的哪条通道」，
        # 只重读档位不动判活，界面上的连接方式就会一直不变。
        try:
            self.reader.invalidate_link()
        except Exception as e:
            swallow("app.refresh.invalidate_link", e)
        # v19：电量缓存也一起作废，点一次刷新就必须重新读，不能继续顶旧值
        try:
            self.reader.invalidate_battery()
        except Exception as e:
            swallow("app.refresh.invalidate_battery", e)
        self._kick.set()
        self.feedback = "正在刷新…"
        self.feedback_until = time.time() + 1.5
        self._render()

    def _draw_battery(self, pct):
        c = self.batt_cv
        c.delete("all")
        if self.link == "wired":
            # v21：有线充电态 —— 电池框稍微放大，内部改成细长闪电
            self._draw_charge_battery()
            return
        col = battery_color(pct, True)
        x0, y0, x1, y1 = 1, 5, 46, 23
        c.create_rectangle(x0, y0, x1, y1, outline=col, width=2)
        mid = (y0 + y1) // 2
        c.create_rectangle(x1 + 1, mid - 4, x1 + 5, mid + 4, fill=col, outline=col)
        cells = battery_cells(pct)
        gap = 2
        ix0, ix1 = x0 + 4, x1 - 3
        cw = (ix1 - ix0 - gap * 4) / 5.0
        for i in range(5):
            ax = ix0 + i * (cw + gap)
            c.create_rectangle(ax, y0 + 4, ax + cw, y1 - 4,
                               fill=(col if i < cells else CELL_OFF), outline="")

    def _draw_charge_battery(self):
        """v21：有线充电态 —— 50×22 电池框 + 细长闪电，替代 5 格电量条"""
        c = self.batt_cv
        col = ACCENT
        x0, y0, x1, y1 = 1, 2, 50, 24
        c.create_rectangle(x0, y0, x1, y1, outline=col, width=2)
        mid = (y0 + y1) // 2
        c.create_rectangle(x1 + 1, mid - 4, x1 + 5, mid + 4,
                           fill=col, outline=col)
        ix0, iy0, ix1, iy1 = x0 + 5, y0 + 4, x1 - 5, y1 - 4
        iw, ih = ix1 - ix0, iy1 - iy0
        pts = []
        for px, py in _CHARGE_BOLT:
            pts.extend([ix0 + px * iw, iy0 + py * ih])
        c.create_polygon(pts, fill=col, outline="")

    def _charge_color(self):
        """v21：1.2 秒一循环的呼吸色（ACCENT 亮度 72%~100%）"""
        k = 0.86 + 0.14 * math.cos(2.0 * math.pi * self._charge_phase / 1.2)
        r = max(0, min(255, int(0x33 * k)))
        g = max(0, min(255, int(0xD1 * k)))
        b = max(0, min(255, int(0x7A * k)))
        return "#%02x%02x%02x" % (r, g, b)

    def _charge_tick(self):
        self._charge_after = None
        if self.link != "wired":
            return
        self._charge_phase = (self._charge_phase + 0.08) % 1.2
        try:
            self.pct_lbl.configure(fg=self._charge_color())
        except Exception:
            return
        try:
            self._charge_after = self.root.after(80, self._charge_tick)
        except Exception:
            self._charge_after = None

    def _start_charge_anim(self):
        if self._charge_after is None and self.link == "wired":
            self._charge_phase = 0.0
            self._charge_tick()

    def _stop_charge_anim(self):
        if self._charge_after is not None:
            try:
                self.root.after_cancel(self._charge_after)
            except Exception:
                pass
            self._charge_after = None

    def _render(self):
        pct = self._eff_pct()
        online = self._eff_online()
        if self.link == "wired":
            # v21：有线态数字区显示「充电中」，并做 1.2 秒呼吸
            self.pct_lbl.configure(text="充电中", fg=self._charge_color())
            self._start_charge_anim()
        else:
            self._stop_charge_anim()
            if pct is None:
                self.pct_lbl.configure(text="--%", fg=DIM)
            else:
                self.pct_lbl.configure(text=f"{pct}%",
                                       fg=battery_color(pct, True))
        self._draw_battery(pct)

        if self.dpis and self.dpi_idx is not None:
            self.dpi_lbl.configure(text=f"{self.dpis[self.dpi_idx]}")
            shown = dpi_tiers(self.dpis)
            disp = min(self.dpi_idx + 1, shown)
            self.dpi_sub.configure(text=f"第 {disp}/{shown} 档 · 点击或 Ctrl+滚轮调档")
        else:
            self.dpi_lbl.configure(text="--")
            msg = "点刷新按钮或浮窗重读档位"
            self.dpi_sub.configure(text=msg)

        if self.feedback and time.time() < self.feedback_until:
            self.dpi_sub.configure(text=self.feedback, fg=ACCENT)
        else:
            self.dpi_sub.configure(fg=DIM)

        if online:
            # 连接模式统一挪到右上角（原「在线」位），底部不再重复写模式，
            # 只留电量口径说明与更新时间 —— 底部那行原本被「2.4G 已连接·
            # 已对齐 2.4G/蓝牙·时间」挤到超宽，末尾的「更新」被窗口裁掉。
            self.state_lbl.configure(text=self._mode_text(), fg=ACCENT)
            if self.link == "wired":
                _w = "有线供电" if pct is None else "设备上报"
                self.tip_lbl.configure(
                    text=f"{_w} · {time.strftime('%H:%M:%S')} 更新", fg=DIM)
            else:
                extra = ""
                if self._batt_note == "cross":
                    # 两通道做过校准（口径/粒度不同，统一取保守值）
                    extra = "已对齐 2.4G/蓝牙 · "
                elif self._src == "gatt":
                    extra = ("蓝牙缓存 · " if self._batt_note == "cache"
                             else "蓝牙直读 · ")
                elif self._src == "usb":
                    # v18：2.4G / 有线的电量来自 HID 特征报告 F7（设备当场回报）
                    extra = "HID 直读 · "
                if self._stale and self._stale_ts:
                    # v12.0：静止沿用。写「读数」而不是「更新」——这一轮的数值
                    # 来自上一次真实采集，鼠标没动时设备本来就不上报。
                    _t = time.strftime("%H:%M", time.localtime(self._stale_ts))
                    self.tip_lbl.configure(
                        text=f"{extra}{_t} 读数 · 鼠标静止时沿用", fg=DIM)
                else:
                    self.tip_lbl.configure(
                        text=f"{extra}{time.strftime('%H:%M:%S')} 更新", fg=DIM)
        elif self.err and not self._ch:
            self.state_lbl.configure(text="未连接", fg=BAD)
            self.tip_lbl.configure(text=self.err[:24], fg=DIM)
        else:
            # 蓝牙节点在设备树里但电量还没读到：属于首次建连，别显示成“休眠”。
            # 判据用 bt_pending（USB 侧已连续失败 + 确有蓝牙节点），而不是“设备树
            # 里存在蓝牙 PID” —— 配对过的蓝牙节点常年挂着，拿它当判据会让 2.4G
            # 偶发丢帧直接显示成「蓝牙连接中」。
            if self.link == "bt" or getattr(self, "bt_pending", False):
                self.state_lbl.configure(text="蓝牙", fg=WARN)
                # 实测：蓝牙栈把 HID 服务重新挂起来的过程并不稳定，首次读数
                # 常在 15~25 秒，个别时候更久；而且链路没真连上时 GATT 读会
                # 一直等到系统超时（实测单次 7.8 秒）。旧版直接写「约 10 秒」
                # 属于承诺了做不到的事，用户掐表就发现对不上。现在按实测状态
                # 说人话，不写秒数，也不让用户以为卡住要重启软件。
                if ble_link_status() == 1:
                    self.tip_lbl.configure(
                        text="蓝牙已连上 · 正在读取电量…", fg=DIM)
                elif getattr(self, "_bt_check", False):
                    # v13.4：等够时间还没有任何读数，别继续只写「耗时较长」——
                    # 事实证明可以一直等下去。换成能动手的三件事。
                    self.tip_lbl.configure(
                        text="蓝牙待连 · 请确认电源、拨档在蓝牙位且已配对", fg=DIM)
                else:
                    self.tip_lbl.configure(
                        text="蓝牙连接中…（耗时较长，会自动重试）", fg=DIM)
            else:
                # 启动宽限：程序刚打开、还没跑完第一轮「真的读到东西」的采集时，
                # 界面写「识别中」而不是「未收到回包」。启动瞬间要么在枚举通道、
                # 要么在等 GATT 建连，报错文案会被当成程序坏了（截屏反馈里就是
                # 「打开三次两次看到未响应」）。宽限只覆盖开头几秒，之后仍然
                # 如实报未响应。
                # v13.8：已经能确认「通道在、鼠标只是没动」时，别再拿启动宽限
                # 盖住结论 —— 冷启动时用户看到的就是一直「识别中…」，体感就是
                # 「重启软件识别特别慢」。静止沿用本身已是明确结论，直接写已连接。
                if self._still:
                    self.state_lbl.configure(text="已连接", fg=FG)
                    self.tip_lbl.configure(text="鼠标静止 · 读数沿用上次采集", fg=DIM)
                elif warm_text_ok(time.time(),
                                  getattr(self, "_boot_ts", 0.0),
                                  getattr(self, "_warm_done", False)):
                    self.state_lbl.configure(text="识别中…", fg=DIM)
                    self.tip_lbl.configure(text="正在读取鼠标状态…", fg=DIM)
                elif self._still or self._ch:
                    # v12.0：接收器/线还插在机器上，只是这一轮没应答。鼠标不动时
                    # 设备本来就不回包，这不是故障。以前这里写「未收到回包，
                    # 正在重试…」，用户正常使用中短暂停手就看到这句，被当成程序
                    # 出错。现在只要通道还在，一律按「识别中」处理；只有通道真的
                    # 被拔走、或超出静止窗口，才照实报未响应。
                    self.state_lbl.configure(text="识别中…", fg=DIM)
                    bt_unk = getattr(self, "_bt_unk", False)
                    # 超时提示只有一处：别再为 still / bt_unk 两条路径各写一份。
                    # bt_unk 时不显示（还在确认连接方式，谈不上「长时间无上报」）。
                    if self._nosig >= BT_WAIT_HINT and (self._still or not bt_unk):
                        # 等太久还没等到任何一侧的证据（「2.4G 拨到蓝牙」最容易
                        # 落在这里）：给出可操作的检查方向，别让用户以为软件卡死。
                        self.tip_lbl.configure(
                            text="长时间无上报 · 请确认鼠标电源、拨档开关与蓝牙配对",
                            fg=DIM)
                    elif self._still:
                        # 静止沿用最多撑 STILL_HOLD_MAX（30 分钟）
                        self.tip_lbl.configure(text="鼠标静止，等待下次上报…", fg=DIM)
                    elif bt_unk:
                        # 正在确认「鼠标还在 2.4G 还是已经切到蓝牙」，通常只持续
                        # 一两轮（链路探测约 0.2 秒），写清楚在干什么。
                        self.tip_lbl.configure(text="正在确认连接方式…", fg=DIM)
                    else:
                        # 仍不写「未收到回包」——那会被当成软件故障（v12.0 的教训）
                        self.tip_lbl.configure(text="正在读取鼠标状态…", fg=DIM)
                else:
                    # 拿不到回包才显示未响应。旧版这里写「休眠」，鼠标在用也照弹，
                    # 用户看到就是「明明在用却显示休眠」。
                    self.state_lbl.configure(text="未响应", fg=DIM)
                    self.tip_lbl.configure(text="未收到回包，正在重试…", fg=DIM)
        if not online and self.dpis and self.dpi_idx is not None:
            shown = dpi_tiers(self.dpis)
            self.dpi_sub.configure(
                text=f"第 {min(self.dpi_idx + 1, shown)}/{shown} 档 · 上次读数")
        # v14：圆盘与完整面板同一轮刷新、同一个数值来源
        self._sync_simple(pct, online)

    def quit(self):
        self._stop.set()
        self._kick.set()
        if self.simple is not None:
            try:
                self._simple_pos = self.simple.pos()
                self._save_cache()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
            try:
                self.simple.destroy()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        try:
            if self.hook:
                self.hook.destroy()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        try:
            if self.tray:
                self.tray.destroy()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        try:
            self.reader.close()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        self._stop_charge_anim()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
