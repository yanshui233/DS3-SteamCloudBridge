#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形向导入口 —— 双击就是一个软件.

    SteamCloudBridgeGUI.exe                      打开向导 (打包后)
    python tools/bridge_gui.py                   同样效果 (仓库里)
    python tools/bridge_gui.py --smoke           只把界面建起来然后退出 (自检)
    python tools/bridge_gui.py --appid 374320    指定游戏
    python tools/bridge_gui.py --lang en         指定界面语言 (zh / en)
    python tools/bridge_gui.py --version

不传 `--lang` 时按"系统语言"自动选: 中文系统给中文, 其它一律给英文 (面向海外的
默认). 用户在界面上点选的语言会存进 `%APPDATA%\\SteamCloudBridge\\settings.json`,
下次打开还记得.

**为什么另开一个入口而不是给 `bridge.py` 加 `--gui`**: 那个是 CLI, 打包时保留
控制台窗口; 向导要的是 windowed (没有黑框). 两个 exe 各管一头, 共享同一套 `src/*`,
所以判据只有一份.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.console import harden_console  # noqa: E402
from src.i18n import LANGS  # noqa: E402
from src.version import VERSION  # noqa: E402


def _emit(text: str) -> None:
    """写一行字 —— windowed exe 下 stdout/stderr 可能是 None, 不能直接 print."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.write(text + "\n")
            stream.flush()
            return
        except Exception:
            continue


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="SteamCloudBridgeGUI",
        description="黑魂存档同步 · 图形向导 (一步步引导)",
    )
    ap.add_argument("--appid", default=None, help="目标游戏 appid (默认 374320)")
    ap.add_argument("--page", default=None, type=int, help="直接打开第几步 (1-5)")
    ap.add_argument(
        "--lang",
        default=None,
        choices=list(LANGS),
        help="界面语言 (zh / en); 不传就按系统语言自动选",
    )
    ap.add_argument("--smoke", action="store_true", help="建好窗口后立刻退出 (打包自检用)")
    ap.add_argument("--version", action="store_true", help="打印版本号")
    args = ap.parse_args(argv)

    if args.version:
        _emit(VERSION)
        return 0

    harden_console()

    # DPI 感知必须在这里声明 —— 早于任何窗口/DC 的创建.
    # 不声明的话 Windows 会把整个窗口当位图拉伸 1.25 倍: 字发虚, 而且
    # `GetWindowRect`(物理像素) 与 tk 的绘制坐标(逻辑像素) 错位, 窗口底部的
    # 按钮栏会被裁掉. 实测踩过.
    try:
        import win_dpi  # noqa: F401  import 即生效

        _dpi_mode = win_dpi.ACTIVE
    except Exception:  # pragma: no cover - 非 Windows
        _dpi_mode = "n/a"

    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # pragma: no cover - 精简系统上才会发生
        _emit(f"这台机器缺少图形界面组件 (tkinter): {exc}")
        _emit("可以改用命令行版本 SteamCloudBridgeBridge.exe, 功能是一样的。")
        return 2

    from src.gui.app import run as run_gui

    passthrough: list[str] = []
    if args.appid:
        passthrough += ["--appid", str(args.appid)]
    if args.page is not None:
        passthrough += ["--page", str(max(1, args.page) - 1)]
    if args.lang:
        passthrough += ["--lang", args.lang]
    if args.smoke:
        passthrough.append("--smoke")
    return run_gui(passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
