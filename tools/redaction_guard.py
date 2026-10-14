#!/usr/bin/env python3
"""脱敏守卫: 真实标识符不许出现在**会随包发出**的地方.

单一事实来源 —— `tests/test_redaction.py`(回归锁) 与 `tools/verify_release.py`
(出包验收) 都从这里取清单与解码逻辑, 免得两处各写一份然后一起漂.

为什么需要它: 真实存档目录名 / accountid 曾长期躺在 `src/` 的 docstring 里,
随包发给了别人. 更麻烦的是 —— **明文扫 exe / 单文件版永远 0 命中**
(单文件版把源码 base64 内嵌, exe 里是压缩 .pyc), 所以"扫不到"完全不能当成
"干净", 必须走**解码级**检查 (`decoded_blobs`).

⚠ 本文件自己就在 `tools/` 里 (= 会随包发出的面), 所以清单**必须以 base64 存放**,
  否则守卫自己就成了泄露源. 末条自检盯着这点.

用法:

    python tools/redaction_guard.py                # 扫默认面 (src/ tools/ packaging/)
    python tools/redaction_guard.py <目录或文件>...  # 扫指定目标 (含解码级)
"""

from __future__ import annotations

import ast
import base64
import sys
import zlib
from pathlib import Path

# 会随包发出的源码面 (build_single_file / build_exe 的内嵌源)
SHIPPED_DIRS: tuple[str, ...] = ("src", "tools")
SHIPPED_EXTRA: tuple[str, ...] = ("packaging",)
_TEXT_SUFFIXES = {".py", ".md", ".bat", ".txt"}
_BINARY_SUFFIXES = {".exe"}


def _real(*encoded: str) -> tuple[str, ...]:
    """真实标识符: 以 base64 存放, 避免本文件自己变成泄露源."""
    return tuple(base64.b64decode(e).decode("utf-8") for e in encoded)


# 真 hex 存档目录名 / 真 accountid / 真 SteamID64
REAL_IDENTIFIERS: tuple[str, ...] = _real(
    "MDExMDAwMDEzZjJkMThjZg==",
    "MTA1OTkyMDA3OQ==",
    "NzY1NjExOTkwMjAxODU4MDc=",
)

# 对外一律用这一组虚构值 (三者必须互相自洽)
FAKE_ACCOUNTID = 1234567890
FAKE_HEX_DIR = "01100001499602d2"
FAKE_SID64 = "76561199194833618"


def shipped_sources(root: Path) -> list[Path]:
    """默认扫描面: 会随包发出的源文件."""
    root = Path(root)
    out: list[Path] = []
    for d in SHIPPED_DIRS:
        base = root / d
        if base.is_dir():
            out.extend(sorted(base.rglob("*.py")))
    for d in SHIPPED_EXTRA:
        base = root / d
        if base.is_dir():
            out.extend(sorted(p for p in base.rglob("*") if p.suffix.lower() in _TEXT_SUFFIXES))
    return [p for p in out if "__pycache__" not in p.parts]


def decoded_blobs(py_source: str) -> list[str]:
    """把单文件版内嵌的模块源码解出来 (AST 取 `_EMBEDDED` → base64 解码).

    明文扫这些文件必然 0 命中, 所以这一步是**唯一**能看出真值的方式.
    不是合法单文件 / 解析失败时返回空列表.
    """
    try:
        tree = ast.parse(py_source)
    except (SyntaxError, ValueError):
        return []
    blobs: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "_EMBEDDED" not in names:
            continue
        for value in node.value.values:
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            try:
                blobs.append(base64.b64decode(value.value.strip()).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 — 不是 base64 的条目直接跳过
                continue
    return blobs


def scan_binary_file(path: Path) -> list[str]:
    """扫 exe: 里面的 `.pyc` 是 **zlib 压缩**的 —— 明文扫必然 0 命中.

    做法: 枚举**合法 zlib 头**(`CMF=0x78` 且 `(CMF*256+FLG)%31==0`), 逐个解压,
    在解出的数据里搜真值. 这就是"明文 grep 扫 exe 恒为 0 命中"这条假阴性的解药.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for i in range(len(data) - 2):
        if data[i] != 0x78:
            continue
        if (data[i] * 256 + data[i + 1]) % 31:  # 合法 zlib 头校验, 挡掉绝大多数误报
            continue
        try:
            out = zlib.decompressobj().decompress(data[i:], 16 << 20)
        except Exception:  # noqa: BLE001 — 不是真 zlib 流
            continue
        if len(out) < 64:
            continue
        for real in REAL_IDENTIFIERS:
            if real not in seen and real.encode("utf-8") in out:
                seen.add(real)
                hits.append(f"{path}: 解压后命中 {real} (exe 内压缩 .pyc)")
    return hits


def scan_targets(targets: list[Path]) -> list[str]:
    """扫给定文件: 文本做明文扫, `.py` 额外解码级扫, `.exe` 走 zlib 解压扫."""
    hits: list[str] = []
    for p in targets:
        p = Path(p)
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in _BINARY_SUFFIXES:
            hits.extend(scan_binary_file(p))
            continue
        if suffix not in _TEXT_SUFFIXES:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        for real in REAL_IDENTIFIERS:
            if real.encode("utf-8") in raw:
                hits.append(f"{p}: 明文命中 {real}")
        if suffix == ".py":
            text = raw.decode("utf-8", "replace")
            for blob in decoded_blobs(text):
                for real in REAL_IDENTIFIERS:
                    if real in blob:
                        hits.append(f"{p}: 解码后命中 {real} (内嵌源码)")
    return hits


def scan_tree(root: Path, *, everything: bool = False) -> list[str]:
    """扫一个目录.

    `everything=True`: 扫目录下所有文本文件**与 exe** (出包验收用, 面向"用户解压出来的那一份");
    否则只扫默认发货面 (源码回归锁用).
    """
    root = Path(root)
    if not root.is_dir():
        return []
    if everything:
        targets = [
            p
            for p in sorted(root.rglob("*"))
            if p.is_file()
            and p.suffix.lower() in (_TEXT_SUFFIXES | _BINARY_SUFFIXES)
            and "__pycache__" not in p.parts
        ]
    else:
        targets = shipped_sources(root)
    return scan_targets(targets)


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:]) or []
    root = Path(__file__).resolve().parent.parent
    if args:
        hits: list[str] = []
        for a in args:
            p = Path(a)
            hits.extend(scan_targets([p]) if p.is_file() else scan_tree(p, everything=True))
    else:
        hits = scan_tree(root)
    if hits:
        print(f"[guard] 命中 {len(hits)} 处真实标识符:")
        for h in hits:
            print("   " + h)
        return 1
    print("[guard] 干净: 未发现真实标识符 (含解码级检查).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
