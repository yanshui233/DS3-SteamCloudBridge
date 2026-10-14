"""SteamCloudBridge 图形向导包 (v1.1.2).

对外只暴露一个 `run()`: 打开向导窗口. 导入本包**不会**碰 tkinter ——
真正导入界面层推迟到 `run()` 里, 这样在无图形环境的机器上
`import src.gui` 依然安全 (CI / 单文件构建期的依赖扫描都会走到这里).
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["run"]


def run(argv: Sequence[str] | None = None) -> int:
    """启动图形向导; 返回进程退出码."""
    from .app import run as _run

    return int(_run(list(argv) if argv is not None else None) or 0)


def __getattr__(name: str) -> Any:  # pragma: no cover - 仅用于友好报错
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}; 请用 run()")
