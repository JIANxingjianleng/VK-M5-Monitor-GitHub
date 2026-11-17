# -*- coding: utf-8 -*-
"""设备配置：允许在 devices.json 里加别的型号的鼠标。

默认只认 VK M5（VID 0x374A / PID A321/A322/A323）。用户可以在
%APPDATA%\\VK_M5\\devices.json 里追加设备，字段：

  name            显示名（仅用于日志/排错）
  vid             USB VID，支持 "0x374A" 或 14922
  pid_wired       有线 PID 列表
  pid_wireless    2.4G PID 列表
  pid_bt          蓝牙 PID 列表
  battery_offset  F7 帧里电量字节的下标；null = 内置规则（type 2/3 取 p[2]，
                  其他取 p[1]）
  bt_name_match   蓝牙名称匹配片段（大小写不敏感）
  bt_company      BTHLEDEVICE 实例名里的厂商片段，例如 "VID&02374A"
  bt_addr         可直接指定蓝牙地址（12 位十六进制，可带冒号）
  vendor          协议族；"a955" 表示走 A955 厂商通道（FF02 发 / FF01 收），
                  电量不再从 F7 帧取
  battery_bands   电量分档阈值（百分比上界），A955 为 25/50/75/100 四档
  dpi_table       档位数值表，A955 为 400/800/1600/3200/6400 五档
  dpi_default     默认档序号（1 起），A955 为 2（即 800）

文件不存在时会写一份默认配置，方便直接照着改；文件坏了只记日志并退回默认，
不会覆盖用户内容。
"""
import json
import os
import threading

from .paths import CACHE_PATH, _dbg, swallow

CONFIG_PATH = os.path.join(os.path.dirname(CACHE_PATH), "devices.json")

DEFAULT = {
    "name": "VK M5",
    "vid": 0x374A,
    "pid_wired": [0xA321],
    "pid_wireless": [0xA322],
    "pid_bt": [0xA323],
    "battery_offset": None,
    "bt_name_match": ["VK M5", "VALKYRIE", "VK-M5"],
    "bt_company": ["VID&02374A"],
    "bt_addr": "",
    "vendor": "",
    "battery_bands": [],
    "dpi_table": [],
    "dpi_default": 0,
}

# 内置的 A955 档案（达尔优/Compx，VID 0x260D）。规则来自官方驱动
# OemDrv.exe 与 Cfg.ini [DEV_1]：电量四档 25/50/75/100（power1~4），
# DPI 五档 400/800/1600/3200/6400，默认第 2 档 800。
# 用户文件里自己写了同 VID 的档案时以用户为准，这里不重复加。
A955_PROFILE = {
    "name": "A955",
    "vid": 0x260D,
    "pid_wired": [0x1097],
    "pid_wireless": [0x1074],
    # 蓝牙形态不纳入识别：那条路只有标准 GATT，够不到 FF02 厂商通道，
    # 档位既读不到也改不了，专门适配没有意义。
    "pid_bt": [],
    "battery_offset": None,
    "bt_name_match": [],
    "bt_company": [],
    "bt_addr": "",
    "vendor": "a955",
    "battery_bands": [25, 50, 75, 100],
    # 静态档位表只作兜底：优先读设备配置内存里的真实档位与当前档。
    # 实测本机档位表为 500/950/1900/3800/7550（不是 Cfg.ini 里那份 400 起的
    # 默认表），兜底也用实测值，免得读不到时界面显示成另一套数字。
    "dpi_table": [500, 950, 1900, 3800, 7550],
    "dpi_default": 4,
}

_LOCK = threading.Lock()
_CACHE = {"profiles": None, "mtime": None}

_TEMPLATE = {
    "_doc": ("可在这里追加要识别的鼠标。VK M5 默认已内置；别的型号填 vid/pid_*。"
             "battery_offset 是 F7 帧电量字节下标，null=内置规则。"
             "bt_addr 可直接指定蓝牙地址（12 位十六进制）。"),
    "devices": [dict(DEFAULT)],
}


def _as_int(v, default=None):
    try:
        if isinstance(v, str):
            return int(v.strip(), 16) if v.strip().lower().startswith("0x") \
                else int(v.strip(), 0)
        return int(v)
    except Exception:
        return default


def _as_int_list(v):
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        v = [v]
    out = []
    for x in v:
        n = _as_int(x)
        if n is not None:
            out.append(n)
    return out


def _as_str_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip().upper() for x in v if str(x).strip()]


def _norm_addr(v):
    s = "".join(ch for ch in str(v or "") if ch in "0123456789abcdefABCDEF")
    return s.upper() if len(s) == 12 else ""


def _norm(p):
    if not isinstance(p, dict):
        return None
    vid = _as_int(p.get("vid"))
    addr = _norm_addr(p.get("bt_addr"))
    # 只填蓝牙地址的设备也允许：非 VK 的 BLE 鼠标往往只知道地址
    if vid is None and not addr:
        return None
    off = p.get("battery_offset")
    off = None if off is None else _as_int(off)
    return {
        "name": str(p.get("name") or ("VID%04X" % vid if vid is not None
                                      else "BLE " + addr)),
        "vid": vid,
        "pid_wired": _as_int_list(p.get("pid_wired")),
        "pid_wireless": _as_int_list(p.get("pid_wireless")),
        "pid_bt": _as_int_list(p.get("pid_bt")),
        "battery_offset": off,
        "bt_name_match": _as_str_list(p.get("bt_name_match")),
        "bt_company": _as_str_list(p.get("bt_company")),
        "bt_addr": addr,
        "vendor": str(p.get("vendor") or "").strip().lower(),
        "battery_bands": _as_int_list(p.get("battery_bands")),
        "dpi_table": _as_int_list(p.get("dpi_table")),
        "dpi_default": _as_int(p.get("dpi_default"), 0) or 0,
    }


def _write_template():
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_TEMPLATE, f, ensure_ascii=False, indent=2)
        _dbg("devices: template written to %s" % CONFIG_PATH)
    except Exception as e:
        swallow("devices.template", e)


def load(force=False):
    """读取并缓存设备配置；返回规范化后的 profile 列表"""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except Exception:
        mtime = None
    with _LOCK:
        if (not force and _CACHE["profiles"] is not None
                and _CACHE["mtime"] == mtime):
            return _CACHE["profiles"]
        profiles = []
        raw = None
        if mtime is not None:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                _dbg("devices: bad config, fallback default: %r" % (e,))
        else:
            _write_template()
        if isinstance(raw, dict):
            items = raw.get("devices") if isinstance(raw.get("devices"), list) \
                else [raw]
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        for it in items:
            p = _norm(it)
            if p:
                profiles.append(p)
        if not profiles:
            profiles = [_norm(dict(DEFAULT))]
        # A955 不并进 profiles：profiles 的 VID 集合是 VK 私有帧的枚举范围，
        # 混进 0x260D 会让判活流程拿 VK 的 F7 帧去敲 A955 的厂商通道。
        # A955 只在 builtin_a955() 里单独取，走自己的协议分支。
        _CACHE["profiles"] = profiles
        _CACHE["mtime"] = mtime
        return profiles


def all_vids():
    vids = {p["vid"] for p in load() if p["vid"] is not None}
    return vids or {0x374A}


def name_matches():
    out = []
    for p in load():
        out.extend(p["bt_name_match"])
    # A955 不做蓝牙适配（bt_name_match 为空），这里不用再补
    a = builtin_a955()
    out.extend(a.get("bt_name_match") or [])
    return out or ["VK M5", "VALKYRIE", "VK-M5"]


def company_tags():
    """BLE 实例名里的厂商 VID 标签（用来确认「这个 BLE 设备是鼠标」）

    A955 的蓝牙形态不参与识别，它不再往这里补标签。
    """
    out = []
    for p in load():
        out.extend(p["bt_company"])
    for t in A955_PROFILE["bt_company"]:
        if t not in out:
            out.append(t)
    return out or ["VID&02374A"]


def bt_addr_override():
    for p in load():
        if p["bt_addr"]:
            return int(p["bt_addr"], 16)
    return None


def profile_for(vid=None, pid=None):
    ps = load()
    if vid is not None:
        same = [p for p in ps if p["vid"] == vid]
        if same:
            if pid is not None:
                for p in same:
                    if pid in (p["pid_wired"] + p["pid_wireless"] + p["pid_bt"]):
                        return p
            return same[0]
    if pid is not None:
        for p in ps:
            if pid in (p["pid_wired"] + p["pid_wireless"] + p["pid_bt"]):
                return p
    return None


def mode_for_pid(pid):
    """PID -> wired / 2.4g / None（蓝牙按路径判定，不看 PID）"""
    for p in load():
        if pid in p["pid_wired"]:
            return "wired"
        if pid in p["pid_wireless"]:
            return "2.4g"
    return None


def battery_offset_for(vid, pid):
    """返回 F7 电量字节下标；None = 用内置规则"""
    p = profile_for(vid, pid)
    if p is None:
        return None
    return p["battery_offset"]


def builtin_a955():
    """A955 档案：用户 devices.json 里写了同 VID 的就以用户的为准"""
    for p in load():
        if p["vid"] == A955_PROFILE["vid"]:
            return p
    return _norm(dict(A955_PROFILE))


def _is_a955_vid(vid):
    """A955 只认 USB 侧 VID 0x260D（2.4G 接收器 / 有线直连）"""
    try:
        return int(vid) == A955_PROFILE["vid"]
    except (TypeError, ValueError):
        return False


def vendor_for(vid, pid=None):
    """该设备走哪个协议族；"" = VK 私有帧，\"a955\" = FF02/FF01 厂商通道"""
    p = profile_for(vid, pid)
    if p is not None and p.get("vendor"):
        return p["vendor"]
    if _is_a955_vid(vid):
        return builtin_a955().get("vendor") or ""
    return ""


def dpi_info(vid, pid=None):
    """档位表与默认档：(table, default_1based)；没配置返回 ([], 0)"""
    p = profile_for(vid, pid)
    if p is None or not p.get("dpi_table"):
        if _is_a955_vid(vid):
            p = builtin_a955()
        else:
            return [], 0
    return list(p.get("dpi_table") or []), int(p.get("dpi_default") or 0)


def bands_for(vid, pid=None):
    """电量分档阈值（百分比上界）；空表示不特殊处理"""
    p = profile_for(vid, pid)
    if p is None or not p.get("battery_bands"):
        if _is_a955_vid(vid):
            p = builtin_a955()
        else:
            return []
    return list(p.get("battery_bands") or [])


def name_for_pid(pid):
    """按 PID 反查型号名，认不出返回 None

    PID 是三模之间唯一稳定的区分依据：2.4G 接收器与有线直连的 VID 相同，
    最终能确认「现在插的是哪只鼠标」的只有 PID。界面上的型号标题、托盘
    tooltip 都用它来切，换鼠标后才会跟着变。
    """
    if not pid:
        return None
    try:
        pid = int(pid)
    except Exception:
        return None
    for p in load():
        if pid in (p["pid_wired"] + p["pid_wireless"] + p["pid_bt"]):
            return p["name"]
    a = builtin_a955()
    if pid in (a["pid_wired"] + a["pid_wireless"] + a["pid_bt"]):
        return a["name"]
    return None
