# -*- coding: utf-8 -*-
"""采集编排：一次 read_all 产出连接方式 + 电量 + DPI。"""

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

from .ble import _BLE_FRESH_TS, _BLE_REALTIME, ble_link_status, ble_read_battery, bt_address, bt_hid_battery, bt_link_probe
from .config import BLE_ADDR_RETRY, BLE_BUSY_MAX, BLE_CACHE, BLE_RETRY_MIN, BLE_STALE_MAX, BOOT_GRACE, BT_ADDR_TTL, BT_CHECK_HINT, BT_EVID_WIN, BT_FIRST_CONN, BT_LINK_FRESH, BT_OFF_TAKEOVER, BT_PENDING_MAX, BT_UNKNOWN_GRACE, CLEAN_DEAD_N, CMD_GET_PROFILE, CROSS_BATT_GAP, CROSS_BATT_KEEP, CROSS_BATT_MAX, DEAD_CONFIRM, DEAD_TTL, DEEP_PROBE_GAP, DEEP_PROBE_GAP_DEAD, DEEP_PROBE_GAP_SOFT, DPI_FAMILIES, DPI_PRESETS, DPI_REFRESH, FAKE_CONFIRM, FAKE_PROBE_GAP_FAST, FAKE_PROBE_GAP_SLOW, FAKE_RENEW_MAX, FAKE_TTL, F7_OFF_CONFIRM, GATT_FRESH_SEC, MAX_LAYER, NOFRAME_DEAD_N, NOFRAME_PROBE_GAP, REPORT_LEN, STILL_HOLD_MAX, STILL_PROBE_GAP_FAST, USB_ALIVE_WIN, USB_BT_GRACE, USB_MISS_BT, WIRED_PIDS
from . import devices
from . import a955
from .device import _ACT_CACHE, _dev_refresh_async, _handshake, _hid, _path_pid, _path_vid, _suspect_fake, _usb_alive, _usb_ctrl_exists, deep_probe, dev_snapshot_age, enc9, find_vendor_path, list_devs, probe_usb
from .paths import _dbg, load_usb_path, save_usb_path, swallow

def warm_text_ok(now, boot_ts, warm_done):
    """v11.8：启动宽限内（且还没暖机成功）界面写「识别中…」，不报「未收到回包」。

    实机反馈「打开软件就看到未响应 / 未收到回包」，多数发生在刚启动的那几秒：
    可能 USB 侧刚通一下又断（接收器上电、GATT 抢通道），此时 _got_data 已经被
    置真，旧判据就不再给宽限，界面立刻变成报错文案。改成看「在线是否稳定过
    3 秒」（_warm_done），没稳定之前一律按识别中处理。
    """
    return (not warm_done) and (now - (boot_ts or 0.0)) < BOOT_GRACE

class Reader:
    def __init__(self):
        self.dev = None
        self.family = None          # 命中的 DPI 命令族
        self.layer = None           # 当前板载配置层（None=未探测）
        self._layer_cached = None   # 最近一次 get_profile 结果（短时缓存，省请求）
        self._layer_ts = 0.0
        self.mode = None            # 打开的通道：None=USB(有线/2.4G) / "bt"=蓝牙
        self._ble_addr = None
        self._bt_addr_next = 0.0    # 下次允许重新枚举 BLE 地址的时间点（失败冷却）
        self._bt_addr_ts = 0.0      # v16：BLE 地址缓存的时间戳（见 _bt_addr_id）
        self._ble_pct = None
        self._ble_ts = 0.0
        self._ble_busy = False
        self._ble_busy_ts = 0.0     # 后台蓝牙读取开始的时刻（用于僵死回收）
        self._ble_thread = None
        self._ble_fail = 0        # 蓝牙读失败次数（退避用，避免空转）
        self._ble_next = 0.0      # 下次允许尝试的时间点
        self.on_ble_done = None      # 后台蓝牙读完成后的回调（由 App 注入）
        self._dpi_vals = None       # 最近一次读到的档位表（节流用）
        self._dpi_ts = 0.0
        self._path = load_usb_path()  # 当前打开的 HID 控制口路径（多通道并存时用它锁定）
        # v13.10：优先用上次握手成功的路径。首次 HID 枚举会被 SetupAPI 卡住
        # 40~50 秒，那段时间设备树是空的，界面只能一直「识别中」。路径若已失效
        # （换口/换线），probe_usb 会握手失败，后续自动退回枚举。
        self._cached_path = self._path
        self._act = None            # 最近一次判活选中的 (mode, pid, path)
        self._act_ts = 0.0
        self._force_relink = False   # 下一次 read_all 是否强制重新枚举+判活
        self._usb_miss = 0           # USB/2.4G 侧连续「握手不通」的轮数（蓝牙门禁用）
        self._usb_ok_ts = 0.0        # 最近一次 USB 侧拿到真实在线证据的时刻
        # v11.6：最近一次「私有帧复核也读不到」的时刻。非 0 表示已经拿到硬证据
        # 证明鼠标不在 USB/2.4G 侧，蓝牙判定必须等它，光凭 F7 状态位不算。
        self._usb_dead_ts = 0.0
        self._usb_dead_n = 0         # 私有帧复核「连续不通」的次数（偶发一次不算数）
        self._f7_soft = 0            # F7 状态位假死（判死但私有帧通）的累计次数
        self._last_st = None         # v11.7：最近一次被证实真实的在线帧（假死期沿用）
        self._usb_alive_ts = 0.0     # v11.7：私有帧最近一次「确认在 USB 侧」的时刻
        # v11.7：空置接收器「假在线」防线。_fake_n 是私有帧复核连续不通的次数，
        # _fake_ts 非 0 表示已认定这条 USB 通道是空置接收器（锁定期内不采信 F7 在线位）
        self._fake_n = 0
        self._fake_ts = 0.0
        self._fake_renew = 0         # v11.8：锁定期续期次数（防止无限续期卡在蓝牙）
        self._bt_wait_since = 0.0    # 开始等蓝牙 GATT 建连的时刻（用于限时提示）
        self._f7_off_since = 0.0     # v13.8：链路=0 且读得到帧的起始时刻（连续 0.8 秒才算静止）
        self._f7_trust = False       # v13.8：手动「重新识别连接」后首轮直接认静止
        # v12.0：静止静默。_still_ts 非 0 表示「USB 侧读不到应答、但没有任何
        # 「鼠标挂在蓝牙」的侧面证据」，按静止处理（沿用上次电量，不报未响应）。
        self._still_ts = 0.0
        self._still_n = 0            # 连续多少轮落在静止静默上（诊断用）
        # v12.1：蓝牙链路状态「读不到」的起始时刻。非 0 表示这几轮既判不了
        # 蓝牙、也判不了静止（链路状态探测还没回来），界面写「识别中…」。
        self._bt_unk_ts = 0.0
        self._bt_unk_state = False   # 跨轮保留：当前是否处于「连接方式待确认」
        self._noevi_ts = 0.0         # v12.1：两侧都没证据的起始时刻（界面据此给操作提示）
        # v13.4：蓝牙链路明确未连（link=0 且新鲜）而 USB 侧又连续无应答的起始
        # 时刻。用来给「蓝牙接管」做纯时间兜底，解掉 v13.3 及以前的判定闭锁
        # （等蓝牙证据才放行蓝牙 → 蓝牙栈没接上就永远等）。
        self._bt_off_since = 0.0
        self._deep_ts = 0.0          # 最近一次私有帧判活复核的时刻（节流用）
        self._last_good_batt = None  # 最近一次有效电量（回包电量字段是垃圾值时兜底）
        self._last_good_batt_ts = 0.0  # 上面那个读数是什么时候拿到的（跨通道对齐用）
        self._last_batt_src = ""     # 最近一次有效电量的来源：usb / gatt
        self._ble_fresh_pct = None   # 蓝牙侧最近一次实时（UNCACHED）电量
        self._ble_fresh_ts = 0.0     # 上面那个蓝牙实时值的时刻
        # v16：电量归属指纹。换一只同型号鼠标时 VID/PID 完全一样，光靠硬件 ID
        # 分不出来，只能靠蓝牙地址（配对唯一）来认。指纹一变就必须作废旧读数，
        # 否则新鼠标会顶着上一只的电量显示（实机：新鼠标接到蓝牙上，界面仍是
        # 上一只的 94%，而系统侧这台的真值是 100%）。
        self._batt_key = ""          # 当前读数归属：""=未定 / "usb" / "bt:<addr>"
        self.batt_dev_changed = False   # 一次性标记：本轮刚换了设备（App 据此清显示）
        self._boot_ts = time.time()  # 进程内第一次采集的时刻（冷启动门禁用）
        # v13.7：本轮 F7 帧的分类（见 read_all 里 _st0 处的说明）
        self._f7_frame = False       # 本轮 F7 有没有回帧
        self._f7_on = False          # 帧自证在线（online=0 位 或 can_read=1）
        # v13.9：连续多少轮「连 F7 帧都读不到」。2.4G 静止时帧读得到（只是
        # online=0/can_read=0 的静帧），真拨到蓝牙才会彻底无帧 —— 用它给
        # USB_ALIVE_WIN 的 45 秒压制开一条失效通道，切档才不用干等 45 秒。
        self._noframe_n = 0
        self._ble_kick_ts = 0.0      # v13.9：上次为「通道已空」清掉 GATT 退避的时刻
        # v13.10：连续多少轮读到「干净死帧」（F7 回帧但 online=0 且 can_read=0）。
        # 实机实测（v13.9 验收时）：鼠标拨到蓝牙、接收器空置，F7 仍然回帧，只是内容
        # 是干净死帧 —— 所以 _noframe_n 永远归零，v13.9 那条提速通道从没生效，
        # 界面照旧被 USB_ALIVE_WIN 压满，一直停在「识别中 / 正在确认连接方式」。
        self._clean_dead_n = 0
        self._f7_suspect = False     # 可疑帧：online=1 但 can_read=0（典型=2.4G 静止）
        self._cold = True            # 还没证实过任何一条通道在线（冷启动）
        # 有线节点走「直发-应答」私有帧通道：实测有线供电时 F7 帧里
        # can_send / can_read 恒为 0（type=8），三步握手永远等不到放行，
        # 旧版因此把 DPI 判成「有线不可读」；但私有帧本身完全可用。
        self._wired_direct = False

        # v13.10：立刻在后台做一次设备树枚举预热。进程内第一次
        # hid.enumerate() 实测要 6.9 秒（SetupAPI 首次遍历），放在这里预热
        # 可以让这 6.9 秒与界面初始化并行，而不是让冷启动首轮采集替它等。
        try:
            _dev_refresh_async()
        except Exception:
            pass

    # ---------- 底层 ----------
    def _ble_fresh_ok(self, now, win=BT_EVID_WIN):
        """蓝牙侧有没有「足够新」的实时电量证据（只认 UNCACHED 读数）"""
        return (self._ble_fresh_pct is not None
                and (now - self._ble_fresh_ts) <= win)

    def _bt_evidence(self, now=None):
        """蓝牙侧有没有「鼠标此刻确实连着蓝牙」的活证据（v12.0）

        用来区分两种「USB 侧读不到应答」：
          · 鼠标拨到蓝牙、2.4G 接收器空置 → 蓝牙侧有新读数 / 链路已连接；
          · 鼠标挂在 2.4G、只是暂时没动 → 蓝牙侧毫无动静。
        只认两种硬证据：本进程里 GATT 真读到过电量（且不太旧），或最近一次
        GATT 操作看到的链路状态是「已连接」。注册表里的配对地址、设备树里配对
        过的蓝牙节点都不算 —— 它们常年存在，拿它当证据会把静止误判成空置。
        """
        now = time.time() if now is None else now
        # 谁更新就信谁：USB 侧在最近一次蓝牙读数之后又拿到过真回包，说明鼠标
        # 已经拨回 2.4G/有线了，那份蓝牙读数只能当历史，不能再用来说「鼠标在
        # 蓝牙上」（否则拨回来以后静止两分钟都会被误判成空置接收器）。
        _ble_last = max(self._ble_ts or 0.0, self._ble_fresh_ts or 0.0)
        if (self._ble_pct is not None or self._ble_fresh_pct is not None) \
                and self._usb_ok_ts and self._usb_ok_ts > _ble_last:
            return False
        # v12.0 真机修正：只认 UNCACHED 实时校准值。
        # 实测（2.4G 在用、鼠标静止）：
        #   gatt read 0 ok 100 / link=0 / ble_pct=100 / fresh=None
        # 鼠标明明挂在 2.4G，系统 GATT 照样能返回 100% —— 那是 Windows 对已配对
        # 蓝牙设备缓存的旧值，不代表鼠标此刻连着蓝牙。旧写法把这份缓存当证据，
        # 于是「静止」被判成「接收器空置」，界面立刻写「未收到回包」。
        # 缓存电量同样不算证据（上面那条日志里 ble_pct=100 就是缓存）。真正
        # 说明「鼠标在蓝牙」的是 GATT 链路此刻连着：UNCACHED 读到值，或系统
        # 报告链路已连接。真机在 2.4G 静止时这两项都是空的（link=0、
        # fresh=None），静止判定因此能生效。
        if self._ble_fresh_ok(now):
            return True
        # v13.7：本轮 F7 是可疑帧（online=1、can_read=0）时，蓝牙侧那点残留
        # 的 link=1 不算证据。实测这就是「2.4G 在用、鼠标静止」的帧；把它当
        # 空置接收器，界面就会先报「未响应」，再被 BLE 缓存电量带成
        # 「蓝牙已连接」。真切到蓝牙时 F7 是干净死帧（online=0），不受影响。
        if self._f7_suspect:
            return False
        return self._bt_state(now) == "on"

    def _bt_state(self, now=None):
        """蓝牙侧三态判定（v12.1）：on / off / unknown

        v12.0 只返回 boolean，把「确实没连蓝牙」和「还不知道连没连」压成同一个
        False，于是「不知道」被当成「不在蓝牙」判成静止，静止又掐掉了唯一能刷新
        链路状态的 GATT 操作 —— 拨到蓝牙后界面就永久停在「识别中…」，只能重启
        软件。这里把两件事分开，判定链才能自己走出死锁：

          on      ：GATT 读到过实时电量，或链路状态明确=1
          off     ：链路状态明确=0，且足够新鲜（旧缓存不算反证）
          unknown ：还没读到 —— 既不判蓝牙也不判静止，后台补一次探测
        """
        now = time.time() if now is None else now
        if self._ble_fresh_ok(now):
            return "on"
        try:
            st, fresh = bt_link_probe(BT_LINK_FRESH)
        except Exception:
            st, fresh = None, False
        if st is None:
            # 主动探测还没回来（首次约 0.16 秒），退一步看 GATT 留下的脚印
            try:
                st2 = ble_link_status(max_age=8.0)
            except Exception:
                st2 = None
            if st2 is not None:
                st, fresh = st2, True
        if st == 1:
            return "on"
        if st == 0 and fresh:
            return "off"
        return "unknown"

    def close(self):
        # 先收掉后台蓝牙线程，再关 HID 句柄：WinRT 对象活到进程退出会在解释器
        # 清理阶段被跨线程析构，实测会直接 STATUS_ACCESS_VIOLATION 崩掉
        self.join_ble(6.0)
        self._drop_dev()

    def note_f7_soft(self):
        """快探用私有帧复核证明「F7 状态位假死」时调用

        记一笔假死次数（让 read_all 把复核节流缩到 2 秒，尽快把判活纠回来），
        同时清掉「USB 侧已死」的硬证据 —— 私有帧能通就说明鼠标还在这条路上。
        """
        self._f7_soft = min(self._f7_soft + 1, 5)
        self._usb_dead_ts = 0.0
        self._usb_dead_n = 0
        # 刚复核过，别让下一轮 read_all 立刻再花 0.9 秒戳一次
        if not self._deep_ts or time.time() - self._deep_ts > DEEP_PROBE_GAP_SOFT:
            self._deep_ts = time.time()

    def _drop_dev(self):
        """只关 HID 句柄，不动蓝牙线程

        旧版 battery() 失败时直接调 close()，而 close() 里 join_ble(6.0) 最坏要
        等 6 秒 —— 采集线程被它拖住，界面刷新就从 10 秒级掉到 15 秒以上。丢句柄
        跟收蓝牙线程本来是两件事，这里拆开。
        """
        try:
            if self.dev is not None:
                self.dev.close()
        except Exception:
            pass  # 可忽略：幂等清理/UI 收尾
        self.dev = None
        # 通道换了就必须重判：切 2.4G 后若还留着有线的直连标记，
        # 私有帧会绕过握手直接发，读到的将是无效帧。
        self._wired_direct = False

    def _open(self):
        path = None
        if self.mode != "bt":
            path = self._path or find_vendor_path(self.mode)
        if self.mode != "bt" and not path:
            # USB 通道不在时不要在这里抛：上层会回退到蓝牙
            raise RuntimeError("未找到 VK M5 接收器（0x374A 厂商接口）")
        if not path:
            raise RuntimeError("未找到 VK M5 蓝牙节点")
        dev = _hid().device()
        dev.open_path(path)
        dev.set_nonblocking(1)
        self.dev = dev
        # 有线节点（PID 0xA321）自带一条「直发-应答」私有帧通道：发什么命令就
        # 立刻回什么，不需要 can_send/can_read 放行。2.4G 走原来的三步握手。
        try:
            self._wired_direct = (devices.mode_for_pid(_path_pid(path)) == "wired")
        except Exception:
            self._wired_direct = False

    def _send(self, cmd9):
        if self.dev is None:
            self._open()
        self.dev.send_feature_report(bytes([0x00]) + cmd9 + bytes(REPORT_LEN - 10))
        time.sleep(0.012)

    def _get(self):
        resp = b""
        for _ in range(6):
            try:
                r = bytes(self.dev.get_feature_report(0x00, REPORT_LEN))
            except Exception:
                r = b""
            if r and any(r[1:]):
                return r[1:]
            time.sleep(0.012)
        return resp

    def _sel_mouse(self):
        self._send(enc9([0xF6, 0x05]))

    def status(self):
        self._send(enc9([0xF7]))
        p = self._get()
        if len(p) < 10:
            return None
        dtype = p[6]
        online = (p[4] == 0) if dtype == 2 else (p[3] == 0)
        _off = devices.battery_offset_for(
            _path_vid(self._path), _path_pid(self._path))
        if _off is None or _off >= len(p):
            _off = 2 if dtype in (2, 3) else 1
        return {"can_read": p[0] == 1, "can_send": p[5] == 1,
                "battery": p[_off] if _off < len(p) else p[2],
                "online": online, "type": dtype}

    def _wait(self, pred, timeout=1.2, deadline=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if deadline is not None and time.time() > deadline:
                return None
            st = self.status()
            if st and pred(st):
                return st
            time.sleep(0.09)
        return None

    def _direct_read(self, payload, timeout=0.7):
        """有线通道：直发私有帧再取应答（不经过 can_send/can_read 门禁）

        回包首字节必须等于命令字，否则视为上一轮残留帧。有线节点会把同一条
        应答一直重放着，若只判「有非零回包」，读到的是上一次的档位表。
        """
        if self.dev is None:
            self._open()
        cmd = payload[0]
        try:
            self._send(enc9(payload) if len(payload) <= 9 else
                       bytes(list(payload) + [0] * (64 - len(payload))))
        except Exception:
            self._drop_dev()
            return None
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = bytes(self.dev.get_feature_report(0x00, REPORT_LEN))
            except Exception:
                r = b""
            if len(r) > 1 and r[1] == cmd and any(r[1:]):
                return r[1:]
            time.sleep(0.015)
        return None

    def _gate_send(self, deadline=None):
        """写入门禁：有线直连通道不需要 can_send，直接放行"""
        if self._wired_direct:
            return True
        return bool(self._wait(lambda s: s["can_send"], 1.2, deadline))

    def _request_read(self, payload, deadline=None):
        """发送命令 -> 等 24G 通道可读 -> 发 FC -> 取数据

        deadline 是整轮等待的总闸门：只有线供电时私有帧通道并不响应，若不留
        预算，一次读就会拖好几秒，把采集线程和界面刷新一起拖死。
        """
        # 有线通道：跳过 can_send/can_read 门禁，直接发命令取应答。
        # 实测（VK M5，PID 0xA321）：F7 帧 can_send=can_read=0，但 0x82 / 0x90
        # 直发即回，回包布局与 2.4G 完全一致 —— 这就是「有线读不到 DPI」的真因。
        if self._wired_direct:
            to = 0.7
            if deadline is not None:
                to = max(0.12, min(0.7, deadline - time.time()))
            return self._direct_read(payload, to)
        st = self._wait(lambda s: s["can_send"], 1.2, deadline)
        if not st:
            return None
        self._sel_mouse()
        self._send(enc9(payload) if len(payload) <= 9 else
                   bytes(list(payload) + [0] * (64 - len(payload))))
        if not self._wait(lambda s: s["can_read"], 1.5, deadline):
            return None
        self._send(enc9([0xFC]))
        time.sleep(0.02)
        return self._get()

    # ---------- 业务 ----------
    def _drop_batt_cache(self, why=""):
        """作废所有通道的电量缓存（换了鼠标，旧读数不再代表这台设备）"""
        self._last_good_batt = None
        self._last_good_batt_ts = 0.0
        self._last_batt_src = ""
        self._ble_pct = None
        self._ble_ts = 0.0
        self._ble_fresh_pct = None
        self._ble_fresh_ts = 0.0
        # 蓝牙退避一起清零：旧设备留下的失败计数会把新设备的首次 GATT 读取
        # 推到十几秒之后，用户看到的就是「换了鼠标还是半天不出数」
        self._ble_fail = 0
        self._ble_next = 0.0
        # 模块级「实时校准值」也要一起清：那是上一只鼠标的 GATT 直读结果，
        # 留着的话 _cross_pick 会当成新鲜实时值直接顶掉新设备的读数。
        try:
            _BLE_REALTIME["pct"] = None
            _BLE_REALTIME["ts"] = 0.0
            _BLE_FRESH_TS[0] = 0.0
        except Exception as e:
            swallow("reader.batt_owner.realtime", e)
        self.batt_dev_changed = True
        _dbg("batt_owner: drop cached reading (%s)" % why)
        # 只有「新归属在蓝牙侧」才值得立刻去 GATT 读一次。切到 USB 侧时反而
        # 不能催：此时蓝牙要么已经断了（白等一轮退避），要么干脆是上一只鼠标
        # 的链路，读到的是别人的数。
        if self._batt_key.startswith("bt:"):
            try:
                self.ble_warm()      # 立刻去读新设备的读数（非阻塞）
            except Exception as e:
                swallow("reader.batt_owner.warm", e)
        return None

    def _note_usb_owner(self, now=None):
        """USB/2.4G 通道真在线时登记归属

        2.4G 侧拿不到任何设备身份：接收器没序列号（HidD_GetSerialNumberString
        返回空），私有帧里也没有设备编号，所以归属只能记成 "usb"。换口/换线
        还是同一只鼠标，拿 HID 路径当指纹会在插拔后反复作废电量，界面一直
        「--」，所以这里不记路径。

        但「上一份读数来自蓝牙」这件事一定得作废：2.4G 和蓝牙是同一系列鼠标
        的两种连接方式，换一只新鼠标时它多半是从另一条通道冒出来的，旧通道
        那份读数对新设备毫无意义，留着还会被跨通道对齐拿来压低新读数。
        具体作废与否交给 _note_batt_owner 统一判（通道变了就作废）。
        """
        self._note_batt_owner("usb", "usb hit")
        return None

    def _note_batt_owner(self, key, why=""):
        """登记「当前电量属于哪台设备」；指纹变了就作废旧读数

        key 取 "usb" 或 "bt:<12位蓝牙地址>"。

        v17：判据从「蓝牙地址变了」放宽到「通道变了就作废」。原因：2.4G 通道
        没有任何可核验的设备身份（接收器无序列号、私有帧无设备编号），一旦
        读数从 2.4G 换到蓝牙（或反过来），就没有任何证据能说明这还是同一只
        鼠标 —— 而新鼠标在 2.4G / 蓝牙之间切换是常态。此时留着旧读数，唯一
        的后果就是把别人的电量顶在界面上。作废的代价只是切换瞬间短暂显示
        「读取中」，而两条通道都能在 1～3 秒内给出真实读数。

        同一通道内部的换设备：蓝牙靠地址变化识别；2.4G 侧物理上无法识别
        （同型号接收器插同一口连 Windows 实例 ID 都一样），只能靠上面的
        跨通道作废来兜。
        """
        if not key or key == self._batt_key:
            return
        old = self._batt_key
        self._batt_key = key
        if not old:
            return               # 首次建立归属：没有旧读数可作废
        _old_bt = old.startswith("bt:")
        _new_bt = key.startswith("bt:")
        if _old_bt != _new_bt:
            self._drop_batt_cache("channel switch %s -> %s (%s)"
                                  % (old, key, why))
            return None
        if _old_bt and _new_bt and old != key:
            self._drop_batt_cache("%s -> %s (%s)" % (old, key, why))
        return None

    def _bt_addr_id(self, now=None, force=False):
        """取当前配对的蓝牙地址（带 TTL）

        地址既是 GATT 读电量的入口，也是「哪只鼠标」的唯一指纹。原先是取到一次
        就永久缓存，换一只同型号鼠标后地址一直是旧的 —— 指纹不变，程序就永远
        认为还是同一台，旧电量一直沿用。这里给缓存加保质期（BT_ADDR_TTL），
        注册表直读几毫秒，过期重取开销可以忽略。
        """
        now = time.time() if now is None else now
        expired = (now - self._bt_addr_ts) >= BT_ADDR_TTL
        if force or self._ble_addr is None or expired:
            try:
                # v18：TTL 到期时必须 force —— bt_address() 自己还有 300 秒缓存，
                # 不 force 的话「每 20 秒重取」只是把旧地址再读一遍，换鼠标后
                # 蓝牙电量最多要等 5 分钟才换得过来。
                # 只有「手里已有地址、但过期了」才 force；还没有地址时保留
                # BLE_ADDR_RETRY 的失败冷却，免得蓝牙没配对时每轮起 AEP 枚举。
                addr = bt_address(force=bool(force or (expired
                                                       and self._ble_addr is not None)))
            except Exception as e:
                swallow("reader.bt_addr_id", e)
                addr = None
            self._bt_addr_ts = now
            if addr:
                self._ble_addr = addr
            elif force or expired or self._ble_addr is None:
                # 强制重取/过期重取之后还是拿不到地址：旧地址对应的设备多半
                # 已经换掉/移除了，留着只会继续读上一只的缓存，清掉更诚实。
                self._ble_addr = None
        return self._ble_addr

    def battery(self):
        try:
            st = self.status()
            if not st:
                raise RuntimeError("设备无响应")
            return st
        except Exception:
            self._drop_dev()   # 只丢句柄；close() 会 join 蓝牙线程，能卡住采集线程
            raise

    def ble_battery(self, max_age=BLE_CACHE):
        """蓝牙模式电量：优先 HID 0x77 直读，失败退回 GATT（缓存 max_age 秒）

        v23：官方 Valkyrie Driver 在蓝牙下用 output report 0x77 查询，设备回
        input report `06 77 <pct>`；实测同一时刻 0x77=100、GATT=94，所以优先
        走 0x77。只有 HID 节点不在/查询失败时才退回标准 GATT 0x180F/0x2A19。
        """
        now = time.time()
        if self._ble_pct is not None and now - self._ble_ts < max_age:
            return self._ble_pct
        pct = None
        try:
            pct = bt_hid_battery()
        except Exception as e:
            swallow("reader.bt077", e)
        if pct is not None:
            # 0x77 是设备当场回复，算实时值：登记给归属/跨通道逻辑
            _BLE_FRESH_TS[0] = time.time()
            _BLE_REALTIME["pct"] = pct
            _BLE_REALTIME["ts"] = _BLE_FRESH_TS[0]
            self._ble_pct, self._ble_ts = pct, now
            return pct
        # v16：地址带 TTL 取（换鼠标后地址会变）。旧地址对应的设备已经不在了，
        # 拿旧地址读 GATT 只会拿到失败或上一台的系统缓存值。
        self._bt_addr_id(now)
        pct = ble_read_battery(self._ble_addr)
        if pct is not None:
            self._ble_pct, self._ble_ts = pct, now
        return self._ble_pct

    def ble_warm(self):
        """后台读一次蓝牙电量

        GATT 首次建连实测要 10 秒左右（栈初始化），放在采集线程里会拖住整个
        状态刷新（界面长时间停在“-- / 未连接”）。这里丢到独立线程，读完再
        唤醒采集线程，界面全程不卡。
        """
        if self._ble_busy:
            # 僵死回收：GATT 建连在个别状态下会长时间不返回，若只靠线程自己
            # 复位 _ble_busy，一旦挂住就再也不会有第二次蓝牙读取 ——
            # 用户看到的就是「必须退出软件才能重新识别」。
            if time.time() - self._ble_busy_ts < BLE_BUSY_MAX:
                return
            self._ble_busy = False
        if time.time() < self._ble_next:
            return
        self._ble_busy = True
        self._ble_busy_ts = time.time()

        def run():
            ok = False
            try:
                ok = self.ble_battery(max_age=0) is not None
            except Exception:
                ok = False
            finally:
                if ok:
                    self._ble_fail = 0
                    self._ble_next = 0.0
                else:
                    # 非蓝牙状态时 GATT 连不上，短退避快速重试（建连阶段本来
                    # 就要试几次），别一上来就压 6~30 秒，那会让切换显得很迟钝
                    self._ble_fail = min(self._ble_fail + 1, 5)
                    self._ble_next = time.time() + min(
                        15.0, BLE_RETRY_MIN * self._ble_fail)
                self._ble_busy = False
                cb = self.on_ble_done
                if cb:
                    try:
                        cb()
                    except Exception as e:
                        swallow("reader.cb", e)

        self._ble_thread = threading.Thread(target=run, daemon=True)
        self._ble_thread.start()

    def join_ble(self, timeout=12.0):
        """等后台蓝牙线程收尾（退出前调用，避免 WinRT 对象在解释器清理时炸掉）"""
        t = self._ble_thread
        if t is not None and t.is_alive():
            t.join(timeout)

    def _cross_pick(self, pct, src):
        """v19：取消跨通道对齐 —— 2.4G/有线显示 HID 值，蓝牙显示 GATT 值

        旧版在两条通道都有新鲜读数时取较低值，导致 2.4G 明明读到 100、界面
        却顶着蓝牙那边的 94 不动（同一个鼠标两个模式各说各的）。现在各通道
        各显示各的，只保留蓝牙侧「哪只鼠标」的归属登记（换鼠标要作废旧读数）。
        返回 (用于显示的百分比, 备注)；备注恒为空。
        """
        now = time.time()
        if src == "gatt":
            fresh = (now - _BLE_FRESH_TS[0]) <= GATT_FRESH_SEC
            rt = _BLE_REALTIME
            if rt["pct"] is not None and (now - rt["ts"]) <= GATT_FRESH_SEC:
                # 后台校准线程真读到了 UNCACHED 实时值：它比这一轮的系统缓存
                # 可信，直接用它的数（缓存命中就秒回，值可能停在几分钟前）
                pct = rt["pct"]
                fresh = True
            if fresh:
                # 只有真读到 GATT 实时值，才算「现在挂在蓝牙上」的确凿证据。
                # 顺手登记这只鼠标的归属，换鼠标时上一只的旧读数才会作废。
                try:
                    if self._bt_addr_id(now):
                        self._note_batt_owner("bt:%012x" % int(self._ble_addr),
                                              "gatt read")
                except Exception as e:
                    swallow("reader.batt_owner.gatt", e)
                self._ble_fresh_pct = pct
                self._ble_fresh_ts = now
                self._last_good_batt = pct
                self._last_good_batt_ts = now
                self._last_batt_src = "gatt"
                return pct, ""
            # 不是 UNCACHED 实时值：这是 Windows 的系统缓存，界面要标「蓝牙缓存」
            return pct, "cache"
        return pct, ""

    def _decide_hold(self, now, raw_alive, fake_locked, live):
        """这一轮该不该沿用上一次的读数：假死沿用 / 静止沿用。

        返回 (raw_alive, held, hold_stale, held_stale)，纯判断、不改任何状态，
        沿用成立之后由调用方补 live 帧。

        判据用「私有帧最近一次确认在线的时刻」，不用 _f7_soft：真在线分支每轮
        都会把 _f7_soft 归零，靠它记不住假死这件事。v12.0 加的第二种沿用是静止
        沿用（见 STILL_HOLD_MAX 处的说明），它比假死沿用宽得多（分钟级），所以
        单独用 _still_ts 记时，且绝不刷新 _usb_alive_ts，否则 12 秒的假死窗口会
        被它一轮轮续下去，静止沿用就没有上限了。
        """
        held = False
        hold_stale = False
        held_stale = False          # v12.0：两种沿用都算「没有新证据」
        if not raw_alive and not fake_locked and not self._usb_dead_ts \
                and self._last_st:
            if self._usb_alive_ts and (now - self._usb_alive_ts) <= DEAD_TTL:
                raw_alive = True
                held = True
                held_stale = True
            elif (self._still_ts
                  and (now - self._still_ts) <= STILL_HOLD_MAX
                  and bool(live["path"])
                  and not self._bt_evidence(now)):
                # 接收器还插着（本轮枚举到路径）、蓝牙侧毫无动静、读不到应答
                # —— 鼠标静止。路径为空说明接收器被拔走/鼠标断电，不属于静止，
                # 直接往下报未响应。
                raw_alive = True
                held = True
                hold_stale = True
                held_stale = True
                _dbg("still: hold stale reading (%.1fs, receiver present)"
                     % (now - self._still_ts))
        return raw_alive, held, hold_stale, held_stale

    def _placeholder_out(self):
        """中性占位态：不报蓝牙、不报未响应，界面停在「识别中」

        只在「设备树快照还没就绪、缓存路径也没通上话」时用 —— 这一轮没有任何
        可靠证据，硬判就会把界面顶到蓝牙或未响应上。
        """
        _prev = getattr(self, "_last_out", None)
        if _prev:
            out = dict(_prev)
        else:
            out = {"mode": None, "pid": None, "online": False, "resp": False,
                   "battery": None, "bt": None, "type": None, "source": "",
                   "bt_pending": False, "bt_check": False, "batt_note": "",
                   "still": False, "bt_unk": True, "nosig": 0.0, "ch": True,
                   "dpi_idx": None, "dpis": None, "static": (None, None)}
        out["bt_pending"] = False
        out["pending"] = True
        return out

    # ---------- A955（VID 0x260D，2.4G / 有线）独立通道 ----------
    def _a955_dpi(self, info, table, dflt, ttl=5.0):
        """A955 档位：优先实时读设备配置内存，读不到回退内置/配置表

        实时档位是一组配置内存读（0x08 缓冲区读，3 段往返，正常约 70ms），
        没必要每轮都跑，缓存 5 秒；切档/改档后由 dpi_set 主动失效。
        接收器偶发漏帧（约 0.5 秒超时），所以失败先原地重试一次，仍失败就沿用
        上一次读到的真实值——绝不让浮窗在实时值与静态默认表之间来回跳。
        """
        now = time.time()
        key = info.get("pid")
        c = getattr(self, "_a955_dpi_cache", None)
        if c and c[0] == key and now - c[1] < ttl:
            return list(c[2]), int(c[3])
        got = None
        for attempt in range(2):
            try:
                got = a955.dpi_read(info)
            except Exception as e:
                swallow("reader.a955_dpi", e)
                got = None
            if got and got.get("stages"):
                break
        if got and got.get("stages"):
            dpis = list(got["stages"])
            idx = int(got.get("active") or 0)
            if not 0 <= idx < len(dpis):
                idx = 0
            self._a955_dpi_cache = (key, now, dpis, idx)
            return dpis, idx
        if c and c[0] == key and c[2]:
            # 沿用旧的真实值，并把缓存续一段时间，避免每轮都付两次超时
            self._a955_dpi_cache = (key, now - ttl + 2.0, list(c[2]), int(c[3]))
            return list(c[2]), int(c[3])
        if table:
            dpis = list(table)
            idx = (dflt - 1) if 0 < dflt <= len(table) else 0
            self._a955_dpi_cache = (key, now, dpis, idx)
            return dpis, idx
        return [], 0

    def _a955_out(self):
        """A955 分支：电量走 FF02 命令 / FF01 应答，DPI 取设备配置表

        命中（设备在）就返回完整 out，未命中返回 None 让主流程继续走 VK 私有帧。
        A955 的厂商通道不认 F7 帧，硬敲只会拿回空 ack，所以这里完全绕开握手。
        """
        if not devices.vendor_for(a955.VID):
            return None                     # 档案里没有 a955 协议族
        try:
            info = a955.find()
        except Exception as e:
            swallow("reader.a955_find", e)
            return None
        if not info:
            return None
        mode = a955.mode_of(info["pid"]) or "2.4g"
        table, dflt = devices.dpi_info(a955.VID, info["pid"])
        now = time.time()
        out = {"mode": mode, "pid": info["pid"], "online": True, "resp": False,
               "battery": None, "bt": None, "type": None, "source": "",
               "bt_pending": False, "bt_check": False, "batt_note": "",
               "still": False, "bt_unk": False, "nosig": 0.0, "ch": True,
               "dpi_idx": None, "dpis": None, "static": (mode, info["pid"])}
        try:
            got = a955.read_power(info)
        except Exception as e:
            swallow("reader.a955_power", e)
            got = None
        if got:
            out["resp"] = True
            out["battery"] = got["pct"]
            out["source"] = "hid"
            out["batt_note"] = "HID 直读"
            self._last_good_batt = got["pct"]
            self._last_good_batt_ts = now
            self._last_batt_src = "hid"
            self._usb_ok_ts = now
            self._usb_miss = 0
            self._batt_key = "usb"
        else:
            out["batt_note"] = "A955 未应答"
            out["still"] = True
        dpis, idx = self._a955_dpi(info, table, dflt)
        if dpis:
            out["dpis"] = dpis
            out["dpi_idx"] = idx
        out["dev_changed"] = bool(getattr(self, "batt_dev_changed", False))
        out["batt_key"] = self._batt_key
        self.batt_dev_changed = False
        self._last_out = dict(out)
        return out

    def _save_path_cache(self):
        """记住这轮握手成功的控制口路径，供下次启动跳过枚举"""
        try:
            if self._path and self._path != getattr(self, "_cached_path", None):
                save_usb_path(self._path)
                self._cached_path = self._path
                _dbg("path cache saved")
        except Exception as e:
            swallow("reader.path_cache", e)

    def read_all(self):
        """一次采集：连接方式 + 电量（有 DPI 通道时附带档位表）

        判定顺序：USB 通道（有线/2.4G）能读到在线就用它；读不到再看蓝牙 GATT
        电量能不能读到，能读到说明鼠标此刻挂在蓝牙上 —— 三模切换靠这套判定
        自动跟随，不需要用户手动刷新。
        """
        # A955（VID 0x260D）走自己的厂商通道：命中就地返回，绝不落到下面的
        # VK 私有帧判活流程（F7 帧发给 A955 只会拿回空 ack，白白拖慢首屏）。
        _a = self._a955_out()
        if _a is not None:
            return _a
        # v13.10：设备树快照还没就绪（进程内首次枚举被 SetupAPI 卡住，实测
        # 可达 33 秒）时，本轮不做任何通道判定。否则 probe_usb 拿到的是空设备
        # 树，上层会把「枚举还没回来」当成「接收器/线不见了」，一路漂到蓝牙并
        # 把界面钉在「识别中」。这里直接沿用上一轮结果，等快照就绪再判。
        if dev_snapshot_age() >= 1e8 and not self._path:
            return self._placeholder_out()

        if self._force_relink:
            # 「重新识别连接」要求重判：只作废判活缓存，让 pick_active 重新枚举
            # 设备树并逐个握手。已打开的句柄不在这里关 —— 同一路径反复
            # close/open 会偶发握手失败，一失败就被当成「USB 不通」，界面随即
            # 漂到蓝牙（旧版本每 6 秒强制重判就是这个副作用）。句柄只在路径
            # 真的变了、或连续读取失败时才关。
            self._force_relink = False
            try:
                _ACT_CACHE.update(ts=0.0, val=None)
            except Exception as e:
                swallow("reader.act_cache", e)

        live = probe_usb(self._path)
        # 设备树快照还没就绪（首次枚举被 SetupAPI 卡住），缓存路径也没握上手：
        # 这时「接收器在不在」根本无从判断，判下去就会漂成蓝牙 / 未响应，界面
        # 反而更乱。宁可再等一轮 —— 等快照就绪或缓存路径通上话再判。
        if (dev_snapshot_age() >= 1e8
                and not (live.get("alive")
                         or (live.get("st") or {}).get("online"))):
            return self._placeholder_out()
        # v13.7：把本轮的 F7 帧分类记下来，供蓝牙判定与静止判定共用。
        # 实机日志（2.4G 在用、鼠标静止、Windows 蓝牙侧配对残留）：
        #   fake_probe: F7 says online but vendor frame silent (n=3/2)
        #   bt_state=on no_bt=False link=1 ble_pct=94 fresh=None usb_ok=True
        # 这种帧是「online=1、私有帧不可读」的可疑帧 —— 鼠标就挂在 2.4G 上，
        # 只是没动。旧逻辑把它当「接收器空置」并叠加残留的 BLE 链路位，
        # 界面就写成「蓝牙已连接 94%」。反过来说，接收器真空置（鼠标拨到
        # 蓝牙）时 F7 回的是 online=0 / can_read=0 的干净死帧，两者可以分开。
        _st0 = live.get("st") or {}
        self._f7_frame = bool(live.get("st"))
        # v13.9：连帧都读不到 ＝ 这条 USB 通道真的空了（拨档到蓝牙/拔接收器）。
        # 只要还能读到帧（哪怕是 online=0/can_read=0 的静帧），就按「鼠标还在这
        # 条路上、只是没动」处理，不做「切走」的推断。
        if self._f7_frame:
            self._noframe_n = 0
        else:
            self._noframe_n = min(self._noframe_n + 1, 9)
        self._f7_on = bool(_st0.get("online") or _st0.get("can_read"))
        self._f7_suspect = (bool(_st0.get("online"))
                            and not bool(_st0.get("can_read")))
        # v13.10：干净死帧 = 读到帧、但 online/can_read 两位全 0。实机实测这就是
        # 「接收器空置、鼠标已拨到蓝牙」的帧。连续 CLEAN_DEAD_N 轮即可撤销
        # USB_ALIVE_WIN 的 45 秒压制（仍要私有帧复核确认不通才算数）。
        if self._f7_frame and not self._f7_on and not self._f7_suspect:
            self._clean_dead_n = min(self._clean_dead_n + 1, 9)
        else:
            self._clean_dead_n = 0
        static_mode, pid, _path = live["mode"], live["pid"], live["path"]
        _now = time.time()
        _raw_alive = live["alive"]
        # ---- 阶段 1：在线状态下复核私有帧（识别空置接收器 / 鼠标静止）----
        _raw_alive = self._stage_usb_online(live, _now, _raw_alive)
        # ---- 阶段 2：状态位判死后复核私有帧（含沿用判定用的锁定期标记）----
        _raw_alive, live, _fake_locked = self._stage_usb_dead_probe(
            live, _now, _raw_alive)
        # v11.7：F7 状态位已确认假死（私有帧通过过）时，两次复核之间的那几轮
        # 沿用上一帧真实数据。这些轮次本身没有新证据，若直接报「未响应」，
        # 界面就会在 2.4G 正常与未响应之间每秒抖一下。
        # 判据用「私有帧最近一次确认在线的时刻」，不用 _f7_soft：真在线分支每轮
        # 都会把 _f7_soft 归零，靠它记不住假死这件事。
        # v12.0 加第二种沿用：静止沿用（见 STILL_HOLD_MAX 处的说明）。它比假死
        # 沿用宽得多（分钟级），所以单独用 _still_ts 记时，且绝不刷新 _usb_alive_ts，
        # 否则 12 秒的假死窗口会被它一轮轮续下去，静止沿用就没有上限了。
        _raw_alive, _held, _hold_stale, _held_stale = self._decide_hold(
            _now, _raw_alive, _fake_locked, live)
        if _held:
            live = dict(live, alive=True, st=self._last_st)
        # 通道变了（插拔线 / 切 2.4G / 换口）：锁定新节点，旧的档位表作废，
        # 否则会拿上一条通道的缓存盖住真实档位
        if _path and _path != self._path:
            self._path = _path
            self._drop_dev()
            self._dpi_vals, self._dpi_ts = None, 0.0
            self.family, self.layer = None, None
        out = {"mode": static_mode, "pid": pid, "online": False, "resp": False,
               "battery": None, "bt": None, "type": None, "source": "",
               "bt_pending": False, "bt_check": False, "batt_note": "", "still": False,
               "bt_unk": bool(getattr(self, "_bt_unk_state", False)),
               "nosig": 0.0,
               "ch": True,
               "dpi_idx": None, "dpis": None, "static": (static_mode, pid)}
        # ---- 阶段 3：USB 侧真在线时填充读数 ----
        if _raw_alive:
            self._stage_usb_hit(out, live, static_mode, _held_stale, _hold_stale)
        else:
            # 通道在（接收器/线插着）但鼠标不在这条路上：关掉句柄并记一次失败，
            # 让下面的蓝牙判定能接上。句柄留着只会一直拿到接收器的假回包，
            # 这就是「蓝牙模式下还识别成 2.4G、非要重启软件」的根源。
            self.mode = None
            self._usb_miss += 1
            self._drop_dev()
            if self._usb_miss >= USB_MISS_BT:
                # 连续几轮都握手不通：旧档位表作废，避免切回后拿错通道的缓存
                self._dpi_vals, self._dpi_ts = None, 0.0
        # ---- 阶段 4：蓝牙判定门禁 ----
        if not out["resp"]:
            self._stage_bt(out, pid)
        # ---- 阶段 5：档位表 ----
        if (out["resp"] and out["mode"] in ("wired", "2.4g")) or \
                (out["mode"] == "bt" and out["battery"] is not None):
            self._stage_dpi(out)
        # ---- 阶段 6：显示口径（静止 / 通道在否 / 无证据时长）----
        self._stage_evidence(out, live, _now)
        # v16：把「本轮刚换了设备」的标记一次性带出去，App 据此立即清掉界面上的
        # 旧电量（不能等下一轮读到新值，中间那段就是在骗人）。
        out["dev_changed"] = bool(self.batt_dev_changed)
        out["batt_key"] = self._batt_key
        self.batt_dev_changed = False
        self._last_out = dict(out)   # v13.10：设备树未就绪时用它顶一轮
        return out

    # ---- read_all 的阶段方法：仅为可读性拆分，行为与 v12.3 单函数一致 ----
    def _stage_usb_online(self, live, now, raw_alive):
        """阶段 1：F7 帧说在线时，用私有帧核实「真在线 / 空置接收器 / 鼠标静止」"""
        # ---- v11.7：在线状态下也复核私有帧，识别「空置接收器假在线」----
        # F7 帧里的位两方向都骗过人：既会假死报 0（真在线，v11.6），也会
        # 假活报 1（接收器空置，只剩上一次的旧电量）。所以真假一律看私有帧
        # 有没有应答：真挂在这条路上必通，接收器空置实测 1 秒内也读不到。
        _pp = live["path"] or self._path
        if self._fake_ts:
            # 已认定过空置：锁定期只保证「不盲信 F7 在线位」，不等于「不再复核」。
            # v11.8 修正（实机反馈：2.4G→蓝牙→拨回 2.4G 后界面一直卡在蓝牙）：
            # 旧写法在锁定期内一律按不在线，到期复核万一又读不到就再续期 10 秒，
            # 于是一条读不通的路径（或一次打开失败）就能把界面永久钉在蓝牙上，
            # 只能重启软件。现在改成分级处理：
            #   · 帧自证可读（can_read=1，与「接收器空置」直接矛盾且比旧结论新鲜）
            #     → 当场作废空置结论，立刻回 2.4G；
            #   · 其余按 1.5 秒节流做私有帧复核，通了立刻回 2.4G；
            #   · 续期有上限，超了就不再续期，放回正常判定链重新评估。
            if (live["st"] or {}).get("can_read"):
                self._fake_ts = 0.0
                self._fake_n = 0
                self._fake_renew = 0
                _dbg("fake_probe: %s real again (can_read set)" % _pp)
            elif not self._bt_evidence(now):
                # v12.0：帧说不可读、蓝牙侧也毫无动静 —— 这是鼠标静止，
                # 不是空置接收器。把空置结论连同硬证据一并作废，回到正常
                # 判定链；否则「拨过一次蓝牙再拨回 2.4G」之后，只要鼠标
                # 一静止，这里就会一轮轮续期硬证据，界面隔几秒报一次
                # 「未收到回包」，正是用户这次反馈的现象。
                self._fake_ts = 0.0
                self._fake_n = 0
                self._fake_renew = 0
                self._usb_dead_ts = 0.0
                self._usb_dead_n = 0
                if not self._still_ts:
                    self._still_ts = now
                self._still_n += 1
                _dbg("still: %s clear fake-lock (no bt evidence)" % _pp)
            elif now - self._deep_ts >= (
                    FAKE_PROBE_GAP_SLOW if self._fake_renew > FAKE_RENEW_MAX
                    else FAKE_PROBE_GAP_FAST):
                self._deep_ts = now
                if deep_probe(_pp):
                    self._fake_ts = 0.0
                    self._fake_n = 0
                    self._fake_renew = 0
                    # 私有帧通了：USB 侧没死，硬证据一并作废，别让蓝牙门禁
                    # 再拿 12 秒内的旧证据把界面留在蓝牙
                    self._usb_dead_ts = 0.0
                    self._usb_dead_n = 0
                    self._usb_alive_ts = now
                    _dbg("fake_probe: %s real again (vendor frame alive)"
                         % _pp)
                else:
                    self._fake_renew += 1
                    self._fake_ts = now        # 续期：仍是空置接收器
                    if self._fake_renew > FAKE_RENEW_MAX:
                        # 复核连续读不到太多次：不再无限续期硬证据，转成
                        # 「保守维持 + 慢速复核」——继续不采信 F7 在线位（不会
                        # 显示假在线），但硬证据过期后蓝牙门禁自然失效，界面
                        # 最多落到「未响应」，而不是永久停在蓝牙；私有帧一通
                        # 立刻恢复 2.4G（见上面的到期/节流复核分支）。
                        _dbg("fake_probe: %s renew limit, slow recheck" % _pp)
                    else:
                        # 硬证据跟着续期。锁定期里的复核本身就是一次「私有帧
                        # 还是不通」的新证据，不续期的话 DEAD_TTL 一到，蓝牙
                        # 门禁会误以为 USB 侧从没死过，界面会闪回「未响应」。
                        self._usb_dead_ts = now
                        self._usb_dead_n = max(self._usb_dead_n, DEAD_CONFIRM)
                        self._usb_miss = max(self._usb_miss, USB_MISS_BT)
                    raw_alive = False
            else:
                raw_alive = False
        elif _pp and raw_alive:
            _suspect = _suspect_fake(live["st"])
            # 帧内位自相矛盾（online 报真、can_read 报不可读）时收紧复核间隔，
            # 其余正常在线状态只做低频兜底，不占实时读取的时间。
            # 矛盾的帧一律「先证后信」：第一次立刻戳一次私有帧（多花 0.9 秒，
            # 只在可疑帧上发生），拿到不通的结论后按 1.5 秒快跑到确认，
            # 这样界面不会被接收器回的旧缓存（电量恒 100）钉成「2.4G 已连接」。
            if _suspect and self._bt_evidence(now):
                _gap_f = FAKE_PROBE_GAP_FAST if self._fake_n else 0.0
            else:
                # v12.0：可疑帧 + 蓝牙侧毫无动静，多半是鼠标静止。别按 1.5 秒
                # 一次去戳 0.9 秒的私有帧试探（那会把采集本身拖慢，还每次
                # 都认定「读不到」），退回低频兜底复核。
                _gap_f = FAKE_PROBE_GAP_SLOW
                if not self._still_ts:
                    # 静止起点按「第一次拿不到真回包」记，不等确认门槛：
                    # 在线时采集间隔 30 秒，早超过 DEAD_TTL，等门槛再记就会
                    # 让界面先报一轮「未收到回包」。
                    self._still_ts = now
            if now - self._deep_ts >= _gap_f:
                self._deep_ts = now
                if deep_probe(_pp):
                    self._fake_n = 0        # 私有帧通：位再矛盾也按真在线
                else:
                    self._fake_n += 1
                    _dbg("fake_probe: %s F7 says online but vendor frame "
                         "silent (n=%d/%d)"
                         % (_pp, self._fake_n, FAKE_CONFIRM))
                    if _suspect and self._bt_evidence(now):
                        # 蓝牙侧确有活证据：这就是空置接收器，当轮不按在线显示
                        raw_alive = False
                        # 私有帧刚被证明读不到，就不能再拿「上一帧真在线」
                        # 去沿用（v11.6 的假死沿用分支）。否则这轮又会被
                        # 救回成「2.4G 已连接 100%」，空置接收器的旧缓存
                        # 就这么重新爬上界面。
                        # v11.9：可疑帧（online=1 但 can_read=0）本身就是
                        # 「接收器可能空置」的强信号，这里保持严格——一次
                        # 读不到就当轮退出显示。用户报的「2.4G 模式下文字
                        # 一直跳」不是这条路径：真挂在 2.4G 时 F7 回的是
                        # 可读帧，偶发丢帧表现为整帧读不到（online=0），
                        # 走下面的判死复核，那段已改成软失败沿用。
                        # v12.0：上加 bt evidence 门禁——蓝牙侧毫无动静时
                        # 这不是空置，而是鼠标静止，照常沿用（见下面的 else）
                        self._usb_alive_ts = 0.0
                    if self._fake_n >= FAKE_CONFIRM:
                        if self._bt_evidence(now):
                            # 连续几次连私有帧都读不到：这帧 F7 只是空置接收器
                            # 回的旧缓存（电量是上次的值），蓝牙侧又有活证据，
                            # 说明鼠标确实拨到蓝牙了，不在 2.4G 上
                            self._fake_ts = now
                            self._fake_renew = 0
                            raw_alive = False
                            # 同步写成 v11.6 的硬证据：蓝牙门禁与显示护栏都认它，
                            # 蓝牙接管不必再多等一轮，界面也不会先闪「2.4G 100%」
                            self._usb_dead_ts = now
                            self._usb_dead_n = max(self._usb_dead_n, DEAD_CONFIRM)
                            self._usb_miss = max(self._usb_miss, USB_MISS_BT)
                            self._still_ts = 0.0
                            self._still_n = 0
                            _dbg("fake_probe: %s receiver idle, not online" % _pp)
                        else:
                            # v12.0：蓝牙侧没有任何活证据 —— 这帧「online 报真、
                            # can_read 报不可读」同样可以是鼠标静止的常态。
                            # 不推翻在线结论，按静止沿用；清掉可疑计数，避免
                            # 静止久了被一次次累计到确认门槛。
                            self._fake_n = 0
                            if not self._still_ts:
                                self._still_ts = now
                            self._still_n += 1
                            _dbg("still: %s suspect frame but no bt evidence, "
                                 "hold" % _pp)
        return raw_alive

    def _stage_usb_dead_probe(self, live, now, raw_alive):
        """阶段 2：状态位判死后按节流间隔复核私有帧

        返回 (raw_alive, live, fake_locked)。fake_locked 是复核前算出的空置
        锁定期标记，调用方的沿用判定必须用同一个值，不要重算。
        """
        # 「确认不在 USB 侧」＝私有帧复核连续 N 次都读不到。偶发一次读不到
        # （2.4G 丢帧、接收器忙于转发）不足以把界面切到蓝牙，必须连续。
        _dead_need = 1 if self._cold else DEAD_CONFIRM
        _dead_ok = self._usb_dead_n >= _dead_need
        # 判死复核的节流间隔（v11.6）：
        #   · 发现 F7 位假死、或只失败过一两次还没「确认」时 → 2 秒，尽快把结论纠回来
        #   · 已确认鼠标不在 USB 侧（挂蓝牙）→ 5 秒复核一次，持续印证即可
        #   · 其余情况 → 5 秒
        _unsure = (self._f7_soft > 0
                   or 0 < self._usb_dead_n < _dead_need)
        _gap = (DEEP_PROBE_GAP_SOFT if _unsure
                else (DEEP_PROBE_GAP_DEAD if _dead_ok
                      else DEEP_PROBE_GAP))
        # v13.9：连 F7 帧都读不到时，「确认不通」这一步没必要再按 2~4 秒的
        # 节奏磨 —— 拨档切到蓝牙后通道本来就没有任何应答，越早把 dead_n 抬到
        # 门禁值，蓝牙判定就越早放行。1 秒一探仍是几毫秒级的握手，不会加重
        # HID 争用（比 v11.x 的 2 秒档还保守）。
        if self._noframe_n >= 1:
            _gap = min(_gap, NOFRAME_PROBE_GAP)
        # v12.0：已判定鼠标静止且还挂着时，别 2 秒敲一次 —— 静止期间本来就没有
        # 应答，反复试探只会加重接收器负担。10 秒复核一次足够及时发现「鼠标
        # 已拨到蓝牙/接收器被拔」。
        # v12.0：通道已经不在了（接收器拔走/鼠标断电）就不该按静止拉长间隔，
        # 否则 DEAD_TTL 的 12 秒沿用窗口会一轮轮被清掉，界面迟迟不肯切蓝牙。
        try:
            _ch_here = bool(live["path"]) or bool(_usb_ctrl_exists())
        except Exception:
            _ch_here = bool(live["path"])
        if not _ch_here:
            self._still_ts = 0.0
            self._still_n = 0
            self._bt_unk_state = False
            self._bt_unk_ts = 0.0
        if (self._still_ts and _ch_here and self._noframe_n == 0
                and (now - self._still_ts) <= STILL_HOLD_MAX):
            # v12.1：静止期间也允许更快的复核 —— 只要蓝牙链路状态不是明确的
            # 「没连」（unknown / 已连接），就说明鼠标随时可能刚拨到蓝牙。为省
            # 几次试探而把复核拉到 10 秒，用户看到的就是「拨完还识别中一大截」。
            try:
                _st_slow = self._bt_state(now) == "off"
            except Exception:
                _st_slow = True
            # v13.4：即便蓝牙链路明确「没连」也不再用 10 秒慢探。拨档切到蓝牙后
            # 这条 2.4G 通道本来就没有应答了，慢探会让「发现通道已死」再拖 10 秒，
            # 叠加蓝牙栈建连的十几秒，体感就是「拨完界面一直不动」。4 秒一探仍远
            # 低于会加重 HID 争用的 2 秒档。
            _gap = max(_gap, STILL_PROBE_GAP_FAST if _st_slow
                       else DEEP_PROBE_GAP_SOFT)
        # v11.7：已认定空置接收器的锁定期内，不在这里再探一次（结论由上面那段
        # 的到期复核统一给出），否则两处复核会互相推翻，界面来回跳。
        # v13.10：干净死帧说明这条 USB 通道已经没人应答（2.4G 静止回的是可疑帧），
        # 把复核提速到 1 秒，尽快把 _usb_dead_n 抬到门禁值；否则上面的静止沿用
        # 会把间隔撑到 4~5 秒，叠在 FAKE_TTL 的 10 秒锁定后面，用户看到的就是
        # 拨完档之后「识别中」一直挂着。
        if self._clean_dead_n >= 1:
            _gap = min(_gap, NOFRAME_PROBE_GAP)
        _fake_locked = bool(self._fake_ts) and (now - self._fake_ts < FAKE_TTL)
        if not raw_alive and not _fake_locked and now - self._deep_ts >= _gap:
            # 状态位判死后复核一次私有帧：真的通就说明是 F7 帧状态位异常，
            # 不能据此把界面切到蓝牙（否则 2.4G 用着用着会漂成蓝牙）。
            self._deep_ts = now
            # v13.6：静止沿用期间设备本来就不会应答，每轮把 0.9 秒超时等满
            # 纯属浪费（采集线程被占，界面刷新点下去也要排队）。静止窗口内
            # 用短超时复核，够判断「私有帧是不通」这件事了。
            _dp_to = 0.45 if (self._still_ts
                              and _ch_here
                              and now - self._still_ts <= STILL_HOLD_MAX) else None
            # v13.8：冷启动/重识别首轮只有 0.45 秒的预算。冷启动时多半是鼠标
            # 静止、私有帧根本不会应答，等满 0.9 秒纯属让「识别中」多挂半秒。
            if _dp_to is None and self._cold:
                _dp_to = 0.45
            if deep_probe(live["path"] or self._path,
                          **({"timeout": _dp_to} if _dp_to else {})):
                self._f7_soft = min(self._f7_soft + 1, 5)
                self._usb_dead_ts = 0.0   # 私有帧通着：USB 侧没死，别当硬证据
                self._usb_dead_n = 0
                # v11.7：私有帧既然通，之前「空置接收器」的结论一并作废，
                # 否则两套复核会各说各话（同步清理，避免锁定期残留）
                self._fake_ts = 0.0
                self._fake_n = 0
                self._bt_off_since = 0.0   # v13.4：私有帧通＝鼠标还在这条路上
                self._usb_alive_ts = now   # 私有帧确认在线，假死期可沿用
                st2 = _handshake(live["path"] or self._path) or {}
                st2.setdefault("online", True)
                live = dict(live, alive=True, st=st2)
                raw_alive = True     # v11.7：救回来的在线结论要写回，否则被下面绕过
                _dbg("deep_probe: %s alive by vendor frame" % live["path"])
                _ACT_CACHE.update(ts=time.time(),
                                  val=(live["mode"], live["pid"], live["path"]),
                                  alive=True)
            else:
                # 私有帧也读不到：比 F7 状态位更有说服力（两套通道都没人应答）。
                # 记下这份硬证据（蓝牙判定必须等它），连续够了才把失败计数提到
                # 门禁值，界面第一时间就写「蓝牙连接中」，不用先闪「2.4G 未响应」。
                self._usb_dead_n += 1
                self._f7_soft = 0
                # v12.0：「蓝牙侧毫无动静」＝ 鼠标还在 2.4G/有线这条路上，只是暂时
                # 静止没上报。静止起点要在第一轮就记下：在线时采集间隔 30 秒，早已
                # 超过 DEAD_TTL 的 12 秒假死沿用窗口，等确认门槛（2 轮）再记，界面
                # 第一次读不到就会直接写「未收到回包」—— 用户这次反馈的就是这个。
                # v12.0：接收器/线不在了（拔走、鼠标断电）就不是「静止」，不能
                # 记静止起点，否则会按静止沿用把它一直当成在线、迟迟不肯切蓝牙。
                try:
                    _ch_present = bool(live["path"]) or bool(_usb_ctrl_exists())
                except Exception:
                    _ch_present = bool(live["path"])
                # v12.1：三态判定。unknown（链路状态还没读到）既不能算「在蓝牙」，
                # 也不能算「不在蓝牙」—— 后者会把「刚拨到蓝牙、GATT 还没建连」判成
                # 静止，而静止又会掐掉唯一能刷新链路状态的 GATT 操作，界面就永久
                # 停在「识别中…」（v12.0 实机反馈：拨到蓝牙后只有重启软件才恢复）。
                _bt_st = self._bt_state(now)
                _bt_unk = (_bt_st == "unknown") and _ch_present
                if _bt_unk:
                    if not self._bt_unk_ts:
                        self._bt_unk_ts = now
                        # 只在刚进入「待确认」时催一次 GATT：链路状态的主力是
                        # bt_link_probe（0.16 秒一次），这里只是顺手把电量也排上队，
                        # 每轮都催会把蓝牙线程占满。
                        try:
                            self.ble_warm()
                        except Exception as e:
                            swallow("reader.ble_warm", e)
                    if (now - self._bt_unk_ts) > BT_UNKNOWN_GRACE:
                        # 等了这么久还是读不到链路状态（蓝牙栈异常 / 探测一直失败）：
                        # 不再空等。USB 侧私有帧已连续读不到，就按「可能在蓝牙」放行，
                        # 界面写「蓝牙连接中」并持续重试 —— 总好过永久「识别中」；
                        # 否则退回静止处理（v12.0 行为）。
                        _bt_st = ("on" if self._usb_dead_n >= _dead_need
                                  else "off")
                        _bt_unk = False
                        _dbg("bt_state: unknown %.1fs, give up -> %s"
                             % (now - self._bt_unk_ts, _bt_st))
                else:
                    self._bt_unk_ts = 0.0
                _no_bt = (_bt_st == "off") and _ch_present
                # v12.1：「待确认」状态要跨轮保留。判定只在复核块里跑（节流 2 秒），
                # 若每轮重新从 False 起算，两次复核之间的轮次又会掉回静止沿用，
                # 界面在「正在确认连接方式」与「鼠标静止」之间来回跳。
                self._bt_unk_state = bool(_bt_unk)
                try:
                    _ls = ble_link_status(max_age=8.0)
                except Exception:
                    _ls = -1
                _dbg("bt_state=%s no_bt=%s link=%s ble_pct=%s fresh=%s usb_ok=%s"
                     % (_bt_st, _no_bt, _ls, self._ble_pct, self._ble_fresh_pct,
                        bool(self._usb_ok_ts)))
                if _no_bt:
                    if not self._still_ts:
                        self._still_ts = now
                        _dbg("still: %s silent but receiver present, hold "
                             "(bt link off)" % (live["path"] or self._path))
                    self._still_n += 1
                if self._usb_dead_n >= _dead_need:
                    if _bt_st == "on":
                        # 连续多轮都读不到，且蓝牙侧确有活证据（GATT 读到过电量 /
                        # 链路已连接）—— 这才算「确认不在 USB 侧」的硬证据，
                        # 此时才结束假死沿用、抬起蓝牙门禁计数。
                        self._usb_dead_ts = now
                        self._usb_alive_ts = 0.0
                        self._usb_miss = max(self._usb_miss, USB_MISS_BT)
                        self._still_ts = 0.0
                        self._still_n = 0
                        self._bt_off_since = 0.0
                    elif _bt_unk:
                        # 还不知道鼠标在不在蓝牙：不写硬证据、也不把计数压回去，
                        # 等这一两轮探测回来（约 0.2 秒）就会有结论。
                        self._usb_dead_n = _dead_need
                    else:
                        # v13.6：链路明确=0（蓝牙此刻根本没连上）→ USB 侧静默
                        # **绝不能**推断成「鼠标拨到蓝牙了」。
                        # 实机日志（v13.5，鼠标挂在 2.4G 上静止）：
                        #   bt_state=off link=None ble_pct=100 fresh=None usb_ok=True
                        #   bt_takeover: usb silent 125.7s with link=off
                        #   deep_probe: ... dead (vendor frame silent, n=2/2)
                        # VK M5 在 2.4G 上不动时 F7 回 online=0、私有帧也不应答，
                        # 设备侧与「接收器空置」完全同形；v13.4 的 20 秒时间兜底
                        # 于是把静止判成拨档，界面显示「蓝牙已连接 100%」—— 那个
                        # 100% 还是 Windows 对已配对设备缓存的旧 GATT 值。
                        # 蓝牙现在只认正向证据（link=1 / GATT 实时读数），静默一律
                        # 按静止沿用；真拨到蓝牙但系统没连上时由「迟迟无回包」提示
                        # 引导用户查配对，不再靠猜。
                        self._bt_off_since = 0.0
                        self._usb_dead_n = _dead_need
                        if not self._still_ts:
                            self._still_ts = now
                            self._still_n += 1
                # v11.9：未确认前（偶发一两轮私有帧没应答，2.4G 丢帧很常见）
                # 只累计次数，不写硬证据、不清沿用标记。旧写法第一轮失败就写
                # _usb_dead_ts 并清 _usb_alive_ts，界面当轮立刻变「未响应」，
                # 下一轮帧又正常跳回「2.4G 已连接」——这就是实机反馈的抖动。
                _dbg("deep_probe: %s dead (vendor frame silent, n=%d/%d)"
                     % (live["path"] or self._path,
                        self._usb_dead_n, _dead_need))
        return raw_alive, live, _fake_locked

    def _stage_usb_hit(self, out, live, static_mode, held_stale, hold_stale):
        """阶段 3：USB / 2.4G 通道真在线 —— 写电量、来源与沿用标记"""
        # v16：USB 侧按「这只鼠标是谁」登记 —— 上一份读数若来自另一条通道
        # （蓝牙），这里会顺手作废它（见 _note_usb_owner）。
        # v17：静止沿用轮不算证据 —— 那是上一轮真帧的复制品，拿它去判「现在
        # 挂在 2.4G 上」会在通道判定来回摇的时候把电量反复清成「读取中」。
        if not held_stale:
            self._note_usb_owner()
        if not held_stale:
            # 确认真在线之后才清零：v11.7 的空置接收器结论要靠这份硬证据压住
            # 蓝牙门禁，清早了会被下一轮当成「USB 从没死过」而挡住蓝牙接管。
            # v12.0：静止沿用（hold_stale）不算「真在线」，这几项一概不动，
            # 也不能刷新 _usb_alive_ts，否则静止沿用会一轮轮续下去没有上限。
            self._f7_soft = 0
            self._usb_dead_ts = 0.0
            self._usb_dead_n = 0
            self._still_ts = 0.0
            self._still_n = 0
            self._bt_off_since = 0.0   # v13.4：真在线，撤销蓝牙接管计时
        # 握手已经拿到真实在线证据，直接用这一帧，不再多跑一次 F7 往返
        self.mode = None
        st = live["st"] or {}
        if st and not held_stale:
            self._last_st = st     # 留一份真实帧，供 F7 假死期沿用
        if _usb_alive(st) and not held_stale:
            self._usb_alive_ts = time.time()
        if not held_stale:
            # 「最近一次真实在线」只能由真回包刷新：静止沿用期间若也刷新，
            # 蓝牙门禁的 grace 就永远走不完，鼠标真拨到蓝牙后会迟迟不接管。
            self._usb_ok_ts = time.time()
            # v13.10：把这轮握手成功的控制口路径落盘。进程内首次 HID 枚举会被
            # SetupAPI 卡住（实机实测 40~50 秒），那段窗口里设备树快照一直是空，
            # read_all 只能返回「识别中」占位 —— 用户看到的就是「打开后一直
            # 识别中、读不到鼠标状态」。有了上次成功的路径，下一轮直接用句柄
            # 握手，不再等枚举。换口/换线时缓存路径会握手失败，自动退回枚举。
            self._save_path_cache()
        self._cold = False
        if not held_stale:
            # 成功：清零失败计数，并扔掉蓝牙残留电量。否则在蓝牙上读到的
            # 100% 会留在缓存里，USB 侧一丢帧就被它盖住，界面变成
            # 「蓝牙 100% / 蓝牙连接中」，而鼠标明明挂在 2.4G 上。
            self._usb_miss = 0
            self._ble_pct = None
            self._ble_fail = 0
            self._ble_next = 0.0
        self._bt_wait_since = 0.0
        batt = st.get("battery")
        dev_on = bool(st.get("online"))
        if batt is not None and 0 < batt <= 100:
            self._last_good_batt = batt
            # 只有这一帧能证明鼠标真的挂在这条通道上（can_read / online 位），
            # 才把它当成「实时基准」，供蓝牙通道的缓存值做对齐；接收器空置时
            # 回的那一帧电量是上一次的旧值（实测恒定 100），不能拿去校正蓝牙
            # 读数，否则蓝牙模式的电量会被一个假的 100% 钉死。
            if (_usb_alive(st) or not self._last_good_batt_ts) \
                    and not held_stale:
                self._last_good_batt_ts = time.time()
                self._last_batt_src = "usb"
        elif not dev_on:
            # 回包里的电量字段在设备未真正在线时是垃圾值（实测报 0），
            # 界面会显示「在线 / 0%」。这种就沿用上一次有效读数。
            batt = self._last_good_batt
        tag = ""
        if batt is not None and 0 < batt <= 100:
            # 与蓝牙侧对齐：两条通道口径不同（2.4G 粗档 100 / 蓝牙细档 94），
            # 统一成一个数，否则拨一下开关数字就变。
            batt, tag = self._cross_pick(batt, "usb")
        out.update(resp=True, online=True, battery=batt,
                   type=st.get("type"), source="usb", dev_online=dev_on,
                   batt_note=tag,
                   mode=(static_mode if static_mode != "unknown" else "2.4g"))
        if hold_stale:
            # v12.0：静止沿用。数据是上次真实读数的，界面照常显示在线，
            # 但尾行要标出读数时刻，而不是让用户以为被实时盯着。
            out.update(resp=True, online=True, stale=True,
                       stale_ts=self._last_good_batt_ts or 0.0)
            # 静止期间不去催蓝牙读数：GATT 常从系统缓存里返回一个旧电量，
            # 那会被当成「鼠标在蓝牙上」的活证据，把静止误判成空置接收器
            # （界面随即跳成蓝牙/未响应，正是不该出现的那种提示）。鼠标真
            # 被拨到蓝牙时，ble_link_status 的链路状态会先变，足以接管。
        return None

    def _stage_bt(self, out, pid):
        """阶段 4：USB 侧无回包时的蓝牙门禁与 GATT 电量"""
        if not out["resp"]:
            now = time.time()
            # 蓝牙判定门禁：USB/2.4G 侧必须「连续多轮握手都不通」，且距最近一次
            # 真实在线已超过 USB_BT_GRACE，或者设备树里根本没有 USB/2.4G 控制口
            # （真的只挂着蓝牙），才允许按蓝牙判。VK M5 偶发丢一两帧很常见，
            # 单轮失败就翻成蓝牙，界面会在 2.4G 与蓝牙之间反复横跳。
            idle = now - self._usb_ok_ts if self._usb_ok_ts else 1e9
            # 冷启动放宽：程序刚打开、从来没有任何一轮证实过在线时，「2.4G 偶发
            # 丢帧被误判成蓝牙」这个顾虑并不成立（那要建立在刚才确实在线过），
            # 而鼠标此刻若挂在蓝牙上，GATT 建连本来就要十几秒；若照样要求连续
            # 2 轮失败 + 2.5 秒 grace，界面开头几秒会先报「未收到回包」，紧接着
            # 才转「蓝牙连接中」，用户看到的就是「打开程序报错」。
            # 所以冷启动降到 1 轮、无 grace：握手一失败就立刻去做蓝牙判定。
            miss_need = 1 if self._cold else USB_MISS_BT
            grace_need = 0.0 if self._cold else USB_BT_GRACE
            # v11.6 硬证据门禁：除了「失败够多轮 + 过了 grace」，还必须有一份
            # 新鲜且被连续证实的「私有帧也读不到」记录。F7 状态位在 2.4G 下会
            # 假死报 0，只凭它连续失败几轮就把界面写成蓝牙，正是实机反馈的
            # 「用着 2.4G 突然跳成蓝牙已连接」。设备树里根本没有 USB 控制口时
            # 不受此限（真的只挂着蓝牙）。
            dead_need2 = 1 if self._cold else DEAD_CONFIRM
            dead_ok = (self._usb_dead_n >= dead_need2
                       and bool(self._usb_dead_ts)
                       and now - self._usb_dead_ts <= DEAD_TTL)
            # v13.7：USB 侧只要还有「活证据」，就不接受任何「切到蓝牙」的结论。
            # 活证据有三种，任意一条成立即算：
            #   · 45 秒内真的读到过私有帧（_usb_ok_ts）
            #   · 本轮 F7 帧自证在线（online=0 位 / can_read=1）
            #   · 本轮 F7 是可疑帧（online=1 但 can_read=0）——实测这就是
            #     鼠标挂在 2.4G 上静止时的帧，不是空置接收器
            # 实机复现（v13.6 日志）：bt_state=on link=1 ble_pct=94 fresh=None
            # usb_ok=True —— 鼠标明明在 2.4G，Windows 那边配过对的 BLE 链路位
            # 残留报 1、GATT 又吐回上次连接的缓存电量 94%，界面就写成
            # 「蓝牙已连接 94%」。这就是用户这次的投诉。
            # 只有 USB 侧彻底没动静（F7 回不了帧 / 干净死帧，且 45 秒内没有
            # 真回包）时，才允许蓝牙接管；真切到蓝牙时正好就是这个样子。
            usb_alive = ((bool(self._usb_ok_ts)
                          and now - self._usb_ok_ts <= USB_ALIVE_WIN)
                         or self._f7_on or self._f7_suspect)
            # v13.9：45 秒压制原本写在上面那行里，本意是「刚还在 2.4G 上跑，
            # 别一丢回包就翻蓝牙」。但拨档切到蓝牙的瞬间 _usb_ok_ts 恰好刚被
            # 刷新，这条压制就会把判定硬顶满 45 秒 —— 这正是用户反馈的「从
            # 2.4G 拨到蓝牙还是慢」的主因（界面最长能挂四十多秒才认蓝牙）。
            # 这里给它开一条失效通道：连续 NOFRAME_DEAD_N 轮连 F7 帧都读不到，
            # 说明接收器那条路真的空了，旧回包不再算「活证据」。
            # 保留原防线：2.4G 静止时帧读得到（_f7_frame=True）→ _noframe_n
            # 归零，且 _f7_suspect 仍单独压着，v13.7 的「2.4G 误显蓝牙」不会
            # 因为这次提速而复活。
            if (self._noframe_n >= NOFRAME_DEAD_N
                    and bool(self._usb_ok_ts)
                    and now - self._usb_ok_ts <= USB_ALIVE_WIN):
                usb_alive = self._f7_on or self._f7_suspect
                _dbg("bt_gate: no f7 frame x%d, usb_ok_ts %.1fs ago not alive"
                     % (self._noframe_n, now - self._usb_ok_ts))
            # v13.10：实机证明「接收器空置」时 F7 照样回帧，只是干净死帧，所以上面
            # 那条（连帧都读不到）几乎永不成立。这里补第二条失效通道：连续
            # CLEAN_DEAD_N 轮干净死帧 + 私有帧复核已确认不通（dead_ok），就撤掉
            # 旧回包带来的压制。2.4G 静止时帧是「可疑帧」（online=1/can_read=0），
            # 走不到这里；F7 位假死时私有帧复核会通过、_usb_dead_n 清零，dead_ok
            # 不成立 —— v13.7 的「2.4G 误显蓝牙」防线仍在。
            # 另有一道硬条件：蓝牙链路必须不是明确的「没连」（off）。VK M5 静止在
            # 2.4G 上时，本机实测也会回干净死帧（online=0/can_read=0），此时链路
            # 位一定是 off；而鼠标真拨到蓝牙后，链路位是 1 或还在 unknown。少了
            # 这条，2.4G 静止就会被当成空置接收器、界面漂成蓝牙。
            try:
                _bt_not_off = self._bt_state(now) != "off"
            except Exception:
                _bt_not_off = False
            if (self._clean_dead_n >= CLEAN_DEAD_N and dead_ok and _bt_not_off
                    and bool(self._usb_ok_ts)
                    and now - self._usb_ok_ts <= USB_ALIVE_WIN):
                usb_alive = self._f7_on or self._f7_suspect
                _dbg("bt_gate: clean dead f7 x%d + probe dead + bt link not off,"
                     " usb_ok_ts %.1fs ago not alive"
                     % (self._clean_dead_n, now - self._usb_ok_ts))
            ctrl_in = _usb_ctrl_exists()
            allow_bt = ((self._usb_miss >= miss_need and idle >= grace_need
                         and dead_ok and not usb_alive)
                        or (not ctrl_in))
            if not allow_bt:
                out["bt"] = None
            else:
                # v17：这里刻意不登记设备归属。走到这一步只说明「2.4G 侧没回包」，
                # 鼠标静置休眠也是这个形状，据此改归属会把在用鼠标的读数清成
                # 「读取中」。归属改由真正的读数来定：USB 真帧见 _stage_usb_hit，
                # 蓝牙 GATT 真值见 _cross_pick。
                # v13.9：终于放行蓝牙的那一刻，2.4G 在线期间 GATT 基本一直连不上，
                # _ble_fail 早已累到 5 档、_ble_next 可能被推到十几秒之后 —— 这是
                # 切档后「看着已经切了，却还要再等十来秒才出电量」的第二个原因。
                # USB 通道既然确认空了，就立刻把退避清零让 GATT 马上建连；同一
                # 时刻只清一次（5 秒节流），避免把重试打成高频轰炸。
                if (self._noframe_n >= NOFRAME_DEAD_N
                        and now - self._ble_kick_ts > 5.0):
                    self._ble_next = 0.0
                    self._ble_fail = 0
                    self._ble_kick_ts = now
                    _dbg("bt_kick: usb channel empty, reset ble backoff")
                if self._ble_pct is not None and now - self._ble_ts < BLE_CACHE:
                    pct = self._ble_pct
                elif (self._ble_pct is not None
                        and now - self._ble_ts < BLE_STALE_MAX):
                    # 缓存过期但还在可信窗口内：先沿用上一次读数，同时后台去刷。
                    # 否则每次缓存到期的那一轮 pct 都为 None，界面会从「蓝牙
                    # 已连接」闪成「蓝牙连接中」再跳回来，看着像在反复重连。
                    pct = self._ble_pct
                    self.ble_warm()
                else:
                    pct = None
                    self.ble_warm()      # 非阻塞：后台读，读完回调唤醒采集
                if pct is not None and not self._ble_fresh_ok(now):
                    # v13.6：系统 GATT 对「曾经配对过」的蓝牙设备会长期返回上次
                    # 连接时的缓存电量（实测 read CACHED 0.00s 返回 100%）。链路
                    # 明确=0 时这份缓存不是「鼠标此刻在蓝牙上」的证据，把它当
                    # 读数显示，就是「明明是 2.4G，却写蓝牙已连接 100%」。
                    try:
                        _lk_now = self._bt_state(now)
                    except Exception:
                        _lk_now = None
                    if _lk_now == "off":
                        _dbg("bt_cache_drop: link=off, cached pct=%s untrusted"
                             % pct)
                        pct = None
                    # v13.7：USB 侧刚刚还有真回包，说明鼠标挂在 2.4G/有线，
                    # 这份 GATT 读数只可能是系统缓存（实测 94% 常年不动）。
                    # 拿它显示「蓝牙已连接」正是用户这次的投诉。
                    elif usb_alive:
                        _dbg("bt_cache_drop: usb alive %.1fs ago, cached pct=%s"
                             " untrusted" % (now - self._usb_ok_ts, pct))
                        pct = None
                out["bt"] = pct
                if pct is not None:
                    # 与 2.4G / 有线通道对齐：GATT 拿到的是系统缓存时，改用最近
                    # 一次实时读数，否则同一个鼠标在两个模式下显示两个电量。
                    pct, _batt_note = self._cross_pick(pct, "gatt")
                    out["bt"] = pct
                    out["batt_note"] = _batt_note
                if pct is None:
                    # GATT 还没读到：只有确认存在蓝牙节点（或已拿到 BLE 地址）
                    # 才允许提示「蓝牙连接中」。接收器插着、鼠标只是这一轮没回包
                    # 时，界面应该显示「未响应」，否则用户看到的就是「明明用着
                    # 2.4G，却告诉我蓝牙」。
                    # 另外限时：真的切到蓝牙时 GATT 通常十几秒内读到；超过
                    # BT_PENDING_MAX 还读不到，就说明鼠标根本不在蓝牙上（多半是
                    # 关机/休眠），继续挂「蓝牙连接中」等于骗人。
                    bt_node = bool(self._ble_addr)
                    if not bt_node and now >= self._bt_addr_next:
                        # 同步补一次 BLE 地址：注册表路径只要几毫秒，拿到就说明
                        # 蓝牙设备确实在系统里，界面第一轮就能写「蓝牙连接中」，
                        # 不用先闪一下「2.4G 未响应」再跳过去。枚举失败则按
                        # BLE_ADDR_RETRY 冷却，避免每轮都去枚举拖慢刷新。
                        self._ble_addr = self._bt_addr_id(now, force=True)
                        self._bt_addr_next = now + (
                            0.0 if self._ble_addr else BLE_ADDR_RETRY)
                        bt_node = bool(self._ble_addr)
                    if not bt_node:
                        try:
                            bt_node = any(dd["bt"] for dd in list_devs())
                        except Exception:
                            bt_node = False
                    if (not bt_node and self._cold
                            and now - self._boot_ts < BT_FIRST_CONN):
                        # v11.8：冷启动头几秒，USB 侧已经握手不通、可设备树里
                        # 蓝牙节点还没枚举出来（配对信息 / HID 服务要等一会儿）。
                        # 这时按「蓝牙连接中」显示，别报「未收到回包」——
                        # 实机反馈里「打开软件就未响应」多半是这一瞬间。
                        bt_node = True
                    if bt_node:
                        if not self._bt_wait_since:
                            self._bt_wait_since = now
                        _waiting = now - self._bt_wait_since
                        out["bt_pending"] = _waiting <= BT_PENDING_MAX
                        # v13.4：两侧都毫无证据超过 BT_CHECK_HINT 后，界面除了
                        # 「蓝牙连接中」还要给一条能动手的提示（电源 / 拨档 / 配对），
                        # 别让用户对着同一句话干等两小时。
                        out["bt_check"] = _waiting >= BT_CHECK_HINT
                        _dbg("bt_wait: %.1fs pending=%s check=%s"
                             % (_waiting, out["bt_pending"], out["bt_check"]))
                    else:
                        self._bt_wait_since = 0.0
                else:
                    self._bt_wait_since = 0.0
                if pct is not None:
                    # 鼠标此刻挂在蓝牙上：PID 换成蓝牙节点的，界面显示才不矛盾
                    bt_pid = None
                    try:
                        for dd in list_devs():
                            if dd["bt"] and dd.get("pid"):
                                bt_pid = dd["pid"]
                                break
                    except Exception as e:
                        swallow("reader.bt_pid", e)
                    out.update(online=True, battery=pct, type=2, source="gatt",
                               mode="bt", pid=bt_pid or pid)
                    self.mode = "bt"
        return None

    def _stage_dpi(self, out):
        """阶段 5：填档位表（冷启动首轮给小预算，避免拖慢首屏）"""
        if (out["resp"] and out["mode"] in ("wired", "2.4g")) or \
                (out["mode"] == "bt" and out["battery"] is not None):
            now = time.time()
            if self._dpi_vals is None or now - self._dpi_ts > DPI_REFRESH:
                # 既然已经收到回包（resp=True），私有帧通道就是通的。但预算不能
                # 给大：一次 dpi_read 最多 6 层 × 0.4s，实测单轮能吃掉 2.4 秒，
                # 界面刷新周期就被它拖长了。1.2 秒足够覆盖正常情况，剩下的层
                # 下一轮再读。
                budget = 1.2
                if self._dpi_vals is None and time.time() - self._boot_ts < 2.0:
                    # 冷启动第一轮：别让档位表把首屏拖到 1.5 秒以上（界面开头
                    # 那段「…」就是这个造成的）。先给小预算把连接方式和电量
                    # 亮出来，档位表下一轮再补齐，用户感知上「打开就有数」。
                    budget = 0.3
                r = self.dpi_read(budget=budget)
                if r:
                    self._dpi_vals, self._dpi_ts = r, now
                else:
                    # 失败也别每轮都卡：8 秒后重试
                    self._dpi_ts = now - DPI_REFRESH + 8.0
            if self._dpi_vals:
                out["dpi_idx"], out["dpis"] = self._dpi_vals
        return None

    def _stage_evidence(self, out, live, now):
        """阶段 6：静止判定 + 通道在否 + 无证据时长"""
        if not out["resp"]:
            # v12.0：没有回包、但接收器还在插着、此前确实在线过 —— 处于静止静默
            # 状态（鼠标不动时设备本来就不上报）。界面据此写「识别中」，不写
            # 「未收到回包」。接收器被拔走（path 为空）或超出静止窗口（关机、
            # 鼠标断电）才照实报未响应。
            _base = max(self._still_ts or 0.0, self._usb_ok_ts or 0.0)
            # v13.7：本轮 F7 回了自证在线 / 可疑的帧，本身就是「通道在、鼠标
            # 只是没动」的正面证据。旧逻辑非要等一次真实私有帧回包才肯判静止
            # （_usb_ok_ts 非 0），于是重启软件、或鼠标静止一阵之后再点「重新
            # 识别」，界面前几秒只能写「识别中…」——用户说的「重启/重新识别
            # 很慢」有一半是这里。只是「识别中」会让人以为在等连接。
            _f7_pos = bool(self._f7_on or self._f7_suspect)
            # v13.8：帧读得到、但 F7 状态位报「不在线」（online=0 的干净死帧）
            # 是 2.4G 静止最典型的形态（实机：鼠标不动时 F7 回 online=0、私有帧
            # 也不应答）。只要蓝牙链路此刻明确「没连上」，有帧就等于「接收器还在、
            # 鼠标只是没动」，可以直接判静止沿用 —— 冷启动/重新识别第一两轮就能
            # 给出「2.4G 已连接（读数沿用）」，不用先挂一会儿「识别中」。
            # 真切到蓝牙时链路位=1，不会走到这里；链路读不到（unknown）也保守
            # 不认，等 0.2 秒后的探测结果再定，避免拨档瞬间被误判成静止。
            if not _f7_pos and getattr(self, "_f7_frame", False) and live["path"]:
                try:
                    _st_now = self._bt_state(now)
                except Exception:
                    _st_now = "unknown"
                if _st_now == "unknown" and now - (self._boot_ts or now) < 5.0:
                    # v13.8：冷启动第一轮链路探测还在后台跑（约 0.16 秒），这里最多
                    # 等 0.2 秒把它等出来。首轮就拿到「蓝牙=0」才能立刻进入静止确认
                    # 计时，否则要多等一整轮（1~2 秒）—— 那正是「重启后识别慢」。
                    _dl = time.time() + 0.2
                    while _st_now == "unknown" and time.time() < _dl:
                        time.sleep(0.04)
                        try:
                            _st_now = self._bt_state(time.time())
                        except Exception:
                            _st_now = "unknown"
                if _st_now == "off":
                    # v13.8：链路=0 要连续保持 0.8 秒才算「蓝牙确实没连上」。拨档刚
                    # 切到蓝牙的那一瞬间，系统还没建立 BLE 链路，链接位仍是 0，
                    # 若当轮就按静止沿用，界面会先闪一下「2.4G 已连接」再跳蓝牙。
                    if getattr(self, "_f7_trust", False):
                        self._f7_trust = False    # 手动重新识别：直接认，不再等确认
                        _f7_pos = True
                    else:
                        if not self._f7_off_since:
                            self._f7_off_since = now
                        if now - self._f7_off_since >= F7_OFF_CONFIRM:
                            _f7_pos = True
                else:
                    self._f7_off_since = 0.0
            else:
                self._f7_off_since = 0.0
            if _f7_pos:
                _base = max(_base, now)
            out["still"] = bool(_base and bool(live["path"])
                                and out.get("source") != "gatt"
                                # v13.7：F7 正面证据在，就不必卡在「链路状态
                                # 未读到」的待确认态，也别被一次可疑帧攒出来的
                                # 空置硬证据钉住 —— 那两项在有帧的前提下都只是
                                # 蓝牙侧残留造成的噪音。
                                and (not out.get("bt_unk") or _f7_pos)
                                # v12.1：已经有「蓝牙侧活证据」的硬证据时不算静止。
                                # _still_ts 会被写硬证据那段清零，但 _base 还取了
                                # _usb_ok_ts，光靠清 _still_ts 压不住 —— 实机表现就是
                                # 已经切到蓝牙了，界面还在写「鼠标静止，等待下次上报」。
                                and (not self._usb_dead_ts or _f7_pos)
                                and (time.time() - _base) <= STILL_HOLD_MAX)
        # v12.0 界面兜底：接收器/线是否还挂在机器上。只要通道在，就不该写
        # 「未响应 / 未收到回包」—— 鼠标不动时本来就不会有应答，写这句等于
        # 把「静止」报成「故障」。通道真被拔走才照实报。
        try:
            out["ch"] = bool(live["path"]) or bool(_usb_ctrl_exists())
        except Exception:
            out["ch"] = bool(live["path"])
        # v12.1：已经多久没拿到过任何一侧的证据（秒）。0 表示本轮有证据。
        # 界面用它区分「刚拨完开关，正在确认」和「等了半天什么都没收到」——
        # 后者要给出可操作的提示，不能一直只写「识别中…」让用户干等。
        if ((out["online"] and not out.get("stale"))
                or out.get("source") == "gatt" or self._usb_dead_ts):
            self._noevi_ts = 0.0
        elif out["ch"]:
            if not self._noevi_ts:
                self._noevi_ts = now
            out["nosig"] = max(0.0, now - self._noevi_ts)
        else:
            self._noevi_ts = 0.0
        return None


    def get_profile(self, deadline=None):
        """设备当前板载配置层（0 起），失败返回 None"""
        for _ in range(2):
            p = self._request_read([CMD_GET_PROFILE], deadline)
            if p and p[0] == CMD_GET_PROFILE and p[1] < MAX_LAYER:
                return p[1]
            time.sleep(0.08)
        return None

    def _bt_cmd(self, payload, wait=0.8, need_reply=True):
        """v24：BLE HID 0x55 命令（与 Valkyrie Driver 同协议）

        output report ID 6：`06 55 <payload>`；input report：`06 55 <reply>`。
        payload 不足 9 字节补零，超过 63 字节截断（报告上限 64 字节），
        下标 7 是 Bit7 校验。返回去掉 `06 55` 后的 payload，失败返回 None。
        """
        try:
            cands = [d for d in list_devs() if d.get("bt")
                     and d.get("usage_page") in (0xFF55, 0xFFFF)
                     and d.get("usage") == 0x0202]
        except Exception:
            cands = []
        if not cands:
            return None
        body = bytearray(payload)
        if len(body) < 9:
            body += bytes(9 - len(body))
        if len(body) > 63:
            body = body[:63]
        body[7] = 255 - (sum(body[:7]) & 255)
        dev = None
        try:
            dev = _hid().device()
            dev.open_path(cands[0]["path"])
            dev.set_nonblocking(1)
            for _ in range(6):
                if not dev.read(128, 5):
                    break
            dev.set_nonblocking(0)
            if dev.write(bytes([0x06, 0x55]) + bytes(body)) < 0:
                return None
            if not need_reply:
                return b""
            end = time.time() + wait
            while time.time() < end:
                r = dev.read(128, 60)
                if not r:
                    continue
                b = bytes(r)
                if len(b) >= 3 and b[0] == 0x06 and b[1] == 0x55:
                    return b[2:]
        except Exception as e:
            swallow("reader.bt_cmd", e)
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass  # 可忽略：幂等清理/UI 收尾
        return None

    def _bt_read_dpi_layer(self, layer):
        p = self._bt_cmd([0x90, layer])
        if not p or len(p) < 44 or p[0] != 0x90:
            return None
        cnt = p[3]
        if not (1 <= cnt <= 8):
            return None
        vals = []
        for i in range(cnt):
            x = p[8 + i * 2] | (p[9 + i * 2] << 8)
            if not (50 <= x <= 26000):
                return None
            vals.append(x)
        real = p[1] if p[1] < MAX_LAYER else layer
        idx = p[2] if p[2] < len(vals) else 0
        return idx, vals, real

    def _read_dpi_layer(self, fam, layer, commit=True, deadline=None):
        """读某一层的 DPI 档位表，返回 (cur,vals,real_layer) 或 None

        回包布局（实测）：p[1]=层号 p[2]=当前档位 p[3]=档位数
        p[8+2i]=第 i 档 X 灵敏度 p[24+2i]=Y 灵敏度。
        """
        p = self._request_read([fam["get"], layer], deadline)
        if not p or len(p) < 44 or p[0] != fam["get"]:
            return None
        cnt = p[3]
        if not (1 <= cnt <= 8):
            return None
        vals = []
        for i in range(cnt):
            x = p[8 + i * 2] | (p[9 + i * 2] << 8)
            if not (50 <= x <= 26000):
                return None
            vals.append(x)
        real_layer = p[1] if p[1] < MAX_LAYER else layer
        # 档位索引越界时钳到首档，避免整层被判废又一档都读不出来
        idx = p[2] if p[2] < len(vals) else 0
        if commit:
            self.family, self.layer = fam, real_layer
        return idx, vals, real_layer

    def _layer_score(self, vals, layer):
        """给某层的档位表打分（越高越可信），-1 表示不可用

        实测证据（VK M5，2.4G 通道）：
          layer0: 90 00 05 06 | 90 01 80 0C B0 04 80 0C 80 0C 80 0C
                  -> 400/3200/1200/3200/3200/3200，当前档位 05（未初始化的影子配置）
          layer1: 90 01 02 06 | 90 01 20 03 B0 04 40 06 80 0C 50 14
                  -> 400/800/1200/1600/3200/5200，当前档位 02（真正生效的配置）
        两层回包里的层号都与请求一致，光比层号分不出来；区别在于影子层的档位
        值大量重复、不递增。所以判据是「严格递增 + 命中官方预设越多越可信」。
        """
        if not vals:
            return -1
        # 关键判据：真实档位表的值互不相同；影子层（未初始化）会大量重复，
        # 例如 400/3200/1200/3200/3200/3200 —— 而且这些值恰好全是官方预设，
        # 单看"命中预设数"是分不出来的，必须靠唯一性。
        if len(set(vals)) != len(vals):
            return -1
        inc = all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))
        if not inc:
            return 40 - layer        # 唯一但不递增：可疑，只能当末位兜底
        hit = sum(1 for v in vals if v in DPI_PRESETS)
        return 600 + hit * 10 + (MAX_LAYER - layer)

    def dpi_read(self, budget=4.0):
        """返回 (当前档位索引, [各档 DPI])；读不到返回 None

        读法：按候选层顺序逐层读 0x90 族档位表并打分，取分最高的层。
        候选顺序 = 上次选定的层 -> 0..7（**不采信设备自称的 profile**：
        get_profile 在 VK M5 上返回 0，而 0 正是那份影子层，之前"越读越错"
        就是被它带偏的）。
        一旦某层「值严格递增且命中 >=3 个官方预设」立即采纳并停止扫描，
        常态只花一次读取。
        """
        deadline = time.time() + budget
        if self.mode == "bt":
            # 蓝牙：先读当前 profile（0x82），再读对应层；0/1 兜底
            lay = 0
            try:
                _p = self._bt_cmd([0x82])
                if (_p and len(_p) >= 2 and _p[0] == 0x82
                        and _p[1] < MAX_LAYER):
                    lay = _p[1]
            except Exception as e:
                swallow("reader.bt_profile", e)
            tried = []
            for layer in (lay, 0, 1):
                if layer in tried:
                    continue
                tried.append(layer)
                r = self._bt_read_dpi_layer(layer)
                if r and r[2] == layer:
                    self.family, self.layer = DPI_FAMILIES[0], layer
                    return r[0], r[1]
            return None
        fam = DPI_FAMILIES[0]
        # 快速路径：已锁定层读得出就直接用。层号在这里必须保持稳定，否则
        # 界面会在 1/2/3 层之间换来换去（这几层回包内容相同，但写入只对
        # 其中一层真正生效，频繁漂移会让"调档位"时好时坏）。
        if self.layer is not None:
            for _try in range(2):
                r = self._read_dpi_layer(fam, self.layer, commit=False,
                                         deadline=min(deadline, time.time() + 1.2))
                if r and r[2] == self.layer and len(set(r[1])) == len(r[1]):
                    return r[0], r[1]
                if time.time() > deadline:
                    break
        cand = []
        if self.layer is not None:
            cand.append(self.layer)
        for l in range(MAX_LAYER):
            if l not in cand:
                cand.append(l)
        best = None
        scanned = 0
        for layer in cand:
            if time.time() > deadline or scanned >= 4:
                break
            if best is not None and best[0] >= 500:
                break
            scanned += 1
            # 每层单独限量，避免某一层没回包就吃光整轮预算
            layer_dl = min(deadline, time.time() + 1.2)
            r = self._read_dpi_layer(fam, layer, commit=False, deadline=layer_dl)
            if not r:
                r = self._read_dpi_layer(fam, layer, commit=False,
                                         deadline=min(deadline, time.time() + 1.2))
            if not r:
                continue
            idx, vals, real = r
            if real != layer:
                _dbg("dpi skip mirror req=%s real=%s" % (layer, real))
                continue
            sc = self._layer_score(vals, layer)
            if sc < 0:
                continue
            if best is None or sc > best[0]:
                best = (sc, layer, idx, vals)
        if best is None:
            return None
        _, layer, idx, vals = best
        if self.layer != layer:
            _dbg("dpi layer -> %s (score %s)" % (layer, best[0]))
        self.family, self.layer = fam, layer
        return idx, vals

    def _build_dpi_buf(self, fam, layer, index, cnt, src):
        buf = bytearray(64)
        buf[0] = fam["set"]
        buf[1] = layer
        buf[2] = index
        buf[3] = cnt
        for i in range(cnt):
            x = src[8 + i * 2] | (src[9 + i * 2] << 8)
            y = src[24 + i * 2] | (src[25 + i * 2] << 8)
            buf[8 + i * 2] = x & 0xFF
            buf[9 + i * 2] = x >> 8
            buf[24 + i * 2] = y & 0xFF
            buf[25 + i * 2] = y >> 8
            buf[40 + i * 3] = src[40 + i * 3]
            buf[41 + i * 3] = src[41 + i * 3]
            buf[42 + i * 3] = src[42 + i * 3]
        buf[7] = 255 - (sum(buf[:7]) & 255)
        return bytes(buf)

    def _write_dpi_layer(self, fam, layer, index, cnt):
        """把某一层的当前档位改成 index（档位表数值与颜色原样写回）"""
        p = self._request_read([fam["get"], layer])
        if not p or p[0] != fam["get"] or len(p) < 44:
            return False
        if not self._gate_send():
            return False
        self._sel_mouse()
        self._send(self._build_dpi_buf(fam, layer, index, cnt, p))
        time.sleep(0.2)
        return True

    def invalidate_dpi(self):
        """让下一次 read_all 立即重读档位（手动刷新/调档后调用）"""
        self._dpi_ts = 0.0
        self._dpi_vals = None

    def invalidate_link(self):
        """让下一次 read_all 重新枚举通道并重新握手判活

        只置标记：判活缓存与句柄都由后台采集线程在 read_all 开头统一处置，
        跨线程直接关句柄会在读写途中炸掉，反复 close/open 也会让握手偶发失败。
        """
        self._force_relink = True
        # v16：用户点刷新时连蓝牙地址缓存一起作废 —— 刚换了鼠标的人点刷新，
        # 要的就是让程序重新认一遍设备，而不是继续用旧地址那份缓存。
        self._bt_addr_ts = 0.0
        self.reset_verdict()

    def invalidate_battery(self):
        """v19：手动刷新电量：把所有通道的旧读数全部作废，立刻重读

        旧实现点刷新只清 DPI 和链路，电量缓存（蓝牙 GATT 值、上次有效读数）
        还留着，界面就会继续顶着旧数字。这里一次清干净，并让后台马上去读一次
        蓝牙；2.4G/有线 的 HID 值下一轮 read_all 会当场重读。
        """
        self._ble_pct = None
        self._ble_ts = 0.0
        self._ble_fresh_pct = None
        self._ble_fresh_ts = 0.0
        self._last_good_batt = None
        self._last_good_batt_ts = 0.0
        self._last_batt_src = ""
        self._ble_fail = 0
        self._ble_next = 0.0
        self._ble_kick_ts = 0.0
        try:
            _BLE_REALTIME["pct"] = None
            _BLE_REALTIME["ts"] = 0.0
            _BLE_FRESH_TS[0] = 0.0
        except Exception as e:
            swallow("reader.invalidate_battery", e)
        try:
            self.ble_warm()
        except Exception as e:
            swallow("reader.invalidate_battery.warm", e)

    def reset_verdict(self):
        """清掉「本轮判定」用的短时状态，让下一轮直接给结论

        v13.8：点「重新识别连接」时只作废判活缓存是不够的 —— 上一轮攒下的
        失败轮数、蓝牙待确认计时、静止起点、深探节流都还压着，新结论要等这些
        计数器重新攒够（2 轮 + 2.5 秒 grace）才出得来，用户体感就是「点了没
        反应，得再点一次才行」。这里只清计数与计时，**不动** _usb_ok_ts
        （那是「刚才真的读到过私有帧」的硬证据，清了会让 2.4G 静止被误判成
        蓝牙）也不动句柄/地址缓存。
        """
        self._usb_miss = 0
        self._usb_dead_n = 0
        self._usb_dead_ts = 0.0
        self._still_ts = 0.0
        self._still_n = 0
        self._f7_off_since = 0.0
        # v13.8：用户点「重新识别连接」是要立刻看结论。链路=0 本身已被_bt_switch_ok
        # 护栏把「真拨到蓝牙」挡在外面（真蓝牙时 link=1，不会被当成静止），所以这里
        # 直接把静止确认计时视作已满足，首轮就出「2.4G 已连接」，不再让用户点完还等。
        self._f7_trust = True
        self._fake_ts = 0.0
        self._fake_n = 0
        self._fake_renew = 0
        self._deep_ts = 0.0
        self._bt_unk_ts = 0.0
        self._bt_off_since = 0.0
        self._bt_wait_since = 0.0

    def dpi_set(self, index):
        """修改当前档位 -> (ok, msg)

        说明：旧版本失败一律提示「鼠标可能休眠」，把「读不到档位表 / 设备忙 /
        档位序号越界」全混成一句，用户鼠标明明在用也看到休眠。这里按原因返回。

        先写投票选出的权威层；写完回读校验，若档位没变（该层只是镜像、并非
        生效层），再回退到 layer0 重写一次，确保真的改到鼠标生效配置上。
        """
        # A955：档位不再是只读 — 已确证配置内存（头 @2 / 档位表 @12）可写，
        # 切档 = 写 DPI 头的当前档序号，写完回读校验；失败按原因回报。
        try:
            nodes = a955.find()
        except Exception as e:
            swallow("reader.a955_find", e)
            nodes = None
        prev_pid = (getattr(self, "_last_out", None) or {}).get("pid")
        if prev_pid in a955.PIDS and not nodes:
            # A955 的档位写在设备配置内存里，只能走 FF02 厂商通道（USB 侧）。
            # 找不到通道就把话说清楚，别让用户以为点了没反应。
            return False, "未找到 A955 接收器（请插 2.4G 接收器或接上线）"
        if nodes and prev_pid in a955.PIDS:          # 上一轮确认在用的是 A955 才写
            ok, msg = a955.dpi_set_active(index, nodes)
            if ok:
                self._a955_dpi_cache = None
                try:
                    self.invalidate_dpi()
                except Exception as e:
                    swallow("reader.a955_invalidate", e)
            return ok, msg
        r = self.dpi_read()
        if not r:
            r = self.dpi_read()          # 2.4G 偶发丢帧很常见，补读一次
        if not r:
            return False, "未读到档位表（稍后再试或点刷新）"
        _cur, vals = r
        if not (0 <= index < len(vals)):
            return False, "档位序号超出设备档位数"
        fam = self.family or DPI_FAMILIES[0]
        cnt = len(vals)
        if self.mode == "bt":
            layer = self.layer if self.layer is not None else 0
            p = self._bt_cmd([0x90, layer])
            if not p or len(p) < 44 or p[0] != 0x90:
                return False, "蓝牙 DPI 读取失败"
            buf = self._build_dpi_buf(fam, layer, index, cnt, p)
            if self._bt_cmd(buf, need_reply=False) is None:
                return False, "蓝牙写入失败"
            time.sleep(0.25)
            back = self._bt_read_dpi_layer(layer)
            if back and back[0] == index:
                self._dpi_vals, self._dpi_ts = (back[0], back[1]), time.time()
                return True, "已生效"
            return True, "已写入（蓝牙未回读确认）"
        sent = False
        tried = []
        for layer in (self.layer, 0):
            if layer is None or layer in tried:
                continue
            tried.append(layer)
            if not self._write_dpi_layer(fam, layer, index, cnt):
                continue
            sent = True
            time.sleep(0.15)
            # 回读 0..3 层：设备有多个板载层，真实生效层不一定是刚写的那个
            # （写入可能落在镜像副本上），所以要在所有层里找「档位真的变了」
            # 的那一层，并把它记为权威层。优先认写入层。
            seen = []
            for lay in [layer] + [x for x in range(4) if x != layer]:
                back = self._read_dpi_layer(fam, lay, commit=False,
                                            deadline=time.time() + 1.2)
                if not back:
                    continue
                seen.append((lay, back))
                if back[0] == index and len(set(back[1])) == len(back[1]):
                    self.family, self.layer = fam, lay
                    # 关键：把新档位写回缓存，否则下一轮采集会用旧缓存把界面
                    # 「盖」回去，看起来像调完又跳回原档位
                    self._dpi_vals, self._dpi_ts = (back[0], back[1]), time.time()
                    return True, "已生效"
        if sent:
            return True, "已写入（设备未回读确认）"
        return False, "写入未被设备接受（通道忙或不可写）"
