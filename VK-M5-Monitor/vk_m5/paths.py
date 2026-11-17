# -*- coding: utf-8 -*-
"""缓存/日志路径与轻量排查日志。"""

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

CACHE_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                          "VK_M5", "state.json")

# v13.10：上次握手成功的 HID 控制口路径。进程内首次 HID 枚举会被 SetupAPI
# 卡住（实机 40~50 秒），这期间设备树快照为空、界面只能一直写「识别中」。
# 记住上次的路径后，下一轮直接用句柄握手，把这段空窗抹掉。
USB_PATH_CACHE = os.path.join(os.path.dirname(CACHE_PATH), "usb_path.txt")


def load_usb_path():
    """读上次成功的控制口路径；没有/读不到返回 None"""
    try:
        with open(USB_PATH_CACHE, "r", encoding="utf-8") as f:
            p = f.read().strip()
        return p or None
    except Exception:
        return None


def save_usb_path(path):
    """记住这轮握手成功的控制口路径（写失败忽略，下次退回枚举）"""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(USB_PATH_CACHE), exist_ok=True)
        with open(USB_PATH_CACHE, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass  # 可忽略：缓存写不进去只是下次慢一点


def _write_log(line):
    """写一行排查日志；未开 VK_M5_DEBUG 时零开销直接返回。"""
    if not os.environ.get("VK_M5_DEBUG"):
        return
    try:
        p = os.path.join(os.path.dirname(CACHE_PATH), "hook.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("%.2f %s\n" % (time.time(), line))
    except Exception:
        pass  # 可忽略：幂等清理/UI 收尾


def _dbg(msg):
    """轻量排查日志：只在设置了 VK_M5_DEBUG=1 时写文件"""
    _write_log(str(msg))


def swallow(where, exc=None):
    """静默异常的统一收口。

    设计约定（v13.3 起）：except 分支不再写裸 pass，一律调用本函数留痕。
    - 默认（未开 VK_M5_DEBUG）直接返回：不写盘、不打印、异常被吞掉，行为与 pass 完全一致；
    - 开了 VK_M5_DEBUG 时按 `[swallow] 现场 类型: 描述` 记一行，事后可定位
      「某功能静默失效」到底是哪一层出的问题。
    """
    if not os.environ.get("VK_M5_DEBUG"):
        return
    name = type(exc).__name__ if exc is not None else "-"
    detail = str(exc) if exc is not None else ""
    _write_log("[swallow] %s %s: %s" % (where, name, detail))
