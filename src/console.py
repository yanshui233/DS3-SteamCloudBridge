"""控制台加固 —— 唯一的实现, 给探测器和搬运器共用.

背景见 `harden_console` 的 docstring. 抽出来是因为它本来在 `tools/machine_probe.py`
里, 搬运器 (`tools/bridge.py`) 也需要同样的保护: 它的输出里同样有
`DARK SOULS™ III` 这种游戏名, 在 GBK 控制台上 `print` 会抛
`UnicodeEncodeError` 把进程干掉.

保持向后兼容: `machine_probe._harden_console` 仍然存在 (只是改成 import 别名),
`tests/test_probe.py` 里的 4 个用例继续有效.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

__all__ = ["harden_console"]


def harden_console(streams: Any = None) -> list[str]:
    """让 stdout/stderr 不会因编码问题崩溃, 返回被修正的流名 (便于测试).

    背景: 中文 Windows 控制台默认代码页 GBK(cp936), 而 Steam 游戏名里常见
    `™`(U+2122)、`é` 这类字符 —— `print("DARK SOULS™ III")` 会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2122'`,
    整个进程直接挂掉. 实测: 打包成 exe 后在干净目录双击必崩, 且报告只写了一半.

    只改 `errors`, **不改 encoding** —— 这样:
      * 走 `RUN-*.bat` (已 `chcp 65001`) 时中文照常显示;
      * 直接双击 exe 时中文也正常, 只有 `™` 退化成一个 `?`;
      * 无论如何都不会因为一个字符崩掉.
    """
    targets: Iterable[Any] = (sys.stdout, sys.stderr) if streams is None else tuple(streams)
    fixed: list[str] = []
    for idx, stream in enumerate(targets):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")
            fixed.append(getattr(stream, "name", None) or f"stream{idx}")
        except Exception:  # noqa: BLE001
            # 某些被重定向的流没有 reconfigure; 忽略即可, 不要因此再抛
            pass
    return fixed
