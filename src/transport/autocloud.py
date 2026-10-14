"""搬运执行器: 备份 -> 原子写入 -> 刷新 mtime -> 回读校验.

## 三条硬约束 (全部来自实测)

1. **绝不用 `shutil.copy2` / `copystat` 写存档**
   —— 它们会把源文件的旧 mtime 也带过去. 实测: 被监视文件的 mtime 没变时 Steam
   打印 `Skipping un-modified file`, 改动**永远不上云**. 所以写完必须显式 `os.utime`.
2. **原子替换** —— 先写同级临时文件再 `os.replace`, 避免写到一半被中断留下半个存档.
3. **执行前重查 Steam 进程** —— 计划与执行之间 Steam 可能被启动, 此时写文件会与
   云端仲裁抢 (实测: 启动期下行会把本地删除的文件灌回来).

默认 `apply=False`, 即 **dry-run** —— 只回报"将发生什么", 不碰磁盘.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guard import steam_running
from .plan import VERDICT_BLOCKED, VERDICT_SKIP, TransferPlan, sha1_of

__all__ = ["ApplyResult", "apply_plan", "atomic_write_fresh", "backup_file"]

_TMP_SUFFIX = ".scb-tmp"


@dataclass
class ApplyResult:
    """一次执行 (或干跑) 的结果."""

    applied: bool = False
    dry_run: bool = True
    actions: list[str] = field(default_factory=list)
    backup_path: str | None = None
    source_sha1: str = ""
    target_sha1_after: str = ""
    verified: bool = False
    error: str = ""
    journal_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "actions": list(self.actions),
            "backup_path": self.backup_path,
            "source_sha1": self.source_sha1,
            "target_sha1_after": self.target_sha1_after,
            "verified": self.verified,
            "error": self.error,
            "journal_path": self.journal_path,
        }


def backup_file(src: Path, dst: Path) -> None:
    """把现有文件备份到 `dst` (保留原 mtime —— 备份就该忠实还原)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def atomic_write_fresh(src: Path, dst: Path) -> None:
    """把 `src` 的内容写到 `dst`: 临时文件 + 原子替换 + 显式刷新 mtime.

    **禁用 `copy2`/`copystat`**: 见模块 docstring 第 1 条.
    """
    tmp = dst.with_name(dst.name + _TMP_SUFFIX)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        os.utime(dst, None)  # 显式刷新 —— 这是"能被云扫到"的前提
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _journal(path: Path, record: dict[str, Any]) -> None:
    """追加一行 JSON 记录 (JSON Lines), 便于事后审计与回滚."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def apply_plan(
    plan: TransferPlan,
    *,
    apply: bool = False,
    journal_path: Path | None = None,
) -> ApplyResult:
    """执行 (或干跑) 一份计划.

    * `apply=False` (默认): 只回报动作序列, 不碰磁盘.
    * `apply=True`: 真正写入. 会**重新探测 Steam 进程**; 若 Steam 已启动则拒绝.
    """
    res = ApplyResult(dry_run=not apply)
    res.source_sha1 = plan.source.sha1
    res.backup_path = plan.backup_path

    if plan.blocked:
        res.error = "计划存在阻断项, 拒绝执行: " + "; ".join(
            f"{g.code}: {g.message}" for g in plan.blockers
        )
        return res

    if plan.verdict == VERDICT_SKIP:
        res.actions.append("目标已是最新, 无需搬运")
        res.verified = True
        res.target_sha1_after = plan.source.sha1
        return res

    target = plan.target
    if target is None or target.abs_path is None:
        res.error = "计划没有可用的目标落点"
        return res
    tgt_path = target.abs_path
    src_path = plan.source.path

    # ---- dry-run: 只报告 ----
    if not apply:
        res.actions.append(f"[dry-run] 源: {src_path} (sha1={plan.source.sha1 or '?'})")
        res.actions.append(f"[dry-run] 目标: {tgt_path}")
        for step in plan.steps:
            res.actions.append(f"[dry-run] {step.kind}: {step.detail}")
        res.actions.append("[dry-run] 未做任何改动")
        return res

    # ---- 真跑: 先重查 Steam (防计划与执行之间被启动) ----
    running = steam_running()
    if running is True:
        res.error = "执行前复查发现 Steam 正在运行, 已中止 (避免与云端仲裁抢文件)"
        return res
    if running is None:
        res.actions.append("警告: 无法确认 Steam 是否在运行, 继续执行但请自行核对")

    # ---- 备份 ----
    if target.exists:
        if plan.backup_path:
            try:
                backup_file(tgt_path, Path(plan.backup_path))
                res.actions.append(f"已备份 -> {plan.backup_path}")
            except OSError as exc:
                res.error = f"备份失败, 已中止 (不冒险覆盖): {exc}"
                return res
        else:
            res.actions.append("警告: 未指定备份目录, 直接覆盖 (建议始终提供 --backup-dir)")

    # ---- 原子写入 ----
    try:
        atomic_write_fresh(src_path, tgt_path)
        res.applied = True
        res.actions.append(f"已写入 {tgt_path} (原子替换 + 刷新 mtime)")
    except OSError as exc:
        res.error = f"写入失败: {exc}"
        return res

    # ---- 回读校验 ----
    res.target_sha1_after = sha1_of(tgt_path)
    res.verified = bool(res.source_sha1) and res.target_sha1_after == res.source_sha1
    if res.verified:
        res.actions.append(f"校验通过: sha1={res.target_sha1_after}")
    else:
        res.actions.append(
            f"校验不一致! 源={res.source_sha1 or '?'} 目标={res.target_sha1_after or '?'}"
        )

    # ---- journal ----
    if journal_path is not None:
        _journal(
            Path(journal_path),
            {
                "ts": int(time.time()),
                "appid": plan.appid,
                "action": "transfer",
                "source": str(src_path),
                "target": str(tgt_path),
                "source_sha1": res.source_sha1,
                "target_sha1_after": res.target_sha1_after,
                "backup_path": res.backup_path,
                "verified": res.verified,
            },
        )
        res.journal_path = str(journal_path)

    return res
