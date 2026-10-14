# -*- coding: utf-8 -*-
"""win_dpi.py —— 让当前进程成为 DPI 感知 (per-monitor aware v2).

为什么必须做
============
实测 (2026-10-12, 本机 1536x864 逻辑 / 1920x1080 物理 @125%):
  * `GetDpiForWindow(steam_hwnd) = 120` (125% 缩放)
  * 本进程 `GetProcessDpiAwareness = 0` (**DPI unaware**)
于是同一扇窗口拿到的几何被拆成两套坐标系, **互相差 1.25 倍**:
  * `GetWindowRect`         = (212, 59, 1226, 661)      <- 虚拟坐标 (物理 x0.8)
  * `DWMWA_EXTENDED_FRAME`  = (265, 74, 1533, 827)      <- 物理坐标
  * `SM_CXSCREEN`           = 1536                      <- 虚拟 (物理 1920)
结果: 截图 (window-relative) 与 `SetCursorPos`/`SendInput` 用的坐标系不一致,
**点击会偏**, 而且 `SetForegroundWindow` 看似成功、按键却落到别处.

修法: 在创建任何 DC / 窗口之前把进程切成 per-monitor v2 → 全流程统一物理像素.
必须在使用 user32/GDI **之前**调用; 所以放在模块 import 顶部.

用法
====
    from win_dpi import enable_dpi_awareness
    enable_dpi_awareness()
"""

from __future__ import annotations

import ctypes


def enable_dpi_awareness() -> str:
    """把当前进程切成 DPI 感知. 返回实际生效的方式 (便于日志)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # 1) Win10 1703+: per-monitor v2  (-4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-3)):
            return "per-monitor"
    except Exception:
        pass
    # 2) Win8.1+: shcore
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return "shcore-per-monitor"
    except Exception:
        pass
    # 3) Vista+: system aware (最差也能拿到物理像素的比例一致)
    try:
        if user32.SetProcessDPIAware():
            return "system-aware"
    except Exception:
        pass
    return "none"


# import 即生效 —— 保证后续任何 GDI/窗口调用都拿到物理坐标
ACTIVE = enable_dpi_awareness()

if __name__ == "__main__":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    print("dpi awareness ->", ACTIVE)
    print("SM_CXSCREEN/CYSCREEN =", user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
