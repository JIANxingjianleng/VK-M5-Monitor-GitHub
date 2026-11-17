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
              face="#1f1f27", face_alpha=246, track="#34343f", ss=4):
    """画一枚圆盘：外圈是电量环，内部是实心盘面，返回 (size, rgba)。

    pct=None 表示还没读到电量 —— 只画轨道，不画弧。
    ss = 每像素 ss x ss 子采样，得到平滑边缘（真 alpha）。
    """
    cx = cy = (size - 1) / 2.0
    r_out = size / 2.0 - 2.0          # 盘面外沿（留 2px 透明缓冲）
    r_ring = r_out - ring_w / 2.0     # 环带中心线
    half = ring_w / 2.0

    face_rgb = hex_rgb(face)
    track_rgb = hex_rgb(track)
    base_rgb = hex_rgb(ring_color)
    lit_rgb = _mix(base_rgb, (255.0, 255.0, 255.0), 0.16)
    dark_rgb = _mix(base_rgb, (0.0, 0.0, 0.0), 0.10)

    span = 0.0
    if pct is not None:
        span = max(0.0, min(100.0, float(pct))) * 3.6

    n = ss * ss
    off = [(i + 0.5) / ss - 0.5 for i in range(ss)]

    px = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            i = (y * size + x) * 4
            cface = 0
            cring = 0
            for oy in off:
                dy = y + oy - cy
                for ox in off:
                    dx = x + ox - cx
                    d = math.hypot(dx, dy)
                    if d <= r_out:
                        cface += 1
                    if abs(d - r_ring) <= half:
                        cring += 1
            if cface == 0 and cring == 0:
                continue                                    # 圆外：全透明
            cov_face = cface / n
            cov_ring = cring / n
            den = cov_face if cov_face > cov_ring else cov_ring
            # 环色：按像素中心角度取渐变 / 轨道色
            ang = (math.degrees(math.atan2(x - cx, -(y - cy))) + 360.0) % 360.0
            if span > 0 and ang <= span:
                ring_rgb = _mix(lit_rgb, dark_rgb, ang / 360.0)
            else:
                ring_rgb = track_rgb
            rgb = _mix(face_rgb, ring_rgb, cov_ring / den) if den else face_rgb
            a = max(cov_face * face_alpha, cov_ring * 255.0)
            px[i] = int(round(rgb[0]))
            px[i + 1] = int(round(rgb[1]))
            px[i + 2] = int(round(rgb[2]))
            px[i + 3] = int(min(255.0, round(a)))
    return px


def ring_png(size=124, pct=None, ring_color="#33d17a", ring_w=6.0,
             face="#1f1f27", face_alpha=246, track="#34343f", ss=4):
    """同上，输出 PNG 字节"""
    px = ring_rgba(size, pct, ring_color, ring_w, face, face_alpha, track, ss)
    return png_from_rgba(size, size, px)
