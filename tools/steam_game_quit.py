# -*- coding: utf-8 -*-
"""game_quit.py —— 给指定进程的窗口发按键 (默认 Alt+F4).

复用 send_console.py 里的 ctypes 键盘输入原语(本环境 Add-Type / COM 均被拦截).
按进程映像名匹配窗口, 不靠窗口标题(中文/特殊字符会翻车).

用法:
    python game_quit.py DarkSoulsIII            # Alt+F4
    python game_quit.py DarkSoulsIII --list     # 只看匹配到哪些窗口
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])

import steam_console_send as sc  # noqa: E402


VK_MENU = 0x12  # ALT
VK_F4 = 0x73


def find_by_exe(substr: str) -> list[tuple[int, int, str]]:
    hits = []
    for hwnd, pid, title in sc.list_windows():
        exe = sc.process_exe(pid).lower()
        if substr.lower() in exe:
            hits.append((hwnd, pid, title))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python game_quit.py <进程名片段> [--list]")
        return 2
    needle = argv[1]
    hits = find_by_exe(needle)
    if not hits:
        print(f"[失败] 没找到映像名含 {needle!r} 的可见窗口")
        return 3
    for hwnd, pid, title in hits:
        print(f"[匹配] hwnd={hwnd} pid={pid} title={title!r}")
    if "--list" in argv:
        return 0

    hwnd, pid, title = hits[0]
    sc.focus(hwnd)
    time.sleep(0.4)
    sc._send(
        sc._key_event(vk=VK_MENU),
        sc._key_event(vk=VK_F4),
        sc._key_event(vk=VK_F4, flags=sc.KEYEVENTF_KEYUP),
        sc._key_event(vk=VK_MENU, flags=sc.KEYEVENTF_KEYUP),
    )
    print(f"[完成] 已向 hwnd={hwnd} 发送 Alt+F4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
