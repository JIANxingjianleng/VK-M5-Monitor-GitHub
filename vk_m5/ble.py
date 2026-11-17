# -*- coding: utf-8 -*-
"""蓝牙层：地址解析、GATT 电量、链路三态探测。"""

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

from .config import BLE_ADDR_RETRY, BLE_TIMEOUT, BT_LINK_FRESH, BT_LINK_GAP
from . import devices
from .device import _hid, _path_str, list_devs
from .paths import _dbg, swallow

_BT_ADDR_CACHE = {"addr": None, "ts": 0.0, "tried": 0.0}
_BT_CONN_CACHE = {}          # addr -> (connection_status, ts)：多设备择活用

# v18：Windows 给 VK 设备记的蓝牙公司 ID（BTHLEDEVICE 实例名里的
# `DEV_VID&02374A_PID&A323_...`）。注意它不是 USB 的 VID 0x374A —— 蓝牙这边
# 用的是 SIG 公司 ID 0x02374A。用它确认「这个 BLE 设备确实是 VK 的」，
# 避免新电脑上只有一个别的 BLE 设备时被当成鼠标读电量。
BT_COMPANY_TAG = "VID&02374A"


def _bt_addr_vk_service(addr):
    """这个蓝牙地址在 BTHLEDEVICE 下的服务节点里，VID 是不是 VK

    返回 True=确认是 / False=确认不是 / None=查不到（不能据此否决，
    例如刚配对、服务节点还没枚举出来）。
    """
    try:
        import winreg
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Enum\BTHLEDEVICE")
    except Exception:
        return None
    suffix = "%012X" % int(addr)
    seen = False
    tags = devices.company_tags()
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(base, i)
            except OSError:
                break  # 枚举正常结束
            i += 1
            up = name.upper()
            if suffix not in up:
                continue
            seen = True
            if any(t in up for t in tags):
                return True
    except Exception as e:
        swallow("ble.registry.bthledevice", e)
        return None
    return False if seen else None


def _bt_address_from_registry():
    """从注册表 BTHLE 节点取已配对 BLE 设备地址（零依赖、毫秒级）

    实测这台机器上：
      HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHLE\\Dev_001122334455\\<实例>
        FriendlyName = "VK M5"
    蓝牙 HID 节点还没枚举出来时（鼠标刚拨到蓝牙、GATT 服务还没挂上），
    hid.enumerate() 里一个 bt 节点都没有，而这里照样能拿到地址，蓝牙电量
    才有得读。按 FriendlyName 匹配鼠标名，比 AEP 枚举快两个数量级。
    """
    try:
        import winreg
    except Exception:
        return None
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Enum\BTHLE")
    except Exception:
        return None
    hits = []
    names = devices.name_matches()
    i = 0
    while True:
        try:
            name = winreg.EnumKey(base, i)
        except OSError:
            break  # 枚举正常结束
        i += 1
        m = re.match(r"(?i)^dev_([0-9a-f]{12})$", name)
        if not m:
            continue
        addr = int(m.group(1), 16)
        friendly = ""
        try:
            dk = winreg.OpenKey(base, name)
            j = 0
            while True:
                try:
                    inst = winreg.EnumKey(dk, j)
                except OSError:
                    break  # 枚举正常结束
                j += 1
                try:
                    ik = winreg.OpenKey(dk, inst)
                    v, _ = winreg.QueryValueEx(ik, "FriendlyName")
                    friendly = str(v or "")
                except Exception:
                    friendly = ""
                if friendly:
                    break
        except Exception as e:
            swallow("ble.registry.friendly", e)
        up = friendly.upper()
        name_match = any(n in up for n in names)
        vk = _bt_addr_vk_service(addr)
        hits.append((addr, up, name_match, vk))
    # 名字对 + 服务记录不矛盾 -> 认定（服务记录查不到时也认，刚配对还没枚举
    # 出服务节点是正常现象）。多个鼠标都在册时，优先当前真正连上的那个。
    good = [h for h in hits if h[2] and h[3] is not False]
    if good:
        if len(good) > 1:
            picked = _BT_CONN_PICK.get("addr")
            if picked is not None and any(h[0] == picked for h in good):
                return picked          # 后台已判明谁连着，直接用
            _spawn_conn_probe([h[0] for h in good])   # 后台判定，先给第一个
        return good[0][0]
    # 名字没匹配上（系统语言/改名）：只有「系统里仅此一个 BLE 节点」且它的
    # 服务记录能证明是 VK 设备时才采用。旧版只看「只有一个节点」，新电脑上
    # 若恰好只配了一个蓝牙键盘，就会把键盘的电量当成鼠标电量。
    if len(hits) == 1 and hits[0][3] is True:
        return hits[0][0]
    # 名字对但服务记录显示不是 VK（改名/伪造）：仍按名字认，但放在最后，
    # 避免和多设备场景下的其他节点抢
    for addr, _up, name_match, _vk in hits:
        if name_match:
            return addr
    return None


def _bt_address_from_pnp():
    """设备树里还没有蓝牙 HID 节点时，从 WinRT 设备端点（AEP）找 BLE 地址

    场景：鼠标拨到蓝牙、但 Windows 尚未把 HID over GATT 服务枚举成 HID 节点，
    list_devs() 里一个 bt 节点都没有，旧版 bt_address() 直接返回 None，蓝牙
    电量就永远读不到（界面一直「蓝牙连接中」）。这里退一步用 AEP 枚举已配对
    的 BLE 设备，按名称匹配 VK M5 再取地址。
    """
    async def run():
        from winsdk.windows.devices.enumeration import DeviceInformation
        # 蓝牙 LE 的 AEP 协议 GUID
        aqs = ('System.Devices.Aep.ProtocolId:="{bb7bb05e-5972-42b5-94fc-'
               '76eaa7084d49}"')
        props = ["System.Devices.Aep.DeviceAddress", "System.ItemNameDisplay"]
        infos = await asyncio.wait_for(
            DeviceInformation.find_all_async(aqs, props), 5.0)
        best = None
        for info in infos:
            try:
                pr = dict(info.properties)
            except Exception:
                pr = {}
            name = str(pr.get("System.ItemNameDisplay") or info.name or "")
            raw = pr.get("System.Devices.Aep.DeviceAddress")
            if raw is None:
                continue
            hexs = re.sub(r"[^0-9a-fA-F]", "", str(raw))
            if len(hexs) != 12:
                continue
            up = name.upper()
            if any(n in up for n in devices.name_matches()):
                best = int(hexs, 16)
                break
        return best

    try:
        return asyncio.run(run())
    except Exception:
        return None


# v13.6：AEP 枚举实测首次要 **48 秒**（asyncio.wait_for 只兜住了 find_all，
# 大头在 winsdk 首次加载 WinRT 运行时 + 蓝牙适配器初始化）。这段耗时以前是
# 同步跑在采集线程里的，冷启动第一轮就被它按死 —— 界面几十秒不刷新、用户以为
# 「软件卡死了」，正是「关掉重开识别特别慢」的真凶。改成后台线程跑，调用方
# 立刻拿到 None，枚举完再写回地址缓存。
_BT_PNP = {"busy": False, "done_ts": 0.0}


def bt_pnp_warm():
    """后台预热 AEP 地址枚举（幂等，可随时调用）"""
    if _BT_PNP["busy"] or _BT_ADDR_CACHE["addr"]:
        return
    _BT_PNP["busy"] = True

    def _run():
        try:
            addr = _bt_address_from_pnp()
            if addr is not None:
                _BT_ADDR_CACHE["addr"] = addr
                _BT_ADDR_CACHE["ts"] = time.time()
                _dbg("bt addr via aep (bg): %012x" % addr)
        except Exception as e:
            swallow("ble.pnp.bg", e)
        finally:
            _BT_PNP["busy"] = False
            _BT_PNP["done_ts"] = time.time()

    try:
        threading.Thread(target=_run, daemon=True, name="bt-pnp").start()
    except Exception as e:
        _BT_PNP["busy"] = False
        swallow("ble.pnp.spawn", e)
    return None


def bt_conn_state(addr, ttl=8.0):
    """蓝牙链路是否已连上（1=Connected，0=断开，None=读不出）

    本机同时配了 VK M5 与 A955 两个鼠标，选地址时必须看「谁真的连上」，
    否则会一直去读另一只鼠标的电量。带短缓存，避免每轮采集都戳一次 WinRT。
    """
    if addr is None:
        return None
    now = time.time()
    hit = _BT_CONN_CACHE.get(addr)
    if hit and now - hit[1] < ttl:
        return hit[0]
    st = None
    try:
        st = _bt_link_read(addr)
    except Exception as e:
        swallow("ble.conn.read", e)
    _BT_CONN_CACHE[addr] = (st, now)
    return st


_BT_CONN_PICK = {"addr": None, "ts": 0.0}   # 后台判定出的「当前连着的鼠标」
_BT_CONN_BUSY = [False]


def _spawn_conn_probe(addrs):
    """后台判定多只鼠标里谁真的连着（一次 WinRT 探测实测约 8 秒，绝不能同步做）"""
    if _BT_CONN_BUSY[0]:
        return
    _BT_CONN_BUSY[0] = True

    def _run():
        try:
            for a in addrs:
                if bt_conn_state(a, ttl=5.0) == 1:
                    _BT_CONN_PICK["addr"] = a
                    _BT_CONN_PICK["ts"] = time.time()
                    _BT_ADDR_CACHE["ts"] = 0.0    # 让地址缓存立刻重算
                    _dbg("bt pick connected: %012x" % a)
                    break
        except Exception as e:
            swallow("ble.conn.probe", e)
        finally:
            _BT_CONN_BUSY[0] = False

    try:
        threading.Thread(target=_run, daemon=True, name="bt-conn").start()
    except Exception as e:
        _BT_CONN_BUSY[0] = False
        swallow("ble.conn.spawn", e)


def bt_address(force=False):
    """鼠标的 BLE 地址：先按 HID 路径解析，再退回 AEP 枚举（带缓存）

    HID 节点还没枚举出来时也要能拿到地址，否则蓝牙电量读不到、界面会一直
    卡在「蓝牙连接中」；同时枚举本身有开销，失败结果也缓存一段时间。
    """
    now = time.time()
    c = _BT_ADDR_CACHE
    # v20：devices.json 里显式指定了蓝牙地址就直接用（支持非 VK 型号）
    override = devices.bt_addr_override()
    if override is not None:
        c["addr"], c["ts"], c["tried"] = override, now, now
        return override
    if not force and c["addr"] and now - c["ts"] < 30:   # 30s：换鼠标（2.4G↔蓝牙）后要及时改口，不再死守 5 分钟
        return c["addr"]
    if not force and now - c["tried"] < BLE_ADDR_RETRY:
        return c["addr"]
    addr = None
    # 1) 蓝牙 HID 路径（..._REV&0810_001122334455&Col06#...）最准，且几乎零成本
    _addrs = []
    for d in list_devs():
        if not d["bt"]:
            continue
        p = _path_str(d["path"])
        m = re.search(r"_([0-9a-fA-F]{12})", p)
        if m:
            try:
                a = int(m.group(1), 16)
            except ValueError:
                continue  # 跳过非法条目
            if a not in _addrs:
                _addrs.append(a)
    if _addrs:
        # HID 节点只会给「已连上」的设备建，所以这里拿到的都是活的；多只鼠标
        # 同时在蓝牙时按枚举顺序取第一只。不做连接探测——探测一次要 8 秒，
        # 会卡死采集线程。
        addr = _addrs[0]
    # 2) 注册表 BTHLE 节点：HID 服务还没枚举时也能拿到，且几乎零成本
    if addr is None:
        addr = _bt_address_from_registry()
    # 3) 最后退回 AEP 枚举（WinRT，首次实测 48 秒）—— 只在后台跑，绝不在这里
    #    同步等：它就是冷启动「第一轮采集卡几十秒」的根源。枚举完成后地址会写回
    #    缓存，下一次读取蓝牙电量就能用上。
    if addr is None:
        bt_pnp_warm()
    c["tried"] = now
    if addr is not None:
        c["addr"], c["ts"] = addr, now
    return addr

_BLE_STATE = {"status": None, "ts": 0.0}
_BLE_FRESH_TS = [0.0]   # 最近一次 UNCACHED 成功校准的时刻
# 后台校准线程读到的「真·实时电量」。主线程为了快，通常只能拿到系统缓存，
# 缓存值会滞后好几拍（同一个鼠标 2.4G 报 100、蓝牙缓存还停在 94），
# 所以必须有一路真的把 UNCACHED 读成功，否则两边永远对不上。
_BLE_REALTIME = {"pct": None, "ts": 0.0}
_BLE_CAL_ENABLED = False
# UNCACHED 实时校准开关。实测（这台机器 + VK M5，蓝牙链路已连）：
#   gatt read 1 empty status=1 7.76s —— 实时读大多返回空，还要卡 7.76 秒，
#   而 BLE 设备的读操作是串行的，后台这一路会把主线程的 CACHED 秒回读一并堵住，
#   表现就是「拨了开关要等好久才认出来」。既然拿不到更准的数，就别付这个代价：
#   默认关闭，只靠系统缓存值 + 两通道取低对齐（口径差异下低值更保守也不会虚高）。
#   将来某台机器上 UNCACHED 能秒回时，把它打开即可。

_BLE_CAL_RUNNING = [False]


def _spawn_ble_calib(addr):
    """后台跑一次 UNCACHED 实时读，不占用界面线程

    链路没连上时这次读会卡到 WinRT 超时（约 7.8 秒），所以只能放后台，
    且同一时间只允许一路，避免线程堆积。
    """
    if not _BLE_CAL_ENABLED:
        return
    if _BLE_CAL_RUNNING[0]:
        return
    _BLE_CAL_RUNNING[0] = True

    def _run():
        try:
            asyncio.run(_gatt_read_battery(addr, force_uncached=True))
        except Exception as e:
            swallow("ble.battery.run", e)
        finally:
            _BLE_CAL_RUNNING[0] = False

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        _BLE_CAL_RUNNING[0] = False


async def _gatt_read_battery(addr, force_uncached=False):
    """WinRT GATT：读标准电池服务 0x180F / 电量特征 0x2A19

    实测两条关键事实（这台机器 + VK M5）：
      1. read_value_async(UNCACHED) 在「蓝牙链路没真正连上」时会一直等到
         WinRT 内部超时（实测单次 7.8 秒）才抛错或返回空，所以界面写「10 秒」
         根本不够 —— 这是「读电量比提示的 10 秒还久」的根因；
      2. 成功读取时 r.value 也可能是 None，旧代码直接取 .length 会抛
         AttributeError，被外层当成「设备没响应」。
    因此这里改成：先试 CACHED（系统缓存命中就秒回），再试 UNCACHED；并且
    把每次的耗时和连接状态打点，便于定位到底是链路没连上还是读得慢。

    force_uncached=True 时只走 UNCACHED —— 这是后台校准时用的：主线程那条
    CACHED 快路虽然 30 毫秒就返回，但拿到的是系统缓存（可能滞后好几分钟），
    不重读一次实时的，2.4G 与蓝牙的电量永远差一截。
    """
    from winsdk.windows.devices.bluetooth import (BluetoothLEDevice,
                                                  BluetoothCacheMode,
                                                  BluetoothUuidHelper)
    from winsdk.windows.storage.streams import DataReader
    dev = await BluetoothLEDevice.from_bluetooth_address_async(addr)
    if not dev:
        return None
    try:
        try:
            _BLE_STATE["status"] = int(dev.connection_status)
            _BLE_STATE["ts"] = time.time()
            # v12.1：顺手把主动探测的缓存也刷新掉 —— 这一次 GATT 操作本身就
            # 带回了权威的链路状态，没必要再单独探一次。
            _BT_LINK["status"] = _BLE_STATE["status"]
            _BT_LINK["ts"] = _BLE_STATE["ts"]
        except Exception as e:
            swallow("ble.gatt.bt_link", e)
        try:
            res = await dev.get_gatt_services_for_uuid_async(
                BluetoothUuidHelper.from_short_id(0x180F))
        except Exception as ex:
            _dbg("gatt services fail: %r" % (ex,))
            return None
        if getattr(res, "status", 1) != 0:
            _dbg("gatt services status=%s" % getattr(res, "status", "?"))
            return None
        for s in res.services:
            try:
                cr = await s.get_characteristics_async()
            except Exception as e:
                swallow("ble.gatt.service", e)
                continue
            for c in cr.characteristics:
                if not str(c.uuid).lower().startswith("00002a19"):
                    continue
                # 读取顺序：链路真的连着（connection_status==1）才先走 UNCACHED
                # 拿实时值；链路没连上时 UNCACHED 必然卡到 WinRT 超时（实测
                # 7.8 秒），只用系统缓存值 —— 缓存值本身来自上次真实读取，
                # 鼠标电量这种慢变量够用，而且 30 毫秒就能拿到。
                try:
                    linked = int(dev.connection_status) == 1
                except Exception:
                    linked = False
                fresh_due = time.time() - _BLE_FRESH_TS[0] > 60.0
                if force_uncached:
                    modes = (BluetoothCacheMode.UNCACHED,)
                elif linked:
                    # v19：链路已连就优先读 UNCACHED（真值），失败再退回缓存。
                    # 旧版只在「60 秒没校准过」时才试，链路明明连着却一直吃缓存。
                    modes = (BluetoothCacheMode.UNCACHED,
                             BluetoothCacheMode.CACHED)
                elif fresh_due:
                    # 链路没连上、但一分钟内还没成功校准过：仍然试一次实时读，
                    # 否则 CACHED 一旦没有缓存值就永远读不到电量（表现为一直
                    # 「蓝牙连接中」）。这步在后台线程里跑，不阻塞界面。
                    modes = (BluetoothCacheMode.CACHED,
                             BluetoothCacheMode.UNCACHED)
                else:
                    modes = (BluetoothCacheMode.CACHED,)
                for mode in modes:
                    t0 = time.time()
                    try:
                        # v19：UNCACHED 在链路半死不活时会卡到 WinRT 超时（实测
                        # 7.8 秒），这里单独压到 2.5 秒；失败立刻退回 CACHED，
                        # 不让一次蓝牙读把整轮刷新拖死。
                        if mode == BluetoothCacheMode.UNCACHED:
                            r = await asyncio.wait_for(
                                c.read_value_async(mode), 2.5)
                        else:
                            r = await c.read_value_async(mode)
                    except Exception as ex:
                        _dbg("gatt read %s fail %.2fs: %r"
                             % (mode, time.time() - t0, ex))
                        continue
                    val = getattr(r, "value", None)
                    if getattr(r, "status", 1) == 0 and val is not None \
                            and val.length >= 1:
                        v = DataReader.from_buffer(val).read_byte()
                        if mode == BluetoothCacheMode.UNCACHED:
                            _BLE_FRESH_TS[0] = time.time()
                            _BLE_REALTIME["pct"] = v
                            _BLE_REALTIME["ts"] = _BLE_FRESH_TS[0]
                        _dbg("gatt read %s ok %s %.2fs"
                             % (mode, v, time.time() - t0))
                        if 0 <= v <= 100:
                            if (mode != BluetoothCacheMode.UNCACHED
                                    and not force_uncached and fresh_due):
                                # 这轮拿的是系统缓存，值可能是几分钟前的；
                                # 甩一路后台去读实时的，下一轮好对齐
                                _spawn_ble_calib(addr)
                            return v
                    else:
                        _dbg("gatt read %s empty status=%s %.2fs"
                             % (mode, getattr(r, "status", "?"), time.time() - t0))
    finally:
        try:
            dev.close()
        except Exception as e:
            swallow("ble.gatt.close", e)
    return None


def ble_link_status(max_age=5.0):
    """最近一次 GATT 操作看到的蓝牙链路状态：1=已连接 / 0=未连接 / None=未知

    只读缓存，不主动发起连接（主动查询要几百毫秒，不能放在界面刷新里）。
    """
    if time.time() - _BLE_STATE["ts"] > max_age:
        return None
    return _BLE_STATE["status"]

# ---- 蓝牙链路状态主动探测（v12.1）----
# 与 ble_link_status 的区别：那个只读「GATT 操作留下的脚印」，8 秒没 GATT 就过期；
# 这个可以直接去问系统，不依赖有没有 GATT 操作，因此能打破 v12.0 里
# 「不知道在不在蓝牙 → 判静止 → 不 warm → 永远不知道」的死锁。
_BT_LINK = {"status": None, "ts": 0.0, "tried": 0.0}
_BT_LINK_BUSY = [False]


def _bt_link_read(addr):
    """同步读一次 BLE 链路状态：1=已连接 / 0=未连接 / None=没读到"""
    async def _run():
        from winsdk.windows.devices.bluetooth import BluetoothLEDevice
        dev = await BluetoothLEDevice.from_bluetooth_address_async(addr)
        if not dev:
            return None
        try:
            return int(dev.connection_status)
        finally:
            try:
                dev.close()
            except Exception as e:
                swallow("ble.run.close", e)
    try:
        return asyncio.run(_run())
    except Exception as ex:
        _dbg("bt link probe fail: %r" % (ex,))
        return None


def bt_link_probe(max_age=BT_LINK_FRESH, gap=BT_LINK_GAP):
    """取蓝牙链路状态，必要时后台刷新一次

    返回 (status, fresh)：
      status 1/0/None；fresh 表示这份状态是否足够新鲜 —— 只有新鲜的 0 才能
      当作「鼠标确实不在蓝牙上」的反证（旧缓存不能拿来否决）。
    刷新放在后台线程（实测单次约 0.16 秒，但采集线程不该为它停一拍），
    调用方只读缓存，立刻返回，不会阻塞采集与界面。
    """
    now = time.time()
    if now - _BT_LINK["ts"] <= max_age:
        return _BT_LINK["status"], True
    recent = (now - _BT_LINK["ts"]) <= 20.0     # 20 秒内的观测还算「看过」
    if _BT_LINK_BUSY[0] or (now - _BT_LINK["tried"]) < gap:
        return (_BT_LINK["status"] if recent else None), False
    addr = bt_address()
    if addr is None:
        return (None, False)
    _BT_LINK["tried"] = now
    _BT_LINK_BUSY[0] = True

    def _run():
        try:
            st = _bt_link_read(addr)
        except Exception:
            st = None
        finally:
            if st is not None:
                _BT_LINK["status"] = int(st)
                _BT_LINK["ts"] = time.time()
            _BT_LINK_BUSY[0] = False

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        _BT_LINK_BUSY[0] = False
    return (_BT_LINK["status"] if recent else None), False


async def _ble_read_battery_async(addr):
    """带硬超时的 GATT 读取

    实测 WinRT 的 GATT 建连在部分状态（蓝牙已断、栈正在重连）会长时间不返回，
    旧版没超时，后台蓝牙线程会永久占着 _ble_busy，之后所有蓝牙读取直接被
    跳过 —— 表现就是「必须退出软件才能重新识别」。这里加 wait_for 兜底。
    """
    try:
        return await asyncio.wait_for(_gatt_read_battery(addr), BLE_TIMEOUT)
    except Exception:
        return None


def ble_read_battery(addr):
    """同步封装：蓝牙电量（读不到返回 None，不抛异常）"""
    if addr is None:
        return None
    try:
        return asyncio.run(_ble_read_battery_async(addr))
    except Exception:
        return None


def bt_hid_battery(timeout=0.6):
    """v23：走 BLE HID 的 0x77 查询电量（和 Valkyrie Driver 同一协议）

    官方驱动的做法：output report ID 6 发 0x77，鼠标用 input report
    `06 77 <电量>` 回复。这条通道是设备当场回报，比 Windows GATT 0x2A19
    的缓存准（实测同一时刻 0x77=100、GATT=94）。

    返回 0~100 或 None；失败由调用方退回 GATT。
    """
    try:
        devs = [d for d in list_devs() if d.get("bt")]
    except Exception as e:
        swallow("ble.bt077.list", e)
        return None
    if not devs:
        return None
    # 优先鼠标 vendor collection（usage 0x0202），再试其他 0xFF55 节点
    cands = [d for d in devs if d.get("usage_page") in (0xFF55, 0xFFFF)
             and d.get("usage") == 0x0202]
    for d in devs:
        if d.get("usage_page") in (0xFF55, 0xFFFF) and d not in cands:
            cands.append(d)
    for d in cands:
        dev = None
        try:
            dev = _hid().device()
            dev.open_path(d["path"])
            dev.set_nonblocking(1)
            for _ in range(8):
                if not dev.read(64, 5):
                    break
            dev.set_nonblocking(0)
            try:
                dev.write(bytes([0x06, 0x77]))
            except Exception as e:
                _dbg("bt077 write fail: %r" % (e,))
                continue
            end = time.time() + timeout
            while time.time() < end:
                r = dev.read(64, 60)
                if not r:
                    continue
                b = bytes(r)
                if len(b) >= 3 and b[0] == 0x06 and b[1] == 0x77:
                    v = b[2]
                    if 0 <= v <= 100:
                        _dbg("bt077 ok %d" % v)
                        return v
                elif len(b) >= 3 and b[1] == 0x77:
                    v = b[2]
                    if 0 <= v <= 100:
                        _dbg("bt077 ok(no id) %d" % v)
                        return v
        except Exception as e:
            _dbg("bt077 fail: %r" % (e,))
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass  # 可忽略：幂等清理/UI 收尾
    return None
