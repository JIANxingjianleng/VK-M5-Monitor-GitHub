# -*- coding: utf-8 -*-
"""颜色与电量格等纯展示换算。"""

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

from .config import ACCENT, BAD, DIM, WARN, YELLOW

def color_rgb(hexstr):
    h = hexstr.lstrip("#")
    return (int(h[0:2], 16)) | (int(h[2:4], 16) << 8) | (int(h[4:6], 16) << 16)


def battery_color(pct, online):
    if pct is None or not online:
        return DIM
    if pct <= 15:
        return BAD
    if pct <= 30:
        return WARN
    return ACCENT


def battery_band_color(pct, online=True):
    """简洁模式的四段配色：≥75 绿 / 50~75 橙 / 25~50 黄 / <25 红"""
    if pct is None or not online:
        return DIM
    if pct >= 75:
        return ACCENT
    if pct >= 50:
        return WARN
    if pct >= 25:
        return YELLOW
    return BAD


def battery_cells(pct):
    """五格电量：0~5，电量越低格数越少"""
    if pct is None:
        return 0
    if pct <= 0:
        return 0
    n = int(math.ceil(pct / 20.0))
    return max(1, min(5, n))
