# -*- coding: utf-8 -*-
"""简洁模式：一个小圆盘悬浮窗。

布局（SIZE 见下，默认 124px）：
- 环：贴着圆边，弧长 = 电量百分比，12 点起顺时针
- 中心：电量数值（大字）+ 紧跟一个小 % 号，整体水平居中
- 中心偏下：DPI 数值
- 右上：连接方式短文案；右侧中部：刷新按钮（避开环的弧线）

抗毛边：圆盘由 layered 通过 UpdateLayeredWindow 提交带 alpha 的位图
（per-pixel alpha），边缘是真正的半透明过渡；Tk 的 -transparentcolor 是色键，
半透明像素会和色键混成黑边，所以圆盘不能画在色键窗口上。文字层仍是普通 Tk
窗口（色键透明），压在圆盘图层之上，两者位置同步。
"""

import base64
import math

import tkinter as tk
import tkinter.font as tkfont

from . import layered
from .config import ACCENT, DIM, FG, FONT, WARN
from .ringimg import TRANSPARENT, ring_png, ring_rgba
from .theme import battery_band_color

SIZE = 124          # 窗口边长
RING_W = 6.0        # 环宽
_BOLT = ((0.62, 0.00), (0.30, 0.54), (0.47, 0.54),
         (0.40, 1.00), (0.72, 0.46), (0.55, 0.46))
FACE = "#1f1f27"    # 盘面色（比桌面亮一点，圆盘才有存在感）
TRACK = "#34343f"   # 环未点亮部分的轨道色

# 文字层坐标（相对圆心）
NUM_DY = -5
DEN_DX, DEN_DY = None, 6
DPI_DY = 24
LINK_DX, LINK_DY = 24, -35
BTN_DX, BTN_DY = 40, 2       # 刷新按钮：右中偏下，落在环内沿以内，不碰弧线


def short_link(link, online, pending=False, still=False):
    """连接方式的短文案：圆盘右上放得下"""
    if link == "wired":
        return "有线"
    if link == "bt":
        return "蓝牙"
    if pending:
        return "蓝牙"
    if online:
        return "2.4G"
    if still:
        # v14：通道在、鼠标静止 —— 这时候电量是上次的有效读数，不是「识别中」。
        # 写「上次」和完整面板的「上次读数」对齐，别让人以为还在握手。
        return "上次"
    return "识别中"


class SimpleOverlay:
    def __init__(self, master, on_refresh=None, on_expand=None, on_move=None,
                 on_menu=None):
        self.on_refresh = on_refresh
        self.on_expand = on_expand
        self.on_move = on_move
        self.on_menu = on_menu
        self._drag_moved = False
        self._pct = None
        self._link = None
        self._online = None
        self._still = None
        self._dpi = None
        # v22：有线充电态（整圈绿环 + 大闪电 + 呼吸）
        self._charging = None
        self._charge_after = None
        self._charge_phase = 0.0
        self._img = None
        self._drag = None
        self._pos = (0, 0)
        self._layered_ok = False
        self._rgba = None
        self._pending = False
        # 菜单弹出期间不抢置顶（v15）：圆盘 5 秒一刷新，每次都把两层提到
        # HWND_TOPMOST，会把刚弹出的右键菜单压到圆盘下面去。
        self._no_top = False

        # ---- 文字层（色键透明） ----
        win = tk.Toplevel(master)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", TRANSPARENT)
        except Exception:
            pass          # 非 Windows / 老 Tk：退化成方角，不影响功能
        win.configure(bg=TRANSPARENT)

        cv = tk.Canvas(win, width=SIZE, height=SIZE, bg=TRANSPARENT,
                       highlightthickness=0, bd=0)
        cv.pack()
        self.cv = cv

        # ---- 圆盘层（per-pixel alpha） ----
        disc = tk.Toplevel(master)
        self.disc = disc
        disc.overrideredirect(True)
        disc.attributes("-topmost", True)
        disc.configure(bg=TRANSPARENT)
        try:
            layered.make_layered(disc)
            self._layered_ok = True
        except Exception:
            self._layered_ok = False     # 退回把圆盘画在文字层上

        self._place_default()
        self._bind()
        self._paint(None, None, None)

    # ---------- 位置 ----------
    def _place_default(self):
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        self.move(sw - SIZE - 28, sh - SIZE - 120)

    def move(self, x, y):
        self._pos = (int(x), int(y))
        geo = "%dx%d+%d+%d" % (SIZE, SIZE, self._pos[0], self._pos[1])
        self.win.geometry(geo)
        if self._layered_ok:
            self.disc.geometry(geo)

    def pos(self):
        return self._pos

    def _push_disc(self):
        """把当前圆盘位图推到分层窗口；窗口还没建好时会失败，留给下次重试"""
        if not self._layered_ok or self._rgba is None:
            return False
        try:
            self.disc.update_idletasks()
        except Exception:
            pass
        try:
            return bool(layered.push(self.disc, SIZE, SIZE, self._rgba))
        except Exception:
            return False

    def _sync_layer(self):
        """保证文字层压在圆盘层上面，且两层都是置顶"""
        if not self._layered_ok:
            return
        if self._pending and self._push_disc():
            self._pending = False
            self.cv.delete("disc")       # 圆盘已在独立图层上，去掉退化图
        if self._no_top:
            return                       # 菜单在屏幕上：这一步别把菜单压下去
        try:
            self.disc.attributes("-topmost", True)
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        try:
            layered.bring_above(self.win, self.disc)
        except Exception:
            try:
                self.win.lift(self.disc)
            except Exception:
                pass

    def set_topmost_hold(self, hold):
        """菜单弹出/关闭时调用：True 期间圆盘照常刷新画面，但不重新置顶"""
        self._no_top = bool(hold)
        if not self._no_top:
            try:
                self._sync_layer()
            except Exception:
                pass

    # ---------- 交互 ----------
    def _bind(self):
        """左/右键事件要同时挂在文字层和圆盘层上。

        圆盘是独立的 per-pixel alpha 分层窗口，它压在文字层上面时（连接方式
        变化会触发重绘/推图，Z 序可能翻上去）鼠标落在圆盘上，文字层根本收不到
        事件——用户看到的就是「切了连接方式以后右键弹不出菜单」。两层都绑，
        谁在上都能响应。
        """
        for w in (self.cv, self.disc):
            try:
                self._bind_one(w)
            except Exception:
                pass          # 圆盘层异常不影响文字层

    def _bind_one(self, w):
        c = w
        if w is self.cv:
            c.tag_bind("refresh", "<Enter>",
                       lambda e: c.itemconfigure("refresh", fill=ACCENT))
            c.tag_bind("refresh", "<Leave>",
                       lambda e: c.itemconfigure("refresh", fill=DIM))
            # 刷新按钮不做 tag_bind 点击：圆盘层在上时 tag 命中的是圆盘窗口、
            # 拿不到这个 tag，点击会失效。统一在 _release 里按坐标判定，两层都灵。
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag_to)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-3>", self._menu)
        c.bind("<Double-Button-1>", lambda e: self._expand())

    def _hit_refresh(self, e):
        """这次点击是否落在刷新按钮上（用屏幕坐标算，两层窗口都适用）"""
        try:
            bx, by = self.cv.coords("refresh")
        except Exception:
            return False
        try:
            px = e.x_root - self.win.winfo_rootx()
            py = e.y_root - self.win.winfo_rooty()
        except Exception:
            return False
        return (px - bx) ** 2 + (py - by) ** 2 <= 11 ** 2

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, *self.pos())

    def _drag_to(self, e):
        if not self._drag:
            return
        x0, y0, wx, wy = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if abs(dx) + abs(dy) > 2:
            self.move(wx + dx, wy + dy)
            self._drag_moved = True

    def _release(self, e):
        moved = self._drag_moved
        self._drag = None
        self._drag_moved = False
        # 只有真的拖动过才回写位置，单纯点一下不触发保存
        if moved and callable(self.on_move):
            try:
                self.on_move(self.pos())
            except Exception:
                pass
            return
        if not moved and self._hit_refresh(e):
            self._do_refresh()
            return "break"

    def _do_refresh(self, e=None):
        if callable(self.on_refresh):
            self.on_refresh()
        return "break"

    def _expand(self):
        if callable(self.on_expand):
            self.on_expand()
        return "break"

    def _menu(self, e):
        """右键：交给主程序弹出与完整模式完全同一套自绘菜单

        旧版在圆盘上直接 tk_popup 原生菜单 —— 圆盘是 overrideredirect + topmost
        且没有前台焦点，原生菜单拿不到激活，第一下点击会被系统吞掉，样式也和
        完整模式对不上。统一走 on_menu 回调，两处菜单同源同风格。
        """
        if callable(self.on_menu):
            try:
                self.on_menu(e.x_root, e.y_root)
            except Exception:
                pass
            return "break"
        # 兜底：没有回调时保持最小可用性
        m = tk.Menu(self.win, tearoff=0, activebackground=ACCENT,
                    activeforeground="#101014", font=(FONT, 9))
        m.add_command(label="展开完整面板", command=self._expand)
        m.add_command(label="立即刷新", command=self._do_refresh)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"

    # ---------- 显示 ----------
    def show(self):
        self.win.deiconify()
        if self._layered_ok:
            self.disc.deiconify()
        self._sync_layer()
        self.win.attributes("-topmost", True)

    def hide(self):
        self.win.withdraw()
        if self._layered_ok:
            self.disc.withdraw()

    def destroy(self):
        try:
            self.disc.destroy()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

    def set(self, pct=None, dpi=None, link=None, online=None, pending=False,
            still=False, charging=False):
        """外部喂数据：只在变化时重画，避免每轮都算位图"""
        if (pct, link, online, still, charging) != (
                self._pct, self._link, self._online, self._still,
                self._charging):
            self._pct, self._link, self._online, self._still = (
                pct, link, online, still)
            self._paint(pct, link, online, pending, still, charging)
        if dpi != self._dpi:
            self._dpi = dpi
            self.cv.itemconfigure("dpi", text=(str(dpi) if dpi else "--"))

    # ---------- 绘制 ----------
    def _paint(self, pct, link, online, pending=False, still=False,
               charging=False):
        self._charging = bool(charging)
        self._stop_charge()
        if charging:
            col = ACCENT
        else:
            col = (battery_band_color(pct, bool(online))
                   if pct is not None else DIM)
        ring_pct = 100 if charging else pct
        face = FACE
        self._rgba = ring_rgba(SIZE, ring_pct, ring_color=col, ring_w=RING_W,
                               face=face, track=TRACK)
        drawn = self._push_disc()
        self._pending = not drawn and self._layered_ok
        if not drawn:
            # 退化路径：圆盘画在文字层上（边缘可能有轻微硬边）
            png = ring_png(SIZE, ring_pct, ring_color=col, ring_w=RING_W,
                           face=face, track=TRACK)
            self._img = tk.PhotoImage(data=base64.b64encode(png))
            self.cv.delete("disc")
            self.cv.create_image(0, 0, image=self._img, anchor="nw", tags="disc")

        c = self.cv
        for tag in ("pct", "pctu", "dpi", "link", "refresh"):
            c.delete(tag)

        cx = cy = SIZE / 2.0
        if charging:
            # v22：有线充电态 —— 中心大闪电，不画数字和 %
            x0, y0, x1, y1 = 45, 28, 79, 74
            iw, ih = x1 - x0, y1 - y0
            pts = []
            for px, py in _BOLT:
                pts.extend([x0 + px * iw, y0 + py * ih])
            c.create_polygon(pts, fill=col, outline="", tags="pct")
        else:
            num = str(pct) if pct is not None else "--"
            size_num = 23 if len(num) <= 2 else 19
            f_num = tkfont.Font(root=self.win, family=FONT, size=size_num,
                                weight="bold")
            f_den = tkfont.Font(root=self.win, family=FONT, size=9,
                                weight="bold")
            w_num = f_num.measure(num)
            w_den = f_den.measure("%")
            gap = 3
            left = cx - (w_num + gap + w_den) / 2.0
            c.create_text(left, cy + NUM_DY, text=num, fill=col, font=f_num,
                          anchor="w", tags="pct")
            c.create_text(left + w_num + gap, cy + DEN_DY, text="%", fill=col,
                          font=f_den, anchor="w", tags="pctu")

        c.create_text(cx, cy + DPI_DY, text=(str(self._dpi) if self._dpi else "--"),
                      fill=FG, font=(FONT, 11, "bold"), anchor="center",
                      tags="dpi")

        txt = short_link(link, bool(online), pending, still)
        lcol = ACCENT if online else WARN
        c.create_text(cx + LINK_DX, cy + LINK_DY, text=txt, fill=lcol,
                      font=(FONT, 8), anchor="e", tags="link")
        # 推完图再压一次 Z 序：圆盘层不能盖在文字层上面
        self._sync_layer()
        c.create_text(cx + BTN_DX, cy + BTN_DY, text="\u21bb", fill=DIM,
                      font=(FONT, 10, "bold"), anchor="center", tags="refresh")

        self._sync_layer()
        if charging:
            self._start_charge()

    def _charge_color(self):
        k = 0.86 + 0.14 * math.cos(2.0 * math.pi * self._charge_phase / 1.2)
        r = max(0, min(255, int(0x33 * k)))
        g = max(0, min(255, int(0xD1 * k)))
        b = max(0, min(255, int(0x7A * k)))
        return "#%02x%02x%02x" % (r, g, b)

    def _charge_tick(self):
        self._charge_after = None
        if not self._charging:
            return
        self._charge_phase = (self._charge_phase + 0.08) % 1.2
        try:
            self.cv.itemconfigure("pct", fill=self._charge_color())
        except Exception:
            return
        try:
            self._charge_after = self.win.after(80, self._charge_tick)
        except Exception:
            self._charge_after = None

    def _start_charge(self):
        if self._charge_after is None:
            self._charge_phase = 0.0
            self._charge_tick()

    def _stop_charge(self):
        if self._charge_after is not None:
            try:
                self.win.after_cancel(self._charge_after)
            except Exception:
                pass
            self._charge_after = None
