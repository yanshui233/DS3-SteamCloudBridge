"""跨机搬运往返验证 (第 ④ 项: 真·第二台机器的完整搬运往返).

**唯一缺口是"跨机下行"** —— 至今所有真机验证只覆盖了上传半环.

关键事实 (证据: `cases/local-4b-2026-10-12/ANALYSIS.md` §四, 以及本机
`logs/cloud_log.txt` 的真实行): **下行不必启动游戏**. Steam 控制台

    cloud_sync_down <appid>     等价于"游戏启动时的 on-launch sync"(下行)

于是朋友机侧可以**全自动**完成 —— 他不用进游戏、也就不会碰到存档内容本身.
比"请他启动 DS3 到主菜单再退出"安全得多 (后者会让游戏去读/可能改写存档).

两阶段:

    prepare (本机)   把一份**内容与本机真实档不同**的 .sl2 写进存档 + 推上云,
                     产出 `expected.json` (含云端 CN / 期望 sha1).
    verify  (朋友机) 点亮通道 -> `cloud_sync_down` -> 比对本地字节 == expected.

判据全部取自**真实日志行**(不是推测):

    [AppID 374320] Need to download file DarkSoulsIII/01100001499602d2/DS30000.sl2
    [AppID 374320] HTTP download for file '<rel>' from host '...' - Success.
    [AppID 374320] Download OK for file <rel>              <- 唯一决定性判据
    [AppID 374320] Download complete, result OK
    [AppID 374320] Currently already synced to global change number 'N', should be nothing to download

⚠ `Download complete, result OK` **不能**单独当判据 —— 和上传侧的
`Upload complete, result OK` 一样, 它在"无事可做"的批次里也会打印.
**判据是 `Download OK for file <rel>`**, 与上传侧的 `Upload OK for file <p>` 对称.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "SCHEMA",
    "Expected",
    "DownEvidence",
    "parse_down_evidence",
    "decide",
    "norm_rel",
    "is_safe_rel",
    "locate_target",
    "sha1_of_file",
    "read_tail_lines",
]

SCHEMA = "scb-xfer/1"

# ---------------------------------------------------------------- 路径小工具


def norm_rel(path: str | Path) -> str:
    """统一成日志里的写法: `/` 分隔、无首尾斜杠、小写盘符无关."""
    s = str(path).replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def is_safe_rel(rel: str) -> bool:
    """拒绝绝对路径 / 盘符 / `..` / 空段 —— `expected.json` 是外部输入, 必须防穿越.

    ⚠ 必须在**归一化之前**判绝对路径: `norm_rel` 会去掉前导 `/`,
    若先归一化, `/etc/passwd` 会被洗成 `etc/passwd` 而蒙混过关 (单测抓过这个洞).
    """
    raw = str(rel).strip()
    if not raw:
        return False
    if raw[0] in ("/", "\\"):
        return False
    if re.match(r"^[A-Za-z]:", raw):
        return False
    s = norm_rel(raw)
    if not s:
        return False
    low = s.lower()
    if ":" in low:
        return False
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return False
    return all(p not in (".", "..") for p in parts)


def locate_target(appdata_root: str | Path, rel: str) -> Path:
    """把云侧相对路径落到**本机**的 `%APPDATA%` 下.

    DS3 的 Auto-Cloud 规则是 `root="WinAppDataRoaming" path="DarkSoulsIII" ...`,
    所以 `DarkSoulsIII/<sidhex>/DS30000.sl2` -> `%APPDATA%\\DarkSoulsIII\\<sidhex>\\DS30000.sl2`.
    **目录名不是拼出来的** —— 它来自本机 prepare 阶段真实上传的 `rel_path`.
    """
    if not is_safe_rel(rel):
        raise ValueError(f"非法/可疑的云相对路径: {rel!r}")
    parts = [p for p in norm_rel(rel).split("/") if p]
    return Path(appdata_root).joinpath(*parts)


def sha1_of_file(path: str | Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_tail_lines(path: str | Path, encoding: str = "utf-8") -> list[str]:
    """整文件读成行 (缺失/不可读 -> 空表). 日志只有几 MB, 不必做增量."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        return p.read_text(encoding=encoding, errors="replace").splitlines()
    except OSError:
        return []


# ---------------------------------------------------------------- 下行证据


_RE_NEED = re.compile(r"\[AppID\s+(\d+)\]\s+Need to download file\s+(.+?)\s*$")
_RE_HTTP = re.compile(r"\[AppID\s+(\d+)\]\s+HTTP download for file\s+'(.+?)'")
_RE_OK = re.compile(r"\[AppID\s+(\d+)\]\s+Download OK for file\s+(.+?)\s*$")
_RE_DONE = re.compile(r"\[AppID\s+(\d+)\]\s+Download complete\b")
_RE_SYNCED = re.compile(
    r"\[AppID\s+(\d+)\]\s+Currently already synced to global change number"
    r"\s+'(\d+)',\s+should be nothing to download"
)


@dataclass
class DownEvidence:
    """一次 `cloud_sync_down` 之后, 新增日志里关于"下行"的全部证据."""

    appid: str = ""
    need: list[str] = field(default_factory=list)
    http_started: list[str] = field(default_factory=list)
    http_ok: list[str] = field(default_factory=list)
    download_ok: list[str] = field(default_factory=list)
    download_complete: int = 0
    already_synced_cn: int | None = None
    already_synced_count: int = 0
    lines_scanned: int = 0

    def downloaded(self, rel: str) -> bool:
        want = norm_rel(rel).lower()
        return any(norm_rel(x).lower() == want for x in self.download_ok)

    @property
    def any_activity(self) -> bool:
        return bool(
            self.need
            or self.http_started
            or self.download_ok
            or self.download_complete
            or self.already_synced_count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "need": list(self.need),
            "http_started": list(self.http_started),
            "http_ok": list(self.http_ok),
            "download_ok": list(self.download_ok),
            "download_complete": self.download_complete,
            "already_synced_cn": self.already_synced_cn,
            "already_synced_count": self.already_synced_count,
            "lines_scanned": self.lines_scanned,
        }


def parse_down_evidence(lines: Iterable[str], appid: str | int = "") -> DownEvidence:
    """从 (新增的) 日志行里抽取下行证据. `appid` 为空则不过滤 appid."""
    want = str(appid) if str(appid) else ""
    ev = DownEvidence(appid=want)
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        ev.lines_scanned += 1
        if "download" not in line.lower() and "Download" not in line:
            continue
        m = _RE_NEED.search(line)
        if m and (not want or m.group(1) == want):
            ev.need.append(norm_rel(m.group(2)))
            continue
        m = _RE_SYNCED.search(line)
        if m and (not want or m.group(1) == want):
            ev.already_synced_cn = int(m.group(2))
            ev.already_synced_count += 1
            continue
        m = _RE_OK.search(line)
        if m and (not want or m.group(1) == want):
            ev.download_ok.append(norm_rel(m.group(2)))
            continue
        m = _RE_HTTP.search(line)
        if m and (not want or m.group(1) == want):
            rel = norm_rel(m.group(2))
            if " - Success." in line:
                ev.http_ok.append(rel)
            else:
                ev.http_started.append(rel)
            continue
        m = _RE_DONE.search(line)
        if m and (not want or m.group(1) == want):
            ev.download_complete += 1
            continue
    return ev


def decide(
    expected_rel: str, expected_sha1: str, local_sha1: str | None, ev: DownEvidence
) -> tuple[str, str]:
    """给出 (判定码, 人话). 判定码: `pass` / `byte-mismatch` / `nothing-to-do` / `pending`."""
    if ev.downloaded(expected_rel):
        if local_sha1 is None:
            return "byte-mismatch", "云端已下发该文件, 但本机磁盘上读不到它"
        if local_sha1.lower() == str(expected_sha1).lower():
            return "pass", "云端下发的字节与本机期望值**逐字节一致**"
        return (
            "byte-mismatch",
            f"云端已下发, 但本机内容不符: 实际 {local_sha1[:16]}… ≠ 期望 {str(expected_sha1)[:16]}…",
        )
    if ev.already_synced_cn is not None:
        return (
            "nothing-to-do",
            f"Steam 认为'已同步到云变更号 {ev.already_synced_cn}', 没有东西可下行 "
            "—— 本机判定与云端不一致, 需要人工复核",
        )
    return "pending", "尚未观察到下行证据"


# ---------------------------------------------------------------- expected.json


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _sha1_ok(value: str) -> bool:
    s = str(value).strip().lower()
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


@dataclass
class Expected:
    """本机 prepare 阶段写出的"跨机测试契约". 朋友机只读它, 不接受命令行猜值."""

    appid: str = ""
    rel_path: str = ""
    sha1: str = ""
    size: int = 0
    accountid: int = 0
    account_hex_dir: str = ""
    account_name: str = ""
    persona_name: str = ""
    cloud_change_number: int = 0
    remote_remotetime: int = 0
    remote_sha: str = ""
    created_at: str = ""
    tool_version: str = ""
    label: str = ""
    note: str = ""

    def validate(self) -> None:
        if not str(self.appid).isdigit():
            raise ValueError(f"appid 不是数字: {self.appid!r}")
        if not is_safe_rel(self.rel_path):
            raise ValueError(f"rel_path 非法: {self.rel_path!r}")
        if not _sha1_ok(self.sha1):
            raise ValueError(f"sha1 不是 40 位十六进制: {self.sha1!r}")
        if _as_int(self.accountid) <= 0:
            raise ValueError(f"accountid 非正: {self.accountid!r}")
        if self.size and _as_int(self.size) <= 0:
            raise ValueError(f"size 非法: {self.size!r}")

    @property
    def rel_norm(self) -> str:
        return norm_rel(self.rel_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "appid": str(self.appid),
            "rel_path": self.rel_norm,
            "sha1": str(self.sha1).lower(),
            "size": _as_int(self.size),
            "accountid": _as_int(self.accountid),
            "account_hex_dir": str(self.account_hex_dir),
            "account_name": str(self.account_name),
            "persona_name": str(self.persona_name),
            "cloud_change_number": _as_int(self.cloud_change_number),
            "remote_remotetime": _as_int(self.remote_remotetime),
            "remote_sha": str(self.remote_sha).lower(),
            "created_at": str(self.created_at) or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tool_version": str(self.tool_version),
            "label": str(self.label),
            "note": str(self.note),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Expected":
        if not isinstance(data, dict):
            raise ValueError("expected.json 顶层不是对象")
        schema = str(data.get("schema", ""))
        if schema != SCHEMA:
            raise ValueError(
                f"expected.json 的 schema 是 {schema!r}, 本工具需要 {SCHEMA!r} "
                "(版本不匹配 -> 请用同一版发行包里的这个文件)"
            )
        exp = cls(
            appid=str(data.get("appid", "")),
            rel_path=str(data.get("rel_path", "")),
            sha1=str(data.get("sha1", "")),
            size=_as_int(data.get("size")),
            accountid=_as_int(data.get("accountid")),
            account_hex_dir=str(data.get("account_hex_dir", "")),
            account_name=str(data.get("account_name", "")),
            persona_name=str(data.get("persona_name", "")),
            cloud_change_number=_as_int(data.get("cloud_change_number")),
            remote_remotetime=_as_int(data.get("remote_remotetime")),
            remote_sha=str(data.get("remote_sha", "")),
            created_at=str(data.get("created_at", "")),
            tool_version=str(data.get("tool_version", "")),
            label=str(data.get("label", "")),
            note=str(data.get("note", "")),
        )
        exp.validate()
        return exp

    @classmethod
    def load(cls, path: str | Path) -> "Expected":
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"读不到 expected.json: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"expected.json 解析失败: {exc}") from exc
        return cls.from_dict(data)

    def save(self, path: str | Path) -> Path:
        self.validate()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return p
