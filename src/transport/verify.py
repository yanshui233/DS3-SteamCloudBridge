"""上传校验与重试 (M3).

## 为什么需要它

实测 (`cases/local-4b-2026-10-12/ANALYSIS.md`) 抓到过一次**静默失败**:

    [01:25:54] Need to upload file .../scb-bait-...sl2
    [01:25:54] Upload batch initiated, ChangeNumber: 2
    [01:26:14] Upload Timeout for file .../scb-bait-...sl2     <- 注意: 没有 HTTP upload ... beginning
    [01:26:15] Upload complete, result Timeout

关键特征: **压根没出现 `HTTP upload ... beginning`** —— 文件进了队列, 但传输根本没启动.
下一轮重试 (01:31) 立刻成功. 所以 M3 必须"上传后校验 + 重试", 不能写完就当成功.

## 判据 (全部来自实测)

| 日志行 | 含义 |
|---|---|
| `Upload OK for file <p>` | ✅ 该文件真的传上去了 |
| `Upload Timeout for file <p>` | ⏱ 静默失败, 需重试 |
| `HTTP upload for file '<p>' beginning` | 传输真的启动了 (缺它 = 没启动) |
| `Need to forget file <p>` / `Forget OK for file <p>` | 云端删除 (相反语义, 别当上传) |
| `... no longer matches any patterns ... Removing from cloud` | 云端因规则不匹配而移除 |

**最终权威仍是 `remotecache.vdf` 的 `remotetime > 0` + `sha` 匹配** —— 日志只是更早可见的信号.

## 会话作用域

`Upload OK for file` 会**累积历史**. 只看全文件会把上一会话的成功记录当成本次成功.
所以默认把判据限制在**最后一次 `Client version:` 之后** (与 `cloudchannel` 同一套会话边界).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .cloudchannel import SESSION_MARKER, _appid_of

__all__ = [
    "UploadCheck",
    "SyncOutcome",
    "check_upload",
    "check_upload_from_file",
    "norm_rel",
    "sync_and_verify",
]

VERDICT_OK = "ok"
VERDICT_TIMEOUT = "timeout"
VERDICT_QUEUED = "queued"
VERDICT_FORGOTTEN = "forgotten"
VERDICT_IN_SYNC = "in-sync"
VERDICT_NONE = "none"
VERDICT_UNKNOWN = "unknown"


def norm_rel(path: str) -> str:
    """把路径规范化成云日志里的相对形式 (`A/B/c.sl2`)."""
    return path.replace("\\", "/").strip("/")


@dataclass
class UploadCheck:
    """一次"这次上传成功了吗"的判定."""

    appid: str
    rel_path: str
    verdict: str = VERDICT_UNKNOWN

    uploaded_ok: bool = False
    timeouts: int = 0
    http_started: bool = False
    batches: int = 0
    forgets: int = 0
    removed_from_cloud: int = 0
    skipped_unmodified: int = 0
    """`Skipping un-modified file <p>` —— 本地没变, 无需上传 (等价于已同步)."""

    remote_sha: str = ""
    remote_remotetime: int = 0
    remote_matches: bool = False
    """`remotetime > 0` 且 `sha` 与本地一致 —— 云端确实有这一份."""

    session_scoped: bool = False
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "rel_path": self.rel_path,
            "verdict": self.verdict,
            "uploaded_ok": self.uploaded_ok,
            "timeouts": self.timeouts,
            "http_started": self.http_started,
            "batches": self.batches,
            "forgets": self.forgets,
            "removed_from_cloud": self.removed_from_cloud,
            "skipped_unmodified": self.skipped_unmodified,
            "remote_sha": self.remote_sha,
            "remote_remotetime": self.remote_remotetime,
            "remote_matches": self.remote_matches,
            "session_scoped": self.session_scoped,
            "evidence": list(self.evidence),
        }


def check_upload(
    appid: str,
    rel_path: str,
    lines: Sequence[str],
    *,
    remote_sha: str = "",
    remote_remotetime: int = 0,
    session_scoped: bool = True,
) -> UploadCheck:
    """判定 `rel_path` 这一次有没有真的上到云端."""
    appid = str(appid)
    rel = norm_rel(rel_path)
    chk = UploadCheck(appid=appid, rel_path=rel, remote_sha=remote_sha or "",
                      remote_remotetime=int(remote_remotetime or 0))

    lines = list(lines)
    if session_scoped:
        last = -1
        for i, ln in enumerate(lines):
            if SESSION_MARKER in ln:
                last = i
        if last >= 0:
            lines = lines[last + 1 :]
            chk.session_scoped = True

    for ln in lines:
        if _appid_of(ln) != appid and f"file {rel}" not in ln:
            continue
        if rel not in ln.replace("\\", "/"):
            continue
        low = ln.lower()
        if "upload ok for file" in low:
            chk.uploaded_ok = True
            chk.evidence.append(ln.strip())
        if "skipping un-modified file" in low:
            chk.skipped_unmodified += 1
            chk.evidence.append(ln.strip())
        if "upload timeout for file" in low:
            chk.timeouts += 1
            chk.evidence.append(ln.strip())
        if "http upload" in low and "beginning" in low:
            chk.http_started = True
        if "upload batch initiated" in low:
            chk.batches += 1
        if "forget" in low and "for file" in low:
            chk.forgets += 1
            chk.evidence.append(ln.strip())
        if "removing from cloud" in low or "no longer matches any patterns" in low:
            chk.removed_from_cloud += 1
            chk.evidence.append(ln.strip())

    # 云端记录是最终权威
    if chk.remote_remotetime > 0:
        if not remote_sha or not chk.remote_sha:
            chk.remote_matches = True  # 有副本但无从比 sha
        else:
            chk.remote_matches = True  # sha 由调用方比对; 此处只记"云端有副本"

    # ---- 下结论 ----
    if chk.uploaded_ok:
        chk.verdict = VERDICT_OK  # 日志实锤已上传; remotecache 可能还没落盘
    elif chk.skipped_unmodified and chk.remote_remotetime > 0:
        # 本地没变 + 云端确实有副本 -> 已同步, 无需再传 (幂等调用应算成功)
        chk.verdict = VERDICT_IN_SYNC
    elif chk.forgets and not chk.uploaded_ok:
        chk.verdict = VERDICT_FORGOTTEN
    elif chk.timeouts:
        chk.verdict = VERDICT_TIMEOUT
    elif chk.batches:
        chk.verdict = VERDICT_QUEUED
    elif chk.evidence:
        chk.verdict = VERDICT_UNKNOWN
    else:
        chk.verdict = VERDICT_NONE
    return chk


def check_upload_from_file(
    appid: str,
    rel_path: str,
    log_path: str | Path,
    *,
    remote_sha: str = "",
    remote_remotetime: int = 0,
    session_scoped: bool = True,
) -> UploadCheck:
    p = Path(log_path)
    if not p.is_file():
        chk = UploadCheck(appid=str(appid), rel_path=norm_rel(rel_path))
        chk.verdict = VERDICT_UNKNOWN
        chk.evidence.append(f"日志不存在: {p}")
        return chk
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        chk = UploadCheck(appid=str(appid), rel_path=norm_rel(rel_path))
        chk.evidence.append(f"日志不可读: {exc}")
        return chk
    return check_upload(
        appid,
        rel_path,
        text.splitlines(),
        remote_sha=remote_sha,
        remote_remotetime=remote_remotetime,
        session_scoped=session_scoped,
    )


# ---------------------------------------------------------------- 触发 + 重试


@dataclass
class SyncOutcome:
    appid: str
    rel_path: str
    attempts: int = 0
    succeeded: bool = False
    final: UploadCheck | None = None
    actions: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "rel_path": self.rel_path,
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "final": self.final.to_dict() if self.final else None,
            "actions": list(self.actions),
            "error": self.error,
        }


def sync_and_verify(
    appid: str,
    rel_path: str,
    *,
    send: Callable[[str], bool],
    check: Callable[[], UploadCheck],
    max_attempts: int = 3,
    settle_seconds: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    command: str | None = None,
) -> SyncOutcome:
    """触发上行同步, 校验结果, 失败则重试.

    * `send`  —— 把命令送进 Steam 控制台 (返回是否送达).
    * `check` —— 重新判定一次上传结果 (调用方闭包, 每次重读日志/remotecache).
    * 参数全部可注入, 便于在沙箱里测试整条重试逻辑而不碰真机 Steam.
    """
    from .cloudchannel import command_sync_up

    out = SyncOutcome(appid=str(appid), rel_path=norm_rel(rel_path))
    cmd = command or command_sync_up(appid)

    for attempt in range(1, max_attempts + 1):
        out.attempts = attempt
        ok_send = send(cmd)
        out.actions.append(
            f"[{attempt}/{max_attempts}] 发送 {cmd!r} -> {'已送达' if ok_send else '未确认送达'}"
        )
        if settle_seconds > 0:
            sleep(settle_seconds)

        chk = check()
        out.final = chk
        out.actions.append(
            f"[{attempt}/{max_attempts}] 判定={chk.verdict} "
            f"(uploaded_ok={chk.uploaded_ok} timeouts={chk.timeouts} http={chk.http_started})"
        )
        if chk.verdict in (VERDICT_OK, VERDICT_IN_SYNC) and (
            chk.uploaded_ok or chk.remote_remotetime > 0
        ):
            out.succeeded = True
            return out

    out.error = (
        f"{max_attempts} 次尝试后仍未确认上传成功 (最终判定={out.final.verdict if out.final else '?'})"
    )
    return out
