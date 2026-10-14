"""快照 / 回滚 (M3).

## 为什么回滚**不能靠删文件**

实测 (`cases/local-4b-2026-10-12/ANALYSIS.md`) 把删除语义钉死了:

    本地删掉诱饵 -> 下一轮游戏**启动**时:
        (ValidateCache) File '...scb-bait-...sl2' missing from disk, setting changes in cloud
        Need to download file .../scb-bait-...sl2
        Download OK for file .../scb-bait-...sl2        <- 云端把它灌回来了

**本地删除不会同步到云端, 反而被云端恢复.** 要让云端也删掉, 必须
「删本地 + `cloud_sync_up`, 中间不能夹游戏启动」(01:34 实测有效: `Forget OK for file`).

所以回滚分两种形态:

| 快照时目标 | 回滚动作 | 之后 |
|---|---|---|
| **存在** | 把备份内容写回 (原子替换 + 刷 mtime) | 触发上行, 让云端回到旧版 |
| **不存在** (本次新建的) | 删掉文件 | **必须立即 `cloud_sync_up`**, 否则被灌回来 |

## 三条硬约束 (与 `autocloud` 同源)

1. 写回时**绝不用 `copy2`** —— 保留旧 mtime 会让 Steam 判 `Skipping un-modified file`.
2. **原子替换** —— 不留半个文件.
3. 回滚**默认 dry-run**, 真跑需显式确认.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .autocloud import atomic_write_fresh
from .plan import sha1_of

__all__ = [
    "Snapshot",
    "RestoreResult",
    "take_snapshot",
    "save_snapshot",
    "load_snapshot",
    "restore_snapshot",
]

SNAPSHOT_SCHEMA = 1


@dataclass
class Snapshot:
    """一次"改动前"的落点状态记录 —— 回滚的全部依据."""

    appid: str
    target_path: str
    taken_at: int = 0
    schema: int = SNAPSHOT_SCHEMA

    # 本地侧
    exists: bool = False
    sha1: str = ""
    size: int = 0
    mtime: float = 0.0

    # 云端侧 (来自 remotecache.vdf)
    remote_sha: str = ""
    remote_size: int = 0
    remotetime: int = 0
    syncstate: int = 0
    change_number: int = 0

    # 环境
    channel_lit: bool | None = None

    copy_path: str = ""
    """本地备份副本. 空 = 没做副本 (快照时目标不存在, 或显式 copy=False)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "appid": self.appid,
            "target_path": self.target_path,
            "taken_at": self.taken_at,
            "exists": self.exists,
            "sha1": self.sha1,
            "size": self.size,
            "mtime": self.mtime,
            "remote_sha": self.remote_sha,
            "remote_size": self.remote_size,
            "remotetime": self.remotetime,
            "syncstate": self.syncstate,
            "change_number": self.change_number,
            "channel_lit": self.channel_lit,
            "copy_path": self.copy_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def has_copy(self) -> bool:
        return bool(self.copy_path) and Path(self.copy_path).is_file()


@dataclass
class RestoreResult:
    restored: bool = False
    dry_run: bool = True
    actions: list[str] = field(default_factory=list)
    error: str = ""
    sha1_after: str = ""
    verified: bool = False
    removed: bool = False
    """本次是否走了"删除"路径 (快照时目标不存在). 调用方随后**必须**触发上行同步."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored": self.restored,
            "dry_run": self.dry_run,
            "actions": list(self.actions),
            "error": self.error,
            "sha1_after": self.sha1_after,
            "verified": self.verified,
            "removed": self.removed,
        }


def _snapshot_name(target_path: Path, appid: str, ts: int) -> str:
    return f"{appid}__{target_path.name}__{ts}.snapshot.json"


def take_snapshot(
    target: Any,
    *,
    appid: str = "",
    change_number: int = 0,
    channel_lit: bool | None = None,
    copy_dir: str | Path | None = None,
    copy: bool = True,
) -> Snapshot:
    """给一个 `SaveTarget` 拍快照.

    `target` 只要有 `abs_path` / `remote_sha` / `remote_size` / `remotetime` /
    `syncstate` 属性即可 (即 `src.transport.target.SaveTarget`).
    """
    abs_path = getattr(target, "abs_path", None)
    if abs_path is None:
        raise ValueError("target 没有 abs_path, 无法拍快照")
    p = Path(abs_path)
    snap = Snapshot(
        appid=str(appid or getattr(target, "appid", "")),
        target_path=str(p),
        taken_at=int(time.time()),
        remote_sha=str(getattr(target, "remote_sha", "") or ""),
        remote_size=int(getattr(target, "remote_size", 0) or 0),
        remotetime=int(getattr(target, "remotetime", 0) or 0),
        syncstate=int(getattr(target, "syncstate", 0) or 0),
        change_number=int(change_number or 0),
        channel_lit=channel_lit,
    )

    if p.is_file():
        snap.exists = True
        snap.sha1 = sha1_of(p)
        st = p.stat()
        snap.size = st.st_size
        snap.mtime = st.st_mtime
        if copy and copy_dir is not None:
            dst = Path(copy_dir) / f"{appid or 'save'}__{p.name}__{snap.taken_at}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 副本要忠实还原 -> 这里**可以**用 copy2 (它不是写进被监视目录)
            import shutil

            shutil.copy2(p, dst)
            snap.copy_path = str(dst)
    return snap


def save_snapshot(snap: Snapshot, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(snap.to_dict(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return p


def load_snapshot(path: str | Path) -> Snapshot:
    p = Path(path)
    with open(p, encoding="utf-8") as fh:
        return Snapshot.from_dict(json.load(fh))


def restore_snapshot(
    snap: Snapshot,
    *,
    apply: bool = False,
    journal_path: str | Path | None = None,
) -> RestoreResult:
    """按快照回滚. `apply=False` (默认) 只报告.

    ⚠️ 若走"删除"路径 (`result.removed = True`), 调用方**必须**紧接着触发
    `cloud_sync_up`, 且**中间不能启动游戏** —— 否则云端会把文件灌回来.
    """
    res = RestoreResult(dry_run=not apply)
    target = Path(snap.target_path)

    if not snap.exists:
        # 快照时目标不存在 -> 本次搬运是"新建", 回滚 = 删掉
        res.actions.append(f"快照时目标不存在 -> 回滚方式是删除 {target}")
        if not apply:
            res.actions.append("[dry-run] 未做任何改动")
            return res
        try:
            if target.exists():
                target.unlink()
                res.actions.append(f"已删除 {target}")
            else:
                res.actions.append("目标已不存在, 无需删除")
            res.removed = True
            res.restored = True
            res.actions.append(
                "⚠ 紧接着必须触发 cloud_sync_up, 否则下次游戏启动云端会把文件灌回来"
            )
        except OSError as exc:
            res.error = f"删除失败: {exc}"
        _journal(journal_path, snap, res)
        return res

    # 快照时目标存在 -> 写回备份内容
    if not snap.has_copy:
        res.error = (
            "快照里没有可用副本 (copy_path 为空或文件已丢失), 无法回滚内容. "
            f"原始 sha1={snap.sha1}"
        )
        return res

    if not apply:
        res.actions.append(f"将把 {snap.copy_path} 写回 {target}")
        res.actions.append(f"期望 sha1={snap.sha1} (原子替换 + 刷新 mtime)")
        res.actions.append("[dry-run] 未做任何改动")
        return res

    try:
        atomic_write_fresh(Path(snap.copy_path), target)
        res.restored = True
        res.actions.append(f"已写回 {target}")
    except OSError as exc:
        res.error = f"写回失败: {exc}"
        _journal(journal_path, snap, res)
        return res

    res.sha1_after = sha1_of(target)
    res.verified = bool(snap.sha1) and res.sha1_after == snap.sha1
    if res.verified:
        res.actions.append(f"校验通过: sha1={res.sha1_after}")
    else:
        res.actions.append(
            f"校验不一致! 期望={snap.sha1 or '?'} 实际={res.sha1_after or '?'}"
        )
    res.actions.append("回滚后建议触发 cloud_sync_up, 让云端也回到旧版")
    _journal(journal_path, snap, res)
    return res


def _journal(path: str | Path | None, snap: Snapshot, res: RestoreResult) -> None:
    if path is None:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": int(time.time()),
                    "appid": snap.appid,
                    "action": "rollback",
                    "target": snap.target_path,
                    "snapshot_sha1": snap.sha1,
                    "sha1_after": res.sha1_after,
                    "removed": res.removed,
                    "verified": res.verified,
                    "error": res.error,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
