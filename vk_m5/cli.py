# -*- coding: utf-8 -*-
"""命令行入口与单实例校验。"""
import argparse
import subprocess
import sys

from .app import App
from .config import PAGE_DPI
from .selftest import (dpidump, selftest, selftest_link, selftest_menu,
                       selftest_set, selftest_settest, selftest_tray)

_SINGLETON = None


def _instance_pids():
    """当前所有 VK_M5_Monitor.exe 的 PID（onefile 下父+子算两个）"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq VK_M5_Monitor.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "vk_m5_monitor.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass  # 可忽略：幂等清理/UI 收尾
    return pids


def _window_exists():
    """是否已存在本工具的浮窗

    浮窗标题跟着识别到的鼠标型号走（VK M5 / A955 ...），所以不能只按
    「VK M5」这一个标题找，要把已登记型号名都试一遍，否则换成 A955 后
    单实例保护会失效、允许开出第二个浮窗。
    """
    import ctypes
    names = {"VK M5"}
    try:
        from .devices import builtin_a955, load
        names |= {p.get("name") for p in load()}
        names.add(builtin_a955().get("name"))
    except Exception:
        pass  # 可忽略：认不出型号时退回默认标题
    names.discard(None)
    try:
        u = ctypes.windll.user32
        for n in names:
            if u.FindWindowW("TkTopLevel", n) or u.FindWindowW(None, n):
                return True
        return False
    except Exception:
        return False


def single_instance():
    """是否允许启动：已有实例就退出

    为什么不用命名互斥：onefile 是「父进程解包 + 子进程跑界面」两段，父进程
    先占锁会把真正的界面子进程挡在门外，程序就起不来了；父进程又从不建窗口，
    所以这里查两样东西——同名进程数（自身父链最多 2 个）和浮窗是否存在。
    多开的真实危害是两个浮窗叠在同一位置、两套滚轮钩子各处理一次滚动，
    表现为"滚动切页乱跳/时灵时不灵"。
    """
    if len(_instance_pids()) > 2:
        return False
    return not _window_exists()


def _build_parser():
    p = argparse.ArgumentParser(prog="VK_M5_Monitor",
                                description="VK M5 电量 / DPI 悬浮窗（v14）")
    p.add_argument("--selftest", action="store_true", help="基础自检（读一次电量/DPI）")
    p.add_argument("--linktest", action="store_true", help="连接方式识别自检")
    p.add_argument("--menutest", action="store_true", help="右键菜单自检")
    p.add_argument("--traytest", action="store_true", help="托盘注册自检（写 tray_probe.txt）")
    p.add_argument("--settest", action="store_true", help="档位写入自检")
    p.add_argument("--set", type=int, metavar="IDX", help="把当前档位设为 IDX（0-4）")
    p.add_argument("--dpidump", action="store_true", help="连续打印档位表")
    p.add_argument("--secs", type=int, default=30, help="--dpidump 持续时间（秒）")
    p.add_argument("--dpi", action="store_true", help="启动即锁在 DPI 页（验收用）")
    p.add_argument("--uitest", action="store_true", help="隐藏/显示切换冒烟")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)

    # 一次性退出型自检：不进界面
    if args.traytest:
        return selftest_tray()
    if args.selftest:
        return selftest()
    if args.linktest:
        return selftest_link()
    if args.menutest:
        return selftest_menu()
    if args.settest:
        return selftest_settest()
    if args.dpidump:
        return dpidump(args.secs)
    if args.set is not None:
        return selftest_set(args.set)

    if not single_instance():
        # 已有实例在跑：直接退出，避免两个浮窗叠在一起抢滚轮
        return 0

    app = App()
    if args.dpi:
        def _lock_dpi():
            try:
                app.show_page(PAGE_DPI)
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
            try:
                app.root.after(2000, _lock_dpi)
            except Exception:
                pass  # 可忽略：幂等清理/UI 收尾
        app.root.after(900, _lock_dpi)
    if args.uitest:
        app.root.after(4000, app.hide_to_tray)
        app.root.after(9000, app.show_window)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
