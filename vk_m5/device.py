# -*- coding: utf-8 -*-
"""HID 通道层：枚举、握手、私有帧收发、通道活体探测。"""

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

from . import devices
from .config import BLE_PATH_TAG, CMD_GET_PROFILE, MAX_LAYER, REPORT_LEN, USAGE_VENDOR, VENDOR_PAGES, VID, WIRED_PIDS, WIRELESS_PIDS
from .paths import _dbg

def _hid():
    import hid
    return hid


def enc9(cmd):
    c = list(cmd) + [0] * (9 - len(cmd))
    c[7] = 255 - (sum(c[:7]) & 255)
    return bytes(c)

def _path_str(path):
    if isinstance(path, bytes):
        return path.decode("utf-8", "ignore")
    return str(path or "")


def _is_bt_path(path):
    p = _path_str(path).upper()
    return (BLE_PATH_TAG.upper() in p) or ("BTHLE" in p) or ("BTHENUM" in p)


def _path_pid(path):
    """从 HID 设备路径里取 PID（\\?\HID#VID_374A&PID_A321&MI_02#...）"""
    try:
        m = re.search(r"PID_([0-9A-Fa-f]{4})", _path_str(path))
        return int(m.group(1), 16) if m else None
    except Exception:
        return None


def _path_vid(path):
    try:
        m = re.search(r"VID_([0-9A-Fa-f]{4})", _path_str(path))
        return int(m.group(1), 16) if m else None
    except Exception:
        return None


def _is_vendor_page(page):
    """厂商自定义 usage_page：0xFFFF/0xFF55，或 0xFF00~0xFFFF 之间的任意值"""
    try:
        n = int(page)
    except Exception:
        return False
    return n in VENDOR_PAGES or 0xFF00 <= n <= 0xFFFF


# ---- v13.10：设备树枚举快照 -------------------------------------------
# 实机 py-spy 采样（v13.9 运行中，worker 线程 30 次采样）显示：16 次停在
# hid.enumerate() 里，13 次在等待循环，只有 1 次在私有帧复核 —— 也就是采集
# 线程一半以上的时间花在枚举设备树上。原因：鼠标拨到蓝牙、Windows 正在枚举/
# 重连 BLE HID 节点时，SetupAPI 单次枚举能阻塞十几到二十几秒（实测首次枚举
# 6.9 秒），而一轮采集要枚举 5~6 次（probe_usb / _usb_ctrl_exists ×3 / 找
# 蓝牙节点地址），整轮就被钉在十几到几十秒，界面停在「识别中」不动。
# 改成「快照 + 后台刷新 + 主线程限时等待」：一次枚举的结果给整轮复用，
# 后台线程卡住时主线程最多等 _DEV_WAIT 秒，超时沿用上一份快照，不阻塞采集。
_DEV_SNAP = {"val": [], "ts": 0.0}
_DEV_LOCK = threading.Lock()
_DEV_BUSY = False
_DEV_TTL = 0.5       # 快照新鲜期：一轮里的重复枚举直接命中，不再进系统枚举
_DEV_WAIT = 0.12     # 主线程为「刷新一次快照」最多等多久
_DEV_COLD_WAIT = 0.1  # 冷启动「等预热线程」的上限：只等一小会儿，不为枚举陪跑
DEV_STALE = 3.0      # 超过这么久没刷新出新快照＝设备树不可信（枚举被卡住）


def _enum_vk():
    """真正调一次系统枚举（可能长时间阻塞，只在后台线程或冷启动时调用）"""
    try:
        devs = _hid().enumerate(0, 0)
    except Exception:
        return []
    out = []
    vids = devices.all_vids()
    for d in devs:
        if d.get("vendor_id") not in vids:
            continue
        out.append({"path": d.get("path") or b"",
                    "pid": d.get("product_id") or 0,
                    "usage_page": d.get("usage_page"),
                    "usage": d.get("usage"),
                    "bt": _is_bt_path(d.get("path")),
                    "ctrl": _is_vendor_page(d.get("usage_page"))})
    return out


def _dev_refresh_async():
    """后台刷新一次快照；已有刷新在跑就直接返回（不排队、不积压）"""
    global _DEV_BUSY
    with _DEV_LOCK:
        if _DEV_BUSY:
            return
        _DEV_BUSY = True

    def _run():
        global _DEV_BUSY
        try:
            val = _enum_vk()
            with _DEV_LOCK:
                _DEV_SNAP["val"] = val
                _DEV_SNAP["ts"] = time.time()
        finally:
            with _DEV_LOCK:
                _DEV_BUSY = False

    try:
        threading.Thread(target=_run, name="dev_enum", daemon=True).start()
    except Exception:
        with _DEV_LOCK:
            _DEV_BUSY = False


def dev_snapshot_age():
    """当前快照有多旧（秒）；从未枚举成功过返回一个很大值"""
    with _DEV_LOCK:
        ts = _DEV_SNAP["ts"]
    return (time.time() - ts) if ts else 1e9


def list_devs(max_age=None):
    """列出所有 VK M5 相关 HID 节点（USB / 2.4G / 蓝牙混在一起）

    蓝牙模式下 PID 变 0xA323、usage_page 变 0xFF55、product_string 变成
    "Model_Number"（BLE HID 描述符顺序所致），所以判定只按 VID 收设备，
    再用路径区分是蓝牙还是 USB/接收器。

    v13.10：不再每调一次就同步枚举，改为读快照（见上面那段注释）。
    冷启动没有旧快照时仍然同步枚举一次 —— 只有那一次必须等，否则上层会把
    「还没枚举出来」当成「设备树里没有接收器」，直接误判成只挂蓝牙。
    """
    ttl = _DEV_TTL if max_age is None else max_age
    now = time.time()
    with _DEV_LOCK:
        val = list(_DEV_SNAP["val"])
        ts = _DEV_SNAP["ts"]
    if ts and now - ts <= ttl:
        return val
    if not ts:
        # 冷启动：还没有任何可用快照。
        # 若有预热线程正在枚举，就等它的结果，绝不并发再发一次枚举 —— 首次
        # 枚举实测能卡 33 秒，发两次等于把「识别中」翻倍。
        if _DEV_BUSY:
            deadline = time.time() + _DEV_COLD_WAIT
            while time.time() < deadline:
                time.sleep(0.02)
                with _DEV_LOCK:
                    if _DEV_SNAP["ts"]:
                        return list(_DEV_SNAP["val"])
                    busy = _DEV_BUSY
                if not busy:
                    break
            with _DEV_LOCK:
                if _DEV_SNAP["ts"]:
                    return list(_DEV_SNAP["val"])
            return []      # 仍未就绪：上层据此跳过本轮判定
        val = _enum_vk()   # 没有预热线程（独立调用/单测），同步等一次
        with _DEV_LOCK:
            _DEV_SNAP["val"] = val
            _DEV_SNAP["ts"] = time.time()
        return list(val)
    # 有旧快照：后台去刷新，最多等 _DEV_WAIT 拿新结果，超时沿用旧值
    _dev_refresh_async()
    deadline = time.time() + _DEV_WAIT
    while time.time() < deadline:
        time.sleep(0.01)
        with _DEV_LOCK:
            if _DEV_SNAP["ts"] > ts:
                return list(_DEV_SNAP["val"])
    age = time.time() - ts
    if age >= 1.0:
        _dbg("dev_snap: enum stalled %.1fs, reuse %d nodes" % (age, len(val)))
    with _DEV_LOCK:
        return list(_DEV_SNAP["val"])


def find_vendor_path(mode=None):
    """取控制口路径；mode="bt" 只找蓝牙节点，其余只找 USB/2.4G 节点"""
    devs = list_devs()
    if not devs:
        return None
    want_bt = (mode == "bt")
    cands = [d for d in devs if d["ctrl"] and d["bt"] == want_bt]
    if not cands:
        cands = [d for d in devs if d["bt"] == want_bt]
    if not cands:
        return None
    for d in cands:
        if d.get("usage") == USAGE_VENDOR:
            return d["path"]
    return cands[0]["path"]


def _usb_alive(st):
    """USB / 2.4G 侧这一帧回包能不能说明「鼠标真的挂在这条通道上」

    关键：接收器插在电脑上、鼠标却挂在蓝牙（或关机）时，接收器照样会回一帧
    F7 —— 电量字段是上一次的旧值（实测 100），can_read=0、online=0。旧版判据
    是「有回包即在线」，就被这帧假回包钉死成 2.4G，之后 USB 侧永远不会被判
    失败，蓝牙判定（要 USB 先连续失败）也就永远不触发，只能退出软件重启。
    真在线时 can_read（私有帧可读）或 online（状态位为 0）必有一个成立，
    所以用这两个位判，回包本身只当「通道存在」。
    """
    if not st:
        return False
    return bool(st.get("can_read") or st.get("online"))


def _suspect_fake(st):
    """这一帧「说在线」的话，像不像接收器空置时回的旧缓存（v11.7）

    只看帧内位互相矛不矛盾：真在线时 can_read（私有帧可读）与 online（状态位）
    至少有一个成立；实测空置接收器是 can_read=0、online=1、can_send=1、电量恒 100
    —— 也就是 online 单方面报真、can_read 却说私有帧读不到，正是可疑组合。

    注意：v11.6 那个「F7 状态位假死」的真在线场景（can_read=0 / online=0）在复核
    里会被私有帧证明是活的，所以这里判「可疑」只影响复核频率，不代表判死 ——
    真假一律由私有帧（deep_probe）定，位再怎么矛盾也不会单独写进界面。
    """
    if not st:
        return False
    if st.get("can_read"):
        return False          # 私有帧位报可读：真在线，走低频兜底复核即可
    return bool(st.get("online"))

def probe_link():
    """静态探测：设备树里有哪些通道 -> (mode, pid, path)

    mode: "wired" 有线 / "2.4g" 无线 / "bt" 蓝牙 / "unknown"
    注意这只是“枚举到了什么”，鼠标此刻到底挂在哪条通道上由 Reader 判活。
    """
    devs = list_devs()
    if not devs:
        return "unknown", None, None
    usb = [d for d in devs if not d["bt"]]
    bt = [d for d in devs if d["bt"]]
    ctrl = None
    for d in usb:
        if d.get("usage") == USAGE_VENDOR:
            ctrl = d
            break
    if ctrl is None and usb:
        ctrl = usb[0]
    if ctrl is not None:
        pid = ctrl["pid"]
        mode = devices.mode_for_pid(pid)
        if mode in ("wired", "2.4g"):
            return mode, pid, ctrl["path"]
    if bt:
        return "bt", bt[0]["pid"], bt[0]["path"]
    if ctrl is not None:
        return "2.4g", ctrl["pid"], ctrl["path"]
    return "unknown", None, None


ACT_CACHE = 3.0      # 活动控制口判活结果的缓存时长（秒）
_ACT_CACHE = {"ts": 0.0, "val": None}


def _handshake(path):
    """在指定控制口上做一次 F7 握手判活 -> dict 或 None

    返回里带上 can_read / can_send 与原始帧前 12 字节：判断「鼠标真的挂在
    这条通道上」还是「接收器空置、只回了一帧旧数据」全靠它们。
    """
    dev = None
    try:
        dev = _hid().device()
        dev.open_path(path)
        dev.set_nonblocking(1)
        dev.send_feature_report(bytes([0x00]) + enc9([0xF7]) +
                                bytes(REPORT_LEN - 10))
        time.sleep(0.02)
        for _ in range(5):
            try:
                r = bytes(dev.get_feature_report(0x00, REPORT_LEN))
            except Exception:
                r = b""
            if r and any(r[1:]):
                p = r[1:]
                if len(p) >= 10:
                    dtype = p[6]
                    online = (p[4] == 0) if dtype == 2 else (p[3] == 0)
                    _off = devices.battery_offset_for(
                        _path_vid(path), _path_pid(path))
                    if _off is None or _off >= len(p):
                        _off = 2 if dtype in (2, 3) else 1
                    return {"battery": p[_off] if _off < len(p) else p[2],
                            "online": online, "type": dtype,
                            "can_read": p[0] == 1, "can_send": p[5] == 1,
                            "raw": bytes(p[:12])}
                break
            time.sleep(0.02)
    except Exception:
        return None
    finally:
        try:
            if dev is not None:
                dev.close()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
    return None


def ctrl_candidates():
    """设备树里所有 USB / 2.4G 侧厂商控制口 [(mode, pid, path)...]"""
    out = []
    for d in list_devs():
        if d["bt"] or not d["ctrl"]:
            continue
        pid = d["pid"]
        mode = devices.mode_for_pid(pid) or "2.4g"
        out.append((mode, pid, d["path"]))
    return out


def _usb_ctrl_exists():
    """设备树里还有没有 USB/2.4G 侧控制口（物理上插着线或接收器）

    v13.10：枚举被系统卡住时快照只能沿用旧的，此时「快照里没看到控制口」
    不足以断言接收器/线真的不在（上层拿这个结论去清静止状态、去放行蓝牙
    判定）。快照过期就保守报「在」，宁可慢一轮，不可误判通道消失。
    """
    try:
        if bool(ctrl_candidates()):
            return True
        return dev_snapshot_age() > DEV_STALE
    except Exception:
        return False


def probe_usb(prev_path=None):
    """握手判断鼠标是否真的挂在某条 USB / 2.4G 通道上

    2.4G 接收器和 USB 线同时插着时，设备树里会同时出现多个厂商控制口。若只按
    枚举顺序取第一个，命令可能发到鼠标当前并未使用的那条通道，拿回来的是那
    一路的旧缓存 —— 表现就是档位/电量对不上、刷新一下又好了。

    返回值：dict(mode, pid, path, alive, st)
      alive=True  握手拿到了真实在线证据（can_read / online 位），st 即那一帧
      alive=False 通道在、但鼠标不在这条路上（接收器空置 / 已切蓝牙 / 关机）
    """
    cands = ctrl_candidates()
    if not cands:
        m, pid, p = probe_link()
        return {"mode": m, "pid": pid, "path": p, "alive": False, "st": None}
    if prev_path:
        cands.sort(key=lambda c: 0 if c[2] == prev_path else 1)
    fallback = None
    last_st = None
    for mode, pid, path in cands:
        st = _handshake(path)
        if _usb_alive(st):
            _ACT_CACHE.update(ts=time.time(), val=(mode, pid, path), alive=True)
            return {"mode": mode, "pid": pid, "path": path, "alive": True, "st": st}
        if st is not None:
            last_st = st
        if fallback is None:
            fallback = (mode, pid, path)
    mode, pid, path = fallback or (probe_link()[0], None, None)
    _ACT_CACHE.update(ts=time.time(), val=(mode, pid, path), alive=False)
    # v13.7：alive=False 也要把 F7 那一帧带回去。判「鼠标在 2.4G 上静止」与
    # 「接收器空置、鼠标已拨到蓝牙」全靠它的 online/can_read 位，旧版在这里
    # 把帧丢掉，上层就只能拿 Windows 那边常年残留的 BLE 链路位去猜。
    return {"mode": mode, "pid": pid, "path": path, "alive": False,
            "st": last_st}


def deep_probe(path, timeout=0.9):
    """私有帧复核：这条 USB/2.4G 通道到底通不通

    F7 状态帧里的 can_read / online 位在个别固件状态下并不可靠（v8 起就有
    「online 位常年为 0」的记录）。若只凭这两位判死，鼠标明明挂在 2.4G 上却
    会被判成「不在这条路上」，界面反而漂去蓝牙 —— 那就从「切不过去」变成
    「乱切」。所以连续判死时再用私有帧（GET_PROFILE）复核一次：能收到对应
    回包才算真的不通。空置接收器实测：request_read 等 1.2 秒也拿不到回包。
    """
    if not path:
        return False
    dev = None
    try:
        dev = _hid().device()
        dev.open_path(path)
        dev.set_nonblocking(1)
        dev.send_feature_report(bytes([0x00]) + enc9([CMD_GET_PROFILE]) +
                                bytes(REPORT_LEN - 10))
        time.sleep(0.03)
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                r = bytes(dev.get_feature_report(0x00, REPORT_LEN))
            except Exception:
                r = b""
            if r and any(r[1:]):
                p = r[1:]
                if len(p) > 1 and p[0] == CMD_GET_PROFILE and p[1] < MAX_LAYER:
                    return True
            time.sleep(0.03)
    except Exception:
        return False
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
    return False
