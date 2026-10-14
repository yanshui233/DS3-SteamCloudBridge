"""把 Steam 客户端**分离启动**（供 trigger-sync 在写完存档后自己拉起 Steam）。

## 为什么要"分离"

本进程（`bridge.py`）跑完就退出。如果 Steam 是它的子进程，父进程退出的那一刻
子进程可能被一起带走 —— 实测见过"起来一秒又没了"。所以：

* `DETACHED_PROCESS`（0x8）—— 不继承控制台，脱离本进程的作业/控制台关联；
* `CREATE_NEW_PROCESS_GROUP`（0x200）—— 独立进程组，不被 Ctrl 事件波及；
* 三个标准流全部 `DEVNULL` —— Steam 是 GUI 程序，不需要我们的管道。

## 只做 Windows

非 Windows 返回失败（`False`），由调用方当成"没有启动能力"处理。

## 不做的事

**不**传 `-offlinemode` / `-offline` —— 实测本 build 两个开关都被忽略
（`ExecCommandLine` 确认已传入，Steam 仍照常在线），传了只会让人以为切了离线。
要离线只有 Steam 自己的 UI 菜单一条路（见 `skill: steam-autocloud-save-transfer` §5.2）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["steam_exe_path", "start_steam", "spawn_detached"]

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def steam_exe_path(steam_root: str | Path) -> Path:
    """给定 Steam 根目录，返回 `steam.exe` 的路径（Windows）。"""
    root = Path(steam_root)
    for name in ("steam.exe", "Steam.exe"):
        p = root / name
        if p.is_file():
            return p
    return root / "steam.exe"


def spawn_detached(argv: list[str], *, cwd: str | Path | None = None) -> int | None:
    """分离启动一个进程，返回其 PID；失败返回 `None`。

    只投递、不等待 —— 调用方自行轮询进程是否真的存在
    （GUI 程序"投递成功"和"真的起来了"是两件事）。
    """
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=str(cwd) if cwd else None,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )
    except OSError:
        return None
    return int(proc.pid)


def start_steam(
    steam_root: str | Path,
    *,
    extra_args: list[str] | None = None,
    spawn=spawn_detached,
) -> bool:
    """分离启动 Steam（不带任何会改变行为的开关）。返回"投递是否成功"。

    ⚠ 返回值**只代表命令投递成功**，不代表 Steam 真的起来了 —— 那要看进程轮询。
    """
    exe = steam_exe_path(steam_root)
    if not exe.is_file():
        return False
    argv = [str(exe), *list(extra_args or [])]
    return spawn(argv, cwd=exe.parent) is not None


def wait_for_process(
    image_name: str,
    *,
    timeout_seconds: float = 150.0,
    poll_seconds: float = 2.0,
    find_pids=None,
    sleep=time.sleep,
) -> tuple[bool, float]:
    """轮询等某个映像名出现。返回 `(是否已出现, 实际等待秒数)`。"""
    from . import processes as processes_mod  # 局部导入，避免环

    lister = find_pids or processes_mod.find_pids
    waited = 0.0
    if lister(image_name):
        return True, 0.0
    while waited < timeout_seconds:
        step = min(poll_seconds, timeout_seconds - waited) if poll_seconds > 0 else timeout_seconds
        sleep(step)
        waited += step
        if lister(image_name):
            return True, waited
    return False, waited
