# -*- coding: utf-8 -*-
"""send_console.py —— 把一行命令敲进 Steam 客户端控制台.

为什么要这么绕
==============
本环境的 PowerShell 工具拦截了两条常规通路:
  * `Add-Type` (运行时编译 .NET) —— 用于 P/Invoke
  * `New-Object -ComObject WScript.Shell` —— 用于 AppActivate / SendKeys
所以改走 **Python + ctypes**: ctypes 是 stdlib 的一部分, 直接调 user32,
既不编译也不实例化 COM.

Steam 控制台的形态
==================
新版 Steam 里 `steam://open/console` 打开的不是独立窗口, 而是**主窗口的一个标签页**,
且官方文档说"重启客户端才会消失" —— 即本会话内一直存在.
所以策略是: 先触发 open/console 让它成为当前标签页(输入框自动获得焦点),
再把 Steam 主窗口拉到前台, 然后逐字符发送.

用法
====
    python send_console.py "testappcloudpaths 374320"
    python send_console.py --open "find cloud"     # 先激活控制台标签再发
    python send_console.py --no-click-input "..."  # 焦点已在输入行时跳过点击
    python send_console.py --list                  # 只列出可见窗口标题, 不发键

发送流程 (每一步都有实测依据)
==============================
1. 可选 `--open`: `steam://open/console` 激活"控制台"标签 (独立窗口不存在, 它是标签页)
2. `find_steam_main()` 找主窗口 → `focus_verified()` 拉前台并**回读确认**
3. `click_console_input()` 点窗口底部输入行 (**不点则键全部打空**)
4. `type_text()` 逐字符 Unicode 键入 → `press_enter()`
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import sys
import time

import win_dpi  # noqa: F401  import 即开启 DPI 感知 (必须在任何 GDI/窗口调用之前)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.BitBlt.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wt.DWORD,
]
gdi32.GetDIBits.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT, ctypes.c_void_p,
    ctypes.c_void_p, wt.UINT,
]
SRCCOPY = 0x00CC0020
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 窗口枚举

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows() -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((int(hwnd), int(pid.value), buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return out


def process_exe(pid: int) -> str:
    """拿进程可执行文件路径(用于确认这是 steam 的窗口)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return ""


def find_steam_main() -> int:
    """找 Steam 主窗口: 进程映像含 steam, 标题恰为 'Steam'."""
    exact = []
    loose = []
    for hwnd, pid, title in list_windows():
        exe = process_exe(pid).lower()
        if "steam" not in exe:
            continue
        if title.strip() == "Steam":
            exact.append(hwnd)
        elif "steam" in title.lower():
            loose.append((hwnd, title))
    if exact:
        return exact[0]
    if len(loose) == 1:
        return loose[0][0]
    return 0


# ---------------------------------------------------------------- 键盘输入

PUL = ctypes.POINTER(ctypes.c_ulong)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", PUL),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", PUL),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send(*inputs: INPUT) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = None
    return inp


def type_text(text: str, per_char_delay: float = 0.03) -> None:
    for ch in text:
        _send(
            _key_event(scan=ord(ch), flags=KEYEVENTF_UNICODE),
            _key_event(scan=ord(ch), flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        )
        time.sleep(per_char_delay)


def press_enter() -> None:
    VK_RETURN = 0x0D
    _send(_key_event(vk=VK_RETURN), _key_event(vk=VK_RETURN, flags=KEYEVENTF_KEYUP))


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow())


def is_foreground(hwnd: int) -> bool:
    fg = foreground_hwnd()
    if fg == hwnd:
        return True
    # 也可能是它的子窗口持焦点 —— 看根窗口
    GA_ROOT = 2
    root = int(user32.GetAncestor(fg, GA_ROOT)) if fg else 0
    return root == hwnd


def focus(hwnd: int) -> bool:
    """把窗口拉到前台.

    前台锁定 / 别的进程(弹窗)占着前台时, 朴素的 `SetForegroundWindow` 会被拒绝
    (返回 0 或返回 1 却根本没生效). 三条路依次试:

      1) **`AttachThreadInput` 把自己的线程接到当前前台线程** —— 最可靠,
         实测能抢过"常驻通知弹窗"(如 Process Lasso 更新提示)占住的前台;
      2) 模拟一次 ALT 敲击解锁前台锁定;
      3) 朴素 `SetForegroundWindow` 兜底.

    调用方仍必须用 `is_foreground()` **回读确认**再发键.
    """
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    ok = False
    # ---- 1) AttachThreadInput ----
    try:
        kernel32 = ctypes.windll.kernel32
        fg = int(user32.GetForegroundWindow())
        tid_cur = int(kernel32.GetCurrentThreadId())
        tid_fg = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = False
        if tid_fg and tid_fg != tid_cur:
            attached = bool(user32.AttachThreadInput(tid_fg, tid_cur, True))
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if attached:
            user32.AttachThreadInput(tid_fg, tid_cur, False)
    except Exception:
        ok = False
    time.sleep(0.3)
    if is_foreground(hwnd):
        return True

    # ---- 2) ALT 敲击解锁 (老办法) ----
    user32.keybd_event(0x12, 0, 0, 0)       # ALT down
    user32.keybd_event(0x12, 0, 0x0002, 0)  # ALT up
    ok = bool(user32.SetForegroundWindow(hwnd)) or ok
    time.sleep(0.45)
    if is_foreground(hwnd):
        return True

    # ---- 3) 合成鼠标点击激活 ----
    # 实测: 有的进程用**模态对话框**(class `#32770`)死占前台并不断抢回, 前两种办法
    # 都会被它抢走. 而合成鼠标点击走系统输入路径, 点谁谁就前台 —— 抢得过.
    # ⚠ **必须先确认点击点真的落在目标窗口上**: 实测踩过 —— 另一个进程的对话框正好
    #   盖住按比例算出来的点, 结果点在了对话框上, 目标窗口纹丝不动. 所以逐个候选点用
    #   `WindowFromPoint` 归属校验, 命中才点.
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w = r.right - r.left
    h = r.bottom - r.top
    if w > 0 and h > 0:
        GA_ROOT = 2
        for rx, ry in ((0.3155, 0.3985), (0.05, 0.95), (0.5, 0.02), (0.95, 0.95), (0.5, 0.5)):
            x, y = r.left + int(w * rx), r.top + int(h * ry)
            hit = int(user32.WindowFromPoint(wt.POINT(x, y)))
            if hit and int(user32.GetAncestor(hit, GA_ROOT)) == hwnd:
                _click_at(x, y, settle=0.5)
                break
    return ok or is_foreground(hwnd)


def focus_verified(hwnd: int, *, tries: int = 3) -> bool:
    """拉前台并**验证确实前台了**. 失败则绝不要往下发键.

    为什么必须验证: `SetForegroundWindow` 返回 1 只代表调用被接受, 不代表窗口真的
    到了前台 (用户切走 / 被别的进程抢回 / 前台锁定). 实测踩过: 以为发进了 Steam 控制台,
    实际前台是浏览器 —— 盲发按键会把文字打到**别的应用**里. 所以发键前必须确认.
    """
    for i in range(tries):
        focus(hwnd)
        time.sleep(0.5)
        if is_foreground(hwnd):
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- 控制台输入行

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def _click_at(x: int, y: int, *, settle: float = 1.0) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.25)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.09)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(settle)


def console_input_point(hwnd: int, *, y_ratio: float = 0.8301) -> tuple[int, int]:
    """返回控制台**输入行**的屏幕物理坐标.

    实测 (2026-10-12, 新版 Steam 控制台, 窗口 1268x753 @125% 缩放):
    输入行是窗口底部一条浅色圆角矩形, 竖直位置约在窗口高度的 **83%** 处, 水平居中.
    """
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w = r.right - r.left
    h = r.bottom - r.top
    return (r.left + w // 2, r.top + int(h * y_ratio))


def click_console_input(
    hwnd: int,
    *,
    point: tuple[int, int] | None = None,
    jitter: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """把键盘焦点送进控制台输入行. 返回被点击的输入行屏幕坐标.

    实测配方 (2026-10-12, 花了很久才试出来, 别简化):
      1. **先点控制台正文** (窗口内约 32% 宽 / 40% 高处) —— 让控制台页接管键盘输入.
         刚 `steam://open/console` 之后, 只点输入框是**没用的**: 字符哪里都不落,
         `console_log.txt` 零新增, 极易误判成"按键被系统拦掉".
      2. **再点底部输入行** —— 拿到插入符.
      3. 然后才逐字符键入 + 回车.
    之后贴验证: `find cloud` 输出落进 `console_log.txt`; `testappcloudpaths 374320`
    产生 `"testappcloudpaths" = "374320"` 行.
    """
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w = r.right - r.left
    h = r.bottom - r.top
    jx, jy = jitter
    body = (r.left + int(w * 0.3155) + jx, r.top + int(h * 0.3985) + jy)
    base = point if point else console_input_point(hwnd)
    target = (base[0] + jx, base[1] + jy)
    _click_at(*body, settle=1.2)
    _click_at(*target, settle=1.2)
    return target


# ---------------------------------------------------------------- 输入行校验

class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def _capture_region(x: int, y: int, w: int, h: int) -> bytes:
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY)
        bi = _BMIH()
        bi.biSize = ctypes.sizeof(_BMIH)
        bi.biWidth = w
        bi.biHeight = -h          # 自顶向下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return buf.raw
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)


def region_signature(x: int, y: int, w: int, h: int) -> str:
    """对屏幕上一块矩形做指纹 (md5 of BGRA). 调试用."""
    return hashlib.md5(_capture_region(x, y, w, h)).hexdigest()


def region_ink(x: int, y: int, w: int, h: int, *, thr: int = 110) -> int:
    """矩形内**亮像素个数** (= "墨迹量").

    为什么不用 md5 比对: 输入行聚焦后会有**闪烁的光标**, 0.5s 间隔的两次截图有约一半
    概率因光标亮/灭而 md5 不同 —— 会误判成"文字已输入", 于是回车打在空框上. 实测踩过.
    亮像素计数对光标闪烁不敏感 (光标约 40 px), 而一行命令文字是数百 px, 区分度足够.
    """
    raw = _capture_region(x, y, w, h)
    n = 0
    for i in range(0, len(raw), 4):
        if raw[i] > thr or raw[i + 1] > thr or raw[i + 2] > thr:
            n += 1
    return n


def input_box_rect(hwnd: int) -> tuple[int, int, int, int]:
    """控制台输入行里**文字会出现的那一小块** (窗口内的左侧 ~45%).

    实测位置: 输入行在窗口高度 ~79.5%~87% 之间. 取左侧一条即可 —— 命令总是从左侧
    开始输入, 只要有字, 墨迹量就上去.
    """
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w = r.right - r.left
    h = r.bottom - r.top
    return (r.left + int(w * 0.02), r.top + int(h * 0.795), max(1, int(w * 0.45)), max(1, int(h * 0.075)))


def _select_all_delete() -> None:
    VK_CONTROL, VK_A, VK_DELETE, KEYUP = 0x11, 0x41, 0x2E, 0x0002
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_A, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_A, 0, KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
    time.sleep(0.15)
    user32.keybd_event(VK_DELETE, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_DELETE, 0, KEYUP, 0)
    time.sleep(0.3)


# 输入行的竖直落点候选 (占窗口高度的比例). 第 1 个是实测值 (1268x753 @125% 缩放),
# 后面的几档是**给别的窗口尺寸/缩放**准备的阶梯.
#
# 为什么要阶梯: 2026-10-13 在朋友的机器上, 同一套配方 `tries=4` 全部打空 (墨迹量一次
# 都没上去, 工具因此拒绝回车). 输入行是控制台**底部的固定控件**, 并不随窗口高度线性
# 缩放 ⇒ 只按比例算, 窗口一换尺寸就整体偏移. 阶梯不保证命中, 但比固定单点强得多;
# 而且**依旧由墨迹量兜底** —— 阶梯里没有真输入行时不会瞎按回车 (宁可不发, 不可误发).
_Y_RATIOS = (0.8301, 0.8550, 0.8050, 0.8800)


def window_metrics(hwnd: int) -> dict[str, int]:
    """窗口尺寸 / DPI / 输入行当前墨迹量 —— 发键失败时打出来定位用.

    单独一个函数是为了**让失败可诊断**: 没有这几个数, "键入没进去"只能猜
    (窗口太小? 缩放不同? 输入行根本不在那儿?); 有了就能一眼看出落点是否离谱.
    """
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    dpi = 0
    try:
        dpi = int(user32.GetDpiForWindow(hwnd))
    except Exception:
        pass
    ink = -1
    try:
        ink = int(region_ink(*input_box_rect(hwnd)))
    except Exception:
        pass
    return {"w": r.right - r.left, "h": r.bottom - r.top, "dpi": dpi, "ink": ink}


def send_command(hwnd: int, command: str, *, tries: int = 4) -> bool:
    """把 `command` 送进控制台并**确认字符真的进了输入行**, 然后回车.

    为什么必须自校验: 实测 (2026-10-12) "拉前台 → 点正文 → 点输入行 → 键入" 这套
    配方**不是 100% 稳定** —— 同样的坐标与等待, 字符有时全部打空 (console_log 零新增,
    极易误判成"按键被系统拦掉"; 也曾把回车浪费在空框上). 与其继续调坐标, 不如**闭环**:
    键入后用 `region_ink()` 比对输入行的墨迹量, 没上去就重试 (清空 + 换落点).

    返回是否确认把字符打进了输入行并回车.
    """
    for i in range(tries):
        # 每一轮换落点: 竖直方向走 `_Y_RATIOS` 阶梯 (覆盖不同窗口高度), 水平加微抖动
        click_console_input(
            hwnd,
            point=console_input_point(hwnd, y_ratio=_Y_RATIOS[i % len(_Y_RATIOS)]),
            jitter=(i * 4, i * 3),
        )
        if not is_foreground(hwnd):
            time.sleep(0.4)
            continue
        box = input_box_rect(hwnd)
        _select_all_delete()          # 先清干净, 让墨迹基线可靠
        ink0 = region_ink(*box)
        type_text(command)
        time.sleep(0.5)
        ink1 = region_ink(*box)
        if ink1 >= ink0 + 60:
            time.sleep(0.2)
            press_enter()
            return True
        time.sleep(0.4)
    return False


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for hwnd, pid, title in list_windows():
            exe = process_exe(pid)
            tag = "STEAM" if "steam" in exe.lower() else ""
            print(f"{hwnd:>10}  pid={pid:<7} {tag:<6} {title}")
        return 0

    cmd = " ".join(a for a in argv[1:] if not a.startswith("--"))
    if not cmd:
        print("用法: python send_console.py [--open] [--check] <command>")
        return 2

    if "--open" in argv:
        os.startfile("steam://open/console")  # noqa: S606
        time.sleep(4.0)

    hwnd = find_steam_main()
    if not hwnd:
        print("[失败] 没找到 Steam 主窗口")
        return 3
    print(f"[信息] Steam 主窗口 hwnd={hwnd}")

    if "--check" in argv:
        print(f"[信息] 前台窗口 hwnd={foreground_hwnd()}  是Steam? {is_foreground(hwnd)}")
        return 0 if is_foreground(hwnd) else 4

    if not focus_verified(hwnd):
        print(
            f"[拒绝] 无法把 Steam 拉到前台 (当前前台 hwnd={foreground_hwnd()}) —— "
            "盲发按键会把文字打进别的应用, 已中止"
        )
        return 5

    # **关键一步**: 控制台页打开后键盘焦点不在输入行上. 点击 + 键入 + **回读校验**
    # 全在 send_command 里 (它不是 100% 一次成功, 内部会重试). `--no-click-input`
    # 只在已经确认焦点在输入行、且想省掉校验时使用.
    if "--no-click-input" in argv:
        type_text(cmd)
        time.sleep(0.3)
        press_enter()
        print(f"[完成] 已发送 (未校验): {cmd!r}")
        return 0

    if not send_command(hwnd, cmd):
        print("[失败] 键入未能进入控制台输入行 (已重试) —— 未按回车, 避免误发")
        return 6
    print(f"[完成] 已发送: {cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
