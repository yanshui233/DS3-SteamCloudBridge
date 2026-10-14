"""Windows 进程事实: 找 PID / 读进程**启动时间**.

## 为什么需要它 (M3 会话边界的兜底)

`src/transport/cloudchannel.py` 用 `cloud_log.txt` 里的 `Client version:` 行当会话边界.
但**Steam 启动时并不立刻写这一行** —— 实测 (2026-10-12 14:12 本机):

    steam.exe -shutdown 后重启, `cloud_log.txt` **零增长**, 没有新的 `Client version:`;
    `console_log.txt` 也没有新的 `Console Log Start`.

后果: 上一会话的"已点亮"证据仍落在"最后一次边界之后", 被当成本会话的 →
**假报"已点亮"** (`testappcloudpaths` 重启即失效, 其实本会话根本没点亮) →
`sync` 会带着"以为点亮"的假设去触发, 然后从陈旧日志里读出"成功".

`steam.exe` 的进程启动时间是个**不会撒谎**的边界: 它晚于日志里最后一条会话边界,
就说明"日志里那个会话已经过去了, 本会话还没留下任何记录".

## 只做 Windows

非 Windows / 取不到时一律返回 `None` (调用方按"没有这个兜底"处理, 不误判).
"""

from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as wt
import io
import os
import subprocess
import sys

__all__ = ["find_pids", "process_start_epoch", "steam_start_epoch"]

_ENV_STEAM_START = "SCB_STEAM_START_EPOCH"
"""测试钩子: 显式钉死 `steam.exe` 启动时间 (epoch 秒). `0` = 关掉这个兜底.

隔离测试里跑的是**假 Steam 环境**, 但真机 Steam 可能正好在跑 —— 不钉死的话
`steam_start_epoch()` 会返回真机时间, 把假日志的会话判成过期. 与 `SCB_STEAM_RUNNING`
同一套约定 (只影响这一项).
"""

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_FILETIME_EPOCH_DELTA = 11644473600.0
"""1601-01-01 -> 1970-01-01 的秒数."""


def find_pids(image_name: str) -> list[int]:
    """按**映像名**列 PID (大小写不敏感). 失败返回 `[]`.

    用 `tasklist /FO CSV`: 它的内存字段带千位逗号 (`"112,396 K"`), 必须走 csv 解析,
    不能裸 split(',') —— 实测踩到过.
    """
    if sys.platform != "win32":
        return []
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
        )
    except OSError:
        return []
    text = r.stdout.decode("gbk", "replace")
    if "INFO:" in text:
        return []
    pids: list[int] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().lower() == image_name.lower():
            try:
                pids.append(int(row[1]))
            except (TypeError, ValueError):
                continue
    return pids


def process_start_epoch(pid: int) -> float | None:
    """进程创建时间的 Unix 时间戳 (秒). 取不到返回 `None`."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation = wt.FILETIME()
        exit_time = wt.FILETIME()
        kernel = wt.FILETIME()
        user = wt.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
    finally:
        kernel32.CloseHandle(handle)
    ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return ticks / 1e7 - _FILETIME_EPOCH_DELTA


def steam_start_epoch(image_name: str = "steam.exe") -> float | None:
    """`steam.exe` 的启动时间 (多个 PID 时取**最早**). 没有进程返回 `None`.

    测试钩子 `SCB_STEAM_START_EPOCH`: 设成数字即直接返回该值; 设成 `0`/负数返回 `None`
    (等价"没有这个兜底"), 便于隔离测试里把真机 Steam 的影响排除掉.
    """
    override = os.environ.get(_ENV_STEAM_START)
    if override is not None:
        try:
            val = float(override)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None
    starts = [t for t in (process_start_epoch(p) for p in find_pids(image_name)) if t is not None]
    return min(starts) if starts else None
