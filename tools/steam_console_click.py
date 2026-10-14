# -*- coding: utf-8 -*-
"""steam_console_click.py —— 点击 Steam 控制台区域使其获得输入焦点, 再键入命令.

为什么需要它
============
实测 (2026-10-12): `steam://open/console` 会把"控制台"标签页打开, 但**输入焦点不在
控制台的输入行**上 —— 此时用 SendInput 打字符, 字进了 Steam 主窗口却哪儿都不落,
`console_log.txt` 也不会新增任何行 (连 `find <kw>` 这种必定输出的命令都测不到).
所以必须先**在控制台区域内点一下鼠标**, 把焦点交给它, 再发键.

实现仍是 Python + ctypes 直调 user32 (`Add-Type` / `WScript.Shell` 在本环境被拦).
坐标默认取窗口内相对点, 可用 `--at x y` 覆盖.

用法
====
    python steam_console_click.py "testappcloudpaths 374320"
    python steam_console_click.py --at 500 400 "find cloud"
    python steam_console_click.py --probe-only        # 只点击不键入, 用于调焦点
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import steam_console_send as sc  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
SM_CXSCREEN = 0
SM_CYSCREEN = 1


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def set_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)


def left_click() -> None:
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def main(argv: list[str]) -> int:
    at = None
    probe_only = "--probe-only" in argv
    rest: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--at":
            at = (int(argv[i + 1]), int(argv[i + 2]))
            i += 3
            continue
        if a.startswith("--"):
            i += 1
            continue
        rest.append(a)
        i += 1
    cmd = " ".join(rest)

    hwnd = sc.find_steam_main()
    if not hwnd:
        print("[失败] 没找到 Steam 主窗口")
        return 3
    left, top, right, bottom = window_rect(hwnd)
    win_w, win_h = right - left, bottom - top
    print(f"[信息] Steam hwnd={hwnd} rect=({left},{top})-({right},{bottom}) {win_w}x{win_h}")

    if not sc.focus_verified(hwnd):
        print(f"[拒绝] 无法把 Steam 拉到前台 (当前前台={sc.foreground_hwnd()}) —— 已中止")
        return 5

    # 默认点: 窗口水平中央, 垂直靠下 (控制台内容区空白的中间偏下, 避开导航条与顶部链接)
    cx, cy = at if at else (win_w // 2, int(win_h * 0.72))
    sx, sy = left + cx, top + cy
    print(f"[信息] 点击窗口内 ({cx},{cy}) -> 屏幕 ({sx},{sy})")
    set_cursor(sx, sy)
    time.sleep(0.2)
    left_click()
    time.sleep(0.5)

    if probe_only or not cmd:
        print("[完成] 仅点击 (未键入)")
        return 0

    if not sc.is_foreground(hwnd):
        print(f"[拒绝] 点击后 Steam 失去前台 (当前前台={sc.foreground_hwnd()}) —— 已中止")
        return 5

    sc.type_text(cmd)
    time.sleep(0.3)
    sc.press_enter()
    print(f"[完成] 已点击并发送: {cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
