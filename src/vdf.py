"""Valve Data Format (VDF / KeyValues) 解析器.

Steam 的 `.vdf` / `.acf` 文件都是 KeyValues 文本格式, 结构为:

    "root"
    {
        "key"       "value"
        "sub"
        {
            "k2"    "v2"
        }
    }

本模块只做只读解析, 不依赖任何第三方库, 不做任何写回.
解析结果: dict[str, str | dict]. 值若是字符串则存 str, 若是块则存 dict.
键名统一保留原始大小写 (Steam 用 `appid` 小写和 `AppID` 混用, 由调用方处理).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["parse", "parse_file", "walk", "first_key"]

_TOKEN_RE = re.compile(
    r"""
      (?P<comment>//[^\n]*)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<brace>[{}])
    | (?P<ws>\s+)
    | (?P<bare>[^\s"{}/]+)
    """,
    re.VERBOSE,
)


class VdfError(ValueError):
    """VDF 语法错误."""


def _unescape(raw: str) -> str:
    """把 VDF 字符串字面量 (已含外层引号) 反转义为普通字符串."""
    body = raw[1:-1]
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            # 未知转义: 原样保留反斜杠 (Windows 路径常见, 例如 "C:\Program Files")
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse(text: str) -> dict[str, Any]:
    """解析 VDF 文本, 返回嵌套 dict. 顶层必须是一个或多个 `"name" { ... }` 块."""
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup or ""
        if kind in ("comment", "ws"):
            continue
        tokens.append((kind, m.group()))

    root: dict[str, Any] = {}
    # 栈中每个元素代表一个正在构建的块 (dict)
    stack: list[dict[str, Any]] = [root]
    pending_key: str | None = None
    idx = 0
    total = len(tokens)

    while idx < total:
        kind, raw = tokens[idx]
        idx += 1

        if kind == "brace":
            if raw == "{":
                if pending_key is None:
                    raise VdfError("遇到 '{' 但没有前置键名")
                child: dict[str, Any] = {}
                stack[-1][pending_key] = child
                stack.append(child)
                pending_key = None
            else:  # }
                if len(stack) == 1:
                    raise VdfError("多余的 '}'")
                stack.pop()
            continue

        if kind == "string":
            value = _unescape(raw)
            if pending_key is None:
                pending_key = value
            else:
                # 暂时把键存为哨兵, 若后面跟 '{' 会被覆盖为 dict
                stack[-1][pending_key] = value
                pending_key = None
            continue

        if kind == "bare":
            # 未加引号的裸值 (VDF 兼容写法), 当作值处理
            if pending_key is not None:
                stack[-1][pending_key] = raw
                pending_key = None
            continue

    if len(stack) != 1:
        raise VdfError("块结构未闭合")
    return root


def parse_file(path: str | Path) -> dict[str, Any]:
    """读取并解析 VDF 文件. 编码异常时用替换字符兜底, 保证扫描不中断."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    return parse(text)


def walk(node: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    """把嵌套 VDF 拍平成 (键路径, 值) 列表, 只产出叶子字符串."""
    out: list[tuple[tuple[str, ...], str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(walk(v, prefix + (str(k),)))
    else:
        out.append((prefix, str(node)))
    return out


def first_key(node: dict[str, Any], *names: str, default: Any = None) -> Any:
    """按大小写不敏感顺序取第一个存在的键 (Steam 同名字段大小写不统一)."""
    lowered = {str(k).lower(): v for k, v in node.items()}
    for name in names:
        hit = lowered.get(name.lower())
        if hit is not None:
            return hit
    return default
