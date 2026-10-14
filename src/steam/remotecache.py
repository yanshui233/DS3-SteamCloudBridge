"""`remotecache.vdf` 解析 与 root 枚举的**运行时标定**.

## 背景

`userdata\\<accountid>\\<appid>\\remotecache.vdf` 是 Steam 为 ISteamRemoteStorage
(API 型) 云存维护的缓存清单, 结构:

    374320
    {
        "DS30000.sl2"
        {
            "root"      "4"          <- 文件所在"根目录"枚举
            "size"      "9307472"
            "localtime" "1758380947"
            "time"      "1758380947"
            "remotetime" "0"
            "sha"       "..."
            "syncstate" "1"
            ...
        }
    }

`root` 是**枚举值**而不是路径. 枚举到底是什么, 官方未文档化, 各版本也可能变.

## 本模块的做法: 不信文档, 靠实机标定

对每个出现过的 root 值, 收集它名下所有相对路径, 逐个拼到候选基目录下探测是否存在,
用**命中率**选出真正的基目录. 这样在别人电脑上也能自动适配, 不需要改代码.

标定结果同时记录 `hits / total` 作为置信度: 样本太少时只给"推断"不给"确定".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import vdf

__all__ = [
    "RemoteFileEntry",
    "RemoteCache",
    "parse_remotecache",
    "candidate_bases",
    "calibrate_roots",
    "RootCalibration",
    "ROOT_HINTS",
]

# 已知/推测的枚举语义, 仅用于展示与兜底; **结论以标定为准**.
ROOT_HINTS: dict[str, str] = {
    "0": "Steam Cloud (userdata\\<id>\\<appid>\\remote)",
    "1": "游戏安装目录 (Game Install Dir)",
    "2": "我的文档 (Documents)",
    "3": "%LOCALAPPDATA% (AppData\\Local)",
    "4": "%APPDATA% (AppData\\Roaming)",
    "5": "%USERPROFILE%\\AppData\\LocalLow",
    "9": "%USERPROFILE%\\Saved Games",
    "10": "%ProgramData%",
    "11": "Documents\\Steam Cloud (SteamCloudDocuments)",
    "12": "%USERPROFILE%\\AppData\\LocalLow",
}
# 语义表来源: 本项目实机标定 (0/1/3/4/12) + 社区 SteamCloudFileManager 的公开映射表
# (2/9/11). 缺 4/5/10 的那份表由本项目补全; 结论一律以 `calibrate_roots` 的实机
# 命中率为准, 本表仅用于展示与兜底.


@dataclass
class RemoteFileEntry:
    """remotecache.vdf 里的一个文件条目."""

    name: str  # 相对路径 (含子目录时用 `\\` 或 `/` 分隔)
    root: str = "0"
    size: int = 0
    localtime: int = 0
    remotetime: int = 0
    sha: str = ""
    syncstate: int = 0
    persiststate: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": self.root,
            "size": self.size,
            "localtime": self.localtime,
            "remotetime": self.remotetime,
            "sha": self.sha,
            "syncstate": self.syncstate,
        }


@dataclass
class RemoteCache:
    """一个 appid 的 remotecache.vdf 内容."""

    appid: str
    path: Path
    entries: list[RemoteFileEntry] = field(default_factory=list)
    change_number: int = 0
    """VDF 根节点的 `ChangeNumber`: 该 appid 已提交的云端变更号 (0 = 尚未提交过)."""

    @property
    def roots(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            counts[e.root] = counts.get(e.root, 0) + 1
        return counts

    # ---- 上传往返的**唯一可靠判据** (见 docs/how-steam-cloud-works.md) ----
    #
    # 实测结论: `Successfully synced to ChangeNumber` 是**通用同步完成语句**, 不是
    # "这批上传成功" —— 上传路径里它出现在 `Upload batch initiated` 之前 (实测
    # 3590/105600/219740/431960), Auto-Cloud 型 (DS3) 的上传路径则从不打印它.
    # 因此判断"文件到底有没有真的落到 Valve 服务器"不能靠 cloud_log, 只能靠
    # 本文件的 `remotetime`:
    #
    #   remotetime > 0  -> 该文件在云端有一份副本 (上传曾成功)
    #   remotetime == 0 -> 只有本地登记 (Persisting 过), 从未上云
    #
    # 实测对照 (同一台机器): 241100/730/105600 的 remotetime 都 > 0;
    # 374320 (DS3) 曾恒为 0 —— 与 cloud_log 里"只有 DS3 出现过
    # `AutoCloud is disabled`"互相印证. **2026-10-11 21:33 DS3 已首次上传成功**
    # (remotetime 1791725553 / ChangeNumber 0→1), 该对照自此作废但仍保留其诊断价值.

    @property
    def ever_advanced(self) -> bool:
        """云端变更号已推进 (`ChangeNumber > 0`).

        cloud_log 的 `Need to sync from local change number 'X' to global change
        number 'Y'` 是**上传前**打印的, 所以只看它会把"刚上传成功"的游戏误报成
        `曾推进过=False`. `ChangeNumber` 是 Steam 落盘的结果, 才是权威.
        """
        return self.change_number > 0 or self.upload_verified

    @property
    def upload_verified(self) -> bool:
        """至少有一个文件真的上过云 (`remotetime > 0`)."""
        return any(e.remotetime > 0 for e in self.entries)

    @property
    def max_remotetime(self) -> int:
        """所有条目里最新的云端时间戳 (0 = 从未上云)."""
        return max((e.remotetime for e in self.entries), default=0)

    @property
    def pending_upload(self) -> list[str]:
        """已登记进本地云缓存、但从未上云的文件 (`remotetime == 0`).

        判据: 有本地时间戳或 sha, 说明本地确实产生过该文件; 但云端无副本.
        """
        return [
            e.name
            for e in self.entries
            if e.remotetime <= 0 and (e.localtime > 0 or e.sha)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "path": str(self.path),
            "file_count": len(self.entries),
            "roots": self.roots,
            "change_number": self.change_number,
            "ever_advanced": self.ever_advanced,
            "upload_verified": self.upload_verified,
            "max_remotetime": self.max_remotetime,
            "pending_upload": self.pending_upload,
            "entries": [e.to_dict() for e in self.entries],
        }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def parse_remotecache(path: str | Path, appid: str = "") -> RemoteCache:
    """解析一个 remotecache.vdf. 结构异常时返回空 entries 而不是抛错."""
    p = Path(path)
    if not appid:
        appid = p.parent.name
    cache = RemoteCache(appid=appid, path=p)
    try:
        data = vdf.parse_file(p)
    except Exception:
        return cache

    # 顶层形如 {"374320": {"file": {...}, ...}}; 若顶层直接是文件表也兼容
    block: Any = data
    if isinstance(data, dict) and len(data) == 1:
        only = next(iter(data.values()))
        if isinstance(only, dict) and not _looks_like_entry(only):
            block = only
    if not isinstance(block, dict):
        return cache

    cache.change_number = _as_int(block.get("ChangeNumber"))

    for name, entry in block.items():
        if not isinstance(entry, dict):
            continue
        # 跳过嵌套的目录节点 (某些版本会出现) —— 只收有 root/sha/size 的叶子
        if not _looks_like_entry(entry):
            continue
        cache.entries.append(
            RemoteFileEntry(
                name=str(name),
                root=str(entry.get("root", "0")),
                size=_as_int(entry.get("size")),
                localtime=_as_int(entry.get("localtime")),
                remotetime=_as_int(entry.get("remotetime")),
                sha=str(entry.get("sha", "")),
                syncstate=_as_int(entry.get("syncstate")),
                persiststate=_as_int(entry.get("persiststate")),
                raw={k: str(v) for k, v in entry.items() if not isinstance(v, dict)},
            )
        )
    return cache


def _looks_like_entry(node: dict[str, Any]) -> bool:
    return any(k in node for k in ("root", "sha", "size", "syncstate", "localtime"))


def candidate_bases() -> dict[str, Path]:
    """Windows 下"云存可能落在哪"的**全局**候选基目录 (用于标定).

    注意: 只有全局候选还不够. `root=0` (云端 remote 目录) 和 `root=1`
    (游戏安装目录) 是**按 appid 变化**的, 必须由调用方通过 per-appid
    基目录补充 (见 `calibrate_roots` 的 records 参数).
    """
    home = Path(os.path.expanduser("~"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    localappdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    bases: dict[str, Path] = {
        "%APPDATA%": appdata,
        "%LOCALAPPDATA%": localappdata,
        "%USERPROFILE%\\AppData\\LocalLow": home / "AppData" / "LocalLow",
        "Documents": home / "Documents",
        "Documents(OneDrive)": home / "OneDrive" / "Documents",
        "Saved Games": home / "Saved Games",
        "ProgramData": Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")),
        "%USERPROFILE%": home,
    }
    # 去掉不存在的基目录, 避免污染命中率统计
    return {k: v for k, v in bases.items() if v.is_dir()}


@dataclass
class RootCalibration:
    """某个 root 枚举值的标定结果."""

    root: str
    base_label: str
    base_path: str
    hits: int
    total: int
    samples: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.total == 0:
            return "unknown"
        ratio = self.hits / self.total
        if self.hits >= 2 and ratio >= 0.6:
            return "high"       # 实测命中, 样本足够
        if self.hits >= 1 and ratio >= 0.3:
            return "medium"
        return "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "hint": ROOT_HINTS.get(self.root, ""),
            "base_label": self.base_label,
            "base_path": self.base_path,
            "hits": self.hits,
            "total": self.total,
            "confidence": self.confidence,
            "samples": self.samples[:5],
        }


def calibrate_roots(
    records: Iterable[tuple[str, str, dict[str, Path]]],
    bases: dict[str, Path] | None = None,
    max_probe_per_root: int = 400,
) -> dict[str, RootCalibration]:
    """用真实文件存在性反推每个 root 枚举值对应的基目录.

    参数
    ----
    records : (root, relpath, extra_bases)
        逐条待标定的文件记录. `extra_bases` 是该 appid **专属**的候选基目录
        (例如 `<SteamRemote>` = `userdata\\<id>\\<appid>\\remote`,
        `<GameInstallDir>` = 游戏安装目录). root=0 与 root=1 只有靠它才能命中.

    返回 root 值 -> 最佳标定结果. 未命中任何基目录的 root 也会返回一条
    hits=0 的记录 (confidence=low), 便于在报告里标注"未能标定".
    """
    if bases is None:
        bases = candidate_bases()

    # 收集每个 root 值下的 (相对路径, 专属基目录) 样本
    collected: dict[str, list[tuple[str, dict[str, Path]]]] = {}
    for root, rel, extra in records:
        bucket = collected.setdefault(str(root), [])
        if len(bucket) < max_probe_per_root:
            bucket.append((rel, extra or {}))

    results: dict[str, RootCalibration] = {}
    for root, samples in collected.items():
        best: RootCalibration | None = None

        # (a) 全局基目录: 适用于全部样本
        for label, base in bases.items():
            hits = 0
            hit_samples: list[str] = []
            for rel, _extra in samples:
                if _hit(base, rel):
                    hits += 1
                    if len(hit_samples) < 5:
                        hit_samples.append(str(base / _norm_rel(rel)))
            cand = RootCalibration(
                root=root, base_label=label, base_path=str(base),
                hits=hits, total=len(samples), samples=hit_samples,
            )
            if best is None or cand.hits > best.hits:
                best = cand

        # (b) per-appid 专属基目录: 逐条用**该条自己的**候选路径判定
        by_label: dict[str, list[tuple[str, Path]]] = {}
        for rel, extra in samples:
            for label, path in (extra or {}).items():
                by_label.setdefault(label, []).append((rel, path))
        for label, pairs in by_label.items():
            hits = 0
            hit_samples = []
            for rel, base in pairs:
                if _hit(base, rel):
                    hits += 1
                    if len(hit_samples) < 5:
                        hit_samples.append(str(base / _norm_rel(rel)))
            cand = RootCalibration(
                root=root, base_label=label,
                base_path="(per-appid, 例: " + str(pairs[0][1]) + ")",
                hits=hits, total=len(pairs), samples=hit_samples,
            )
            if best is None or cand.hits > best.hits:
                best = cand

        assert best is not None
        results[root] = best
    return results


def _norm_rel(rel: str) -> str:
    return rel.replace("\\", os.sep).replace("/", os.sep)


def _hit(base: Path, rel: str) -> bool:
    """判断 base/rel 是否存在, 并拒绝目录穿越与绝对路径."""
    rel_norm = _norm_rel(rel)
    if rel_norm.startswith(os.sep) or ".." in rel_norm.split(os.sep):
        return False
    try:
        return (base / rel_norm).exists()
    except OSError:
        return False
