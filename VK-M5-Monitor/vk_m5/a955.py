# -*- coding: utf-8 -*-
"""A955（VID 0x260D）2.4G 接收器 / 有线的厂商协议读取。

协议来源：逆向 OemDrv.exe + 实机验证。

  发送：collection usage_page 0xFF02，Feature report ID 0x08，17 字节
        [0]      = 0x08 报告 ID
        [1]      = 命令字
        [2..15]  = 参数
        [16]     = 校验 = 0x55 - sum(frame[0:16]) & 0xFF
  应答：collection usage_page 0xFF01，Input report 0x09，17 字节
        [0] = 0x09
        [1] = 命令回显
        [2] = 状态（0 = 成功，1 = 不支持/参数错）
        [6] = 电量 0~100
        [7] = 充电标志

已确证命令：
  0x04  读电量（只读，不改设备配置）

未能确证命令（一律只回空 ack，驱动内部会话专有，本工具不去猜）：
  DPI 的实时读写。因此 5 档 DPI 表来自设备侧配置（Cfg.ini 明文
  DM=5 / DPI=400,800,1600,3200,6400 / DEFLEVEL=2），不是实时读出的。

电量显示规则与官方驱动一致：四档，阈值 25/50/75/100（power1~4）。
"""

import time
import threading

import hid

# 厂商通道（FF02 发 / FF01 收）同一时刻只允许一个会话。后台线程读电量、读档位、
# 用户点档位写档位会同时来抢这对句柄，互相把对方的应答帧读走，表现就是
# 「电量能读、档位表读不到、点档位没反应」。所有 A955 访问统一串行化。
_IO_LOCK = threading.RLock()

VID = 0x260D
PID_WIRELESS = 0x1074
PID_WIRED = 0x1097
# 只认 USB 形态（2.4G 接收器 / 有线直连）。蓝牙形态 VID 会变成 0x25A7，
# 那条路上只有标准 GATT，没有 FF02 厂商通道，改不了档位，故不识别、不处理。
PIDS = (PID_WIRELESS, PID_WIRED)

PAGE_SEND = 0xFF02
PAGE_RECV = 0xFF01
RID_SEND = 0x08
LEN = 17

CMD_POWER = 0x04

# 官方驱动 power1~4.png 的四档阈值（百分比上界）
BANDS = (25, 50, 75, 100)
# 静态兜底档位表（实测本机设备配置内存里的真实值；实时读失败时才用）
DPI_TABLE = (500, 950, 1900, 3800, 7550)
DPI_DEFAULT = 4


def _hid():
    return hid


def find(vid=None):
    """找 A955 的收发节点；返回 {send, recv, pid, product} 或 None

    只认厂商 collection（0xFF02 发 / 0xFF01 收），普通鼠标/键盘 collection
    不参与。同一 VID 下插了别的设备时，靠 PID 过滤。
    vid 留空时只扫 USB 形态（VID 0x260D）：2.4G 接收器与有线直连都在这个 VID 下。
    """
    vids = (VID,) if vid is None else (vid,)
    items = []
    for v in vids:
        try:
            items.extend(_hid().enumerate(v, 0))
        except Exception:
            continue
    send = recv = None
    pid = None
    product = ""
    for d in items:
        up = d.get("usage_page")
        if up not in (PAGE_SEND, PAGE_RECV):
            continue
        if d.get("product_id") not in PIDS:
            continue
        if pid is None:
            pid = d.get("product_id")
            product = d.get("product_string") or ""
        if up == PAGE_SEND and send is None:
            send = d["path"]
        elif up == PAGE_RECV and recv is None:
            recv = d["path"]
    if not send or not recv:
        return None
    return {"send": send, "recv": recv, "pid": pid, "product": product}


def _frame(cmd, params=b""):
    fr = bytearray(LEN)
    fr[0] = RID_SEND
    fr[1] = cmd
    for i, b in enumerate(params[:LEN - 3]):
        fr[2 + i] = b
    fr[LEN - 1] = (0x55 - (sum(fr[:LEN - 1]) & 0xFF)) & 0xFF
    return bytes(fr)


def query(cmd, params=b"", nodes=None, timeout=0.6):
    """发一条命令并取应答帧（bytes）；超时/异常返回 None"""
    nodes = nodes or find()
    if not nodes:
        return None
    dev_s = dev_r = None
    _IO_LOCK.acquire()
    try:
        dev_s = _hid().device()
        dev_s.open_path(nodes["send"])
        dev_r = _hid().device()
        dev_r.open_path(nodes["recv"])
        dev_r.set_nonblocking(1)
        # 先清掉上一条命令的残留应答，避免把旧帧当成本次结果
        for _ in range(8):
            if not dev_r.read(64):
                break
        dev_s.send_feature_report(_frame(cmd, params))
        t0 = time.time()
        hit = None
        while time.time() - t0 < timeout:
            r = dev_r.read(64)
            if r:
                b = bytes(r)
                if len(b) >= 8 and b[0] == 0x09 and b[1] == cmd:
                    hit = b
                    if b[2] == 0:
                        break
            else:
                time.sleep(0.02)
        return hit
    except Exception:
        return None
    finally:
        for d in (dev_s, dev_r):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
        _IO_LOCK.release()


def read_power(nodes=None):
    """读电量：返回 {'pct': int, 'charging': bool} 或 None"""
    r = query(CMD_POWER, nodes=nodes)
    if not r or r[2] != 0:
        return None
    pct = r[6]
    if pct > 100:
        return None
    charging = bool(r[7]) if len(r) > 7 and r[7] <= 1 else False
    return {"pct": int(pct), "charging": charging}


def level(pct, bands=BANDS):
    """电量档位 1~4（与驱动 power1~4.png 对应）；None/越界返回 0"""
    if pct is None:
        return 0
    try:
        p = int(pct)
    except Exception:
        return 0
    if p < 0:
        return 0
    for i, hi in enumerate(bands):
        if p < hi:
            return i + 1
    return len(bands)


def mode_of(pid):
    if pid == PID_WIRELESS:
        return "2.4g"
    if pid == PID_WIRED:
        return "wired"
    return None


# ---------------- DPI 档位：配置内存实时读写 ----------------
# 协议来自 OpenMouse（dareu_proto.ts / dareu_hid.ts）解析 + 本机实机验证：
#   内存 2  = DPI 头 4 字节  [档位数, ck, 当前档序号(0 起), ck]
#   内存 12 = 档位表，每档 4 字节 [id 低, ck, id 高, ck]，DPI = (id + 1) * 50
#   每段校验 = (0x55 - sum(payload)) & 0xFF，与命令帧校验同一套
# 读用 0x08（参数：地址高/低 + 长度，单帧 ≤10 字节），写用 0x07 同结构。
# 实测：读头 0x05500352 → 5 档、当前第 4 档；档位表真实值由设备给出，
# 写档位值/写当前档均被接受且回读一致（可逆操作，每次写完都回读校验）。
CMD_READ_BUF = 0x08
CMD_WRITE_BUF = 0x07
ADDR_DPI_HDR = 2
ADDR_DPI_STAGES = 12
CHUNK = 10
DPI_STEP = 50
DPI_MIN = 100
DPI_MAX = 26000
MAX_STAGES = 8


def _mem_ck(payload):
    return (0x55 - (sum(payload) & 0xFF)) & 0xFF


def id_to_dpi(i):
    return (int(i) + 1) * DPI_STEP


def dpi_to_id(dpi):
    """DPI 值 -> 设备存储用 id（50 的整数倍，最小 100）"""
    n = int(round(float(dpi) / DPI_STEP))
    n = max(2, min(DPI_MAX // DPI_STEP, n))
    return n - 1


class _Sess(object):
    """一对句柄走完一次多帧往返，省掉反复开关设备的开销"""

    def __init__(self, nodes):
        self.nodes = nodes
        self.s = self.r = None

    def __enter__(self):
        _IO_LOCK.acquire()
        try:
            self.s = _hid().device()
            self.s.open_path(self.nodes["send"])
            self.r = _hid().device()
            self.r.open_path(self.nodes["recv"])
            self.r.set_nonblocking(1)
            for _ in range(8):
                if not self.r.read(64):
                    break
            return self
        except Exception:
            self._close()
            _IO_LOCK.release()
            raise

    def _close(self):
        for d in (self.s, self.r):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass

    def __exit__(self, *a):
        self._close()
        _IO_LOCK.release()
        return False

    def xfer(self, cmd, params=b"", timeout=0.8):
        try:
            self.s.send_feature_report(_frame(cmd, params))
        except Exception:
            return None
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.r.read(64)
            if r:
                b = bytes(r)
                if len(b) >= 8 and b[0] == 0x09 and b[1] == cmd:
                    return b
                continue
            time.sleep(0.02)
        return None

    def read_mem(self, addr, n, tries=3):
        """读配置内存。接收器偶尔漏应答（约 1~2 成概率一条无回音），
        同一块原地重发即可，不必重开会话。"""
        out = b""
        off = 0
        while off < n:
            seg = min(CHUNK, n - off)
            a = addr + off
            b = None
            for _ in range(tries):
                b = self.xfer(CMD_READ_BUF,
                              bytes([0x00, (a >> 8) & 0xFF, a & 0xFF, seg]))
                if b and b[2] == 0:
                    break
                time.sleep(0.03)
            if not b or b[2] != 0:
                return None
            out += b[6:6 + seg]
            off += seg
        return out

    def write_mem(self, addr, data, tries=2):
        """写配置内存。同一地址重复写同一串字节是幂等的，重发安全。"""
        off = 0
        while off < len(data):
            seg = data[off:off + CHUNK]
            a = addr + off
            b = None
            for _ in range(tries):
                b = self.xfer(CMD_WRITE_BUF,
                              bytes([0x00, (a >> 8) & 0xFF, a & 0xFF, len(seg)])
                              + bytes(seg))
                if b and b[2] == 0:
                    break
                time.sleep(0.03)
            if not b or b[2] != 0:
                return False
            off += len(seg)
        return True


def _hdr_bytes(count, active):
    c = max(1, min(MAX_STAGES, int(count)))
    a = max(0, min(c - 1, int(active)))
    return bytes([c, _mem_ck([c]), a, _mem_ck([a])])


def dpi_read(nodes=None):
    """实时读 DPI 档位表与当前档

    返回 {"count": n, "active": 0 起序号, "stages": [dpi...]}；
    设备不在/无应答返回 None（调用方回退到静态档位表）。
    """
    nodes = nodes or find()
    if not nodes:
        return None
    # 接收器偶发漏一帧（约 0.5 秒超时），整段重开一次会话补读，别让界面回退到
    # 静态兜底表；两次都不行才算读不到。
    for _try in range(2):
        try:
            with _Sess(nodes) as s:
                hdr = s.read_mem(ADDR_DPI_HDR, 4)
                if not hdr or len(hdr) < 4:
                    continue
                count = hdr[0]
                if not 1 <= count <= MAX_STAGES:
                    continue
                raw = s.read_mem(ADDR_DPI_STAGES, count * 4)
                if not raw or len(raw) < count * 4:
                    continue
                stages = []
                for i in range(count):
                    rec = raw[i * 4:i * 4 + 4]
                    stages.append(id_to_dpi(rec[0] | (rec[2] << 8)))
                active = hdr[2] if hdr[2] < count else 0
                return {"count": count, "active": active, "stages": stages}
        except Exception:
            continue
        finally:
            time.sleep(0.02)
    return None


def dpi_set_active(index, nodes=None):
    """切换当前档位（0 起）。写完回读校验，返回 (ok, msg)

    读档位头失败多半是接收器漏了一帧，重开会话再试一次；写入本身是幂等的
    （写同一个当前档序号），重试不会把设备写成别的档。
    """
    nodes = nodes or find()
    if not nodes:
        return False, "找不到 A955 接收器"
    for _try in range(2):
        ok, msg = _set_active_once(index, nodes)
        if msg != "读取档位头失败":
            return ok, msg
        time.sleep(0.03)
    return False, "读取档位头失败（接收器无应答）"


def _set_active_once(index, nodes):
    try:
        with _Sess(nodes) as s:
            hdr = s.read_mem(ADDR_DPI_HDR, 4)
            if not hdr or len(hdr) < 4:
                return False, "读取档位头失败"
            count, cur = hdr[0], hdr[2]
            if not 1 <= count <= MAX_STAGES:
                return False, "档位数异常（%d）" % count
            if not 0 <= index < count:
                return False, "档位序号超出设备档位数"
            if not s.write_mem(ADDR_DPI_HDR, _hdr_bytes(count, index)):
                return False, "写入被设备拒绝"
            back = s.read_mem(ADDR_DPI_HDR, 4)
            if back and len(back) >= 3 and back[2] == index:
                return True, "已生效"
            if back and len(back) >= 3:
                return False, "回读仍为第 %d 档" % (back[2] + 1)
            return True, "已写入（未回读确认）"
    except Exception as e:
        return False, "异常 " + str(e)[:24]


def dpi_write_stage(index, dpi, nodes=None):
    """改某档的 DPI 数值（50 步进，100~26000）。写后回读校验"""
    nodes = nodes or find()
    if not nodes:
        return False, "找不到 A955 接收器"
    val = int(dpi)
    if val < DPI_MIN or val > DPI_MAX:
        return False, "DPI 超出 100~26000"
    try:
        with _Sess(nodes) as s:
            hdr = s.read_mem(ADDR_DPI_HDR, 4)
            if not hdr or len(hdr) < 2:
                return False, "读取档位头失败"
            count = hdr[0]
            if not 0 <= index < count:
                return False, "档位序号超出设备档位数"
            i = dpi_to_id(val)
            rec = bytes([i & 0xFF, _mem_ck([i & 0xFF]),
                         (i >> 8) & 0xFF, _mem_ck([(i >> 8) & 0xFF])])
            if not s.write_mem(ADDR_DPI_STAGES + index * 4, rec):
                return False, "写入被设备拒绝"
            back = s.read_mem(ADDR_DPI_STAGES + index * 4, 4)
            if back and len(back) >= 4:
                got = id_to_dpi(back[0] | (back[2] << 8))
                if got == id_to_dpi(i):
                    return True, "已生效 %d DPI" % got
                return False, "回读仍为 %d DPI" % got
            return True, "已写入（未回读确认）"
    except Exception as e:
        return False, "异常 " + str(e)[:24]


def dpi_table(nodes=None):
    """便捷封装：只要 (状态列表, 当前序号)，读不到返回 ([], 0)"""
    got = dpi_read(nodes)
    if not got:
        return [], 0
    return list(got["stages"]), int(got["active"])
