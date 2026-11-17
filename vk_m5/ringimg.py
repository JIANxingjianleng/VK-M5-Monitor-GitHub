# -*- coding: utf-8 -*-
"""简洁模式圆盘位图：纯标准库生成 RGBA 像素。

两个出口：
- ring_rgba()  直接给分层窗口用的带 alpha 像素（边缘平滑）
- ring_png()   需要普通 PNG 时用（按 alpha 编码）

为什么不用 Pillow：onefile 打包会多出十几 MB，而这里只需要画个圆环，
struct+zlib 手写 PNG / 手算像素就够了。

约定：
- 环从 12 点方向起，顺时针，弧长 = pct% * 360 度
- 圆外留 2px 透明缓冲
"""

import math
import struct
import zlib

TRANSPARENT = "#010203"   # 透明色键（几乎不会出现在主题配色里）


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_from_rgba(w, h, rgba):
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)                      # filter type 0
        raw += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def hex_rgb(h):
    """'#rrggbb' -> (r, g, b)"""
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix(c1, c2, t):
    return (c1[0] + (c2[0] - c1[0]) * t,
            c1[1] + (c2[1] - c1[1]) * t,
            c1[2] + (c2[2] - c1[2]) * t)


def ring_rgba(size=124, pct=None, ring_color="#33d17a", ring_w=6.0,
              face="#1f1f27", face_alpha=255, track="#34343f", ss=8):
    """画一枚圆盘：外圈是电量环，内部是实心盘面，返回 (size*size*4) 字节。

    pct=None 表示还没读到电量 —— 只画轨道，不画弧。
    ss 为每像素子采样数（ss x ss）：只在环带和边缘这两条窄带上做，
    内部实心区直接填充，所以 124px 圆盘几十毫秒就能算完。
    """
    cx = cy = (size - 1) / 2.0
    r_out = size / 2.0 - 2.0          # 盘面外沿（留 2px 透明缓冲）
    r_ring = r_out - ring_w / 2.0     # 环带中心线
    half = ring_w / 2.0
    r_in = r_ring - half              # 环内沿

    face_rgb = hex_rgb(face)
    track_rgb = hex_rgb(track)
    base_rgb = hex_rgb(ring_color)
    lit_rgb = _mix(base_rgb, (255.0, 255.0, 255.0), 0.09)
    dark_rgb = _mix(base_rgb, (0.0, 0.0, 0.0), 0.07)
    fri, fgi, fbi = int(face_rgb[0]), int(face_rgb[1]), int(face_rgb[2])

    span = 0.0
    if pct is not None:
        span = max(0.0, min(100.0, float(pct))) * 3.6

    n = ss * ss
    off = [(i + 0.5) / ss - 0.5 for i in range(ss)]
    fa = float(face_alpha)
    fai = int(round(min(255.0, fa)))

    px = bytearray(size * size * 4)
    soft_in = r_in - 0.7              # 以内：纯盘面
    soft_out = r_out + 0.3            # 以外：全透明
    edge0 = r_out - 1.8               # 外沿软过渡起点
    edge1 = r_out + 0.3               # 外沿完全透明点
    for y in range(size):
        dy = y - cy
        row = y * size
        for x in range(size):
            dx = x - cx
            d = math.hypot(dx, dy)
            if d > soft_out:
                continue
            i = (row + x) * 4
            if d < soft_in:                       # 内部实心：直接填
                px[i] = fri
                px[i + 1] = fgi
                px[i + 2] = fbi
                px[i + 3] = fai
                continue
            cface = 0
            cring = 0
            for oy in off:
                yy = y + oy - cy
                for ox in off:
                    dd = math.hypot(x + ox - cx, yy)
                    if dd <= r_out:
                        cface += 1
                    if -half <= dd - r_ring <= half:
                        cring += 1
            if cface == 0 and cring == 0:
                continue                          # 圆外：全透明
            cov_face = cface / n
            cov_ring = cring / n
            den = cov_face if cov_face > cov_ring else cov_ring
            # 环色：按像素中心角度取渐变 / 轨道色
            ang = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
            if span > 0 and ang <= span:
                ring_rgb = _mix(lit_rgb, dark_rgb, ang / 360.0)
            else:
                ring_rgb = track_rgb
            rgb = _mix(face_rgb, ring_rgb, cov_ring / den) if den else face_rgb
            a = cov_face * fa
            ar = cov_ring * 255.0
            if ar > a:
                a = ar
            if d > edge0:                 # 外沿 1.5px 软过渡，消掉台阶感
                t = (d - edge0) / (edge1 - edge0)
                if t > 1.0:
                    t = 1.0
                a *= (1.0 - t)
            px[i] = int(round(rgb[0]))
            px[i + 1] = int(round(rgb[1]))
            px[i + 2] = int(round(rgb[2]))
            px[i + 3] = int(min(255.0, round(a)))
    return px


def ring_png(size=124, pct=None, ring_color="#33d17a", ring_w=6.0,
             face="#1f1f27", face_alpha=255, track="#34343f", ss=8):
    """同上，输出 PNG 字节"""
    px = ring_rgba(size, pct, ring_color, ring_w, face, face_alpha, track, ss)
    return png_from_rgba(size, size, px)
