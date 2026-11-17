# -*- mode: python ; coding: utf-8 -*-
# VK_M5 v24 打包脚本
# v20：设备配置 devices.json（VID/PID/电量字节偏移/蓝牙名/蓝牙地址）。
# v19 相对 v18 的变更：
#   1) 2.4G/有线电量继续走 HID F7 特征报告（设备当场回报），尾行标「HID 直读」；
#   2) 取消 2.4G 与蓝牙的跨通道对齐：两边各显示各的读数，不再取低值；
#   3) 启动不再把 state.json 里的旧电量摆到界面（旧值曾让界面一直停在 94）；
#   4) 手动刷新会作废所有电量缓存并立刻重读；
#   5) 蓝牙优先 UNCACHED 实时读（2.5 秒超时），缓存窗口 60→20 秒、沿用上限 600→90 秒。
# 功能与打包依赖不变——winsdk 显式收集逻辑照搬 v13.4。
import os

import winsdk

_wroot = list(winsdk.__path__)[0]

# 只收蓝牙判定链路真正需要的子包，避免把整个 41MB 的 winsdk 全塞进 exe。
_NEED_DIRS = [
    ("windows/devices/bluetooth", "winsdk/windows/devices/bluetooth"),
    ("windows/devices/enumeration", "winsdk/windows/devices/enumeration"),
    ("windows/devices/radios", "winsdk/windows/devices/radios"),
    ("windows/storage/streams", "winsdk/windows/storage/streams"),
    ("windows/foundation", "winsdk/windows/foundation"),
    ("system", "winsdk/system"),
]

_datas = []
_hidden = ["winsdk", "winsdk._winrt"]
for _src_rel, _dst_rel in _NEED_DIRS:
    _src = os.path.join(_wroot, *_src_rel.split("/"))
    if not os.path.isdir(_src):
        continue
    for _dp, _dns, _fs in os.walk(_src):
        _rel = os.path.relpath(_dp, _wroot)
        _parts = _rel.split(os.sep)
        _dst = "winsdk/" + "/".join(_parts)
        for _f in _fs:
            if _f.endswith((".pyi", ".pyi.typed")):
                continue
            _datas.append((os.path.join(_dp, _f), _dst))
            if _f.endswith(".py"):
                _base = _f[:-3]
                _mod = ".".join(["winsdk"] + _parts +
                                ([] if _base == "__init__" else [_base]))
                _hidden.append(_mod)
            elif _f.endswith(".pyd"):
                _base = _f.rsplit(".", 1)[0]
                _hidden.append(".".join(["winsdk"] + _parts + [_base]))

# _winrt.pyd 必须作为二进制依赖进 exe，否则 winsdk 的所有投影模块 import 就崩
_winrt_pyd = os.path.join(_wroot, "_winrt.pyd")
_binaries = [(_winrt_pyd, "winsdk")] if os.path.isfile(_winrt_pyd) else []

print("[spec v24] winsdk datas=%d hidden=%d" % (len(_datas), len(_hidden)))

a = Analysis(
    ['run_vk_m5.py'],
    pathex=[],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VK_M5_v24',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['vk.ico'],
)
