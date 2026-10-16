"""版本号 —— **全项目唯一来源**.

为什么单独一个模块: 这个号出现在四个地方 —— 探测器 `--version` 输出、
发行包名与 `VERSION.txt`、搬运器 `--version`、以及内存里的实验记录 ——
过去靠"改的时候记得一起改"来维持, 已经出现过对不上的情况.
现在只在这里改一次, 其余全部 import 它.
"""

from __future__ import annotations

VERSION = "1.3.0"

__all__ = ["VERSION"]
