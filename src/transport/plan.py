"""搬运计划: 把"将要发生什么"算清楚, 且默认**不落盘**.

M2 的交付物就是这个 —— 打印将写哪个文件、mtime 是否更新、伴生文件是什么状态,
全部只读地算出来, 由人过目后再决定是否执行.

## 为什么计划里必须带 `touches_disk`

`docs/plan-v3.md` 的坑 #3: Steam **只同步主档 `.sl2`**, 伴生文件 (`.co2`/`.ds3os`)
不走云. 跨机器时 B 机残留的旧伴生文件是否会让 DS3 判定存档异常, 是 4b 未闭环的
真风险点. 所以计划里显式列出伴生文件的**新旧关系**, 但不自动动它们 —— 动它属于
M3 的快照/回滚职责.
"""

from __future__ import annotations

import fnmatch
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .guard import Guard
from .target import SaveTarget, TargetResolution

__all__ = [
    "SourceInfo",
    "CompanionFile",
    "PlanStep",
    "TransferPlan",
    "sha1_of",
    "build_plan",
]

_CHUNK = 1 << 20
# 伴生文件比源文件旧这么多天时, 提示"可能不匹配"
_STALE_DAYS = 3.0

VERDICT_COPY = "copy"
VERDICT_SKIP = "skip-identical"
VERDICT_BLOCKED = "blocked"


def sha1_of(path: str | Path) -> str:
    """流式算 sha1 (大存档也不吃内存). 读不动返回空串."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(_CHUNK)
                if not block:
                    break
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


@dataclass
class SourceInfo:
    """源文件实况."""

    path: Path
    exists: bool = False
    size: int = 0
    mtime: float = 0.0
    sha1: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "size": self.size,
            "mtime": self.mtime,
            "sha1": self.sha1,
        }


@dataclass
class CompanionFile:
    """目标目录里的伴生文件 (不走云的那些)."""

    name: str
    path: str
    exists: bool = True
    size: int = 0
    mtime: float = 0.0
    cloud_synced: bool = False
    """是否匹配该游戏的 Auto-Cloud 规则 (如 `*.sl2`) —— 匹配的会走云, 其余不会."""
    age_days_vs_source: float = 0.0
    """相对源文件修改时间的差值 (正 = 比源旧)."""

    @property
    def stale(self) -> bool:
        return self.age_days_vs_source >= _STALE_DAYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "size": self.size,
            "mtime": self.mtime,
            "cloud_synced": self.cloud_synced,
            "age_days_vs_source": round(self.age_days_vs_source, 2),
            "stale": self.stale,
        }


@dataclass
class PlanStep:
    """计划里的一步."""

    kind: str
    detail: str
    touches_disk: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "touches_disk": self.touches_disk}


@dataclass
class TransferPlan:
    """一次搬运的完整计划."""

    appid: str
    name: str
    verdict: str
    source: SourceInfo
    target: SaveTarget | None
    steps: list[PlanStep] = field(default_factory=list)
    guards: list[Guard] = field(default_factory=list)
    companions: list[CompanionFile] = field(default_factory=list)
    backup_path: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_BLOCKED

    @property
    def blockers(self) -> list[Guard]:
        return [g for g in self.guards if g.blocking]

    @property
    def warnings(self) -> list[Guard]:
        return [g for g in self.guards if not g.blocking]

    @property
    def will_write(self) -> bool:
        return any(s.touches_disk for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "name": self.name,
            "verdict": self.verdict,
            "blocked": self.blocked,
            "will_write": self.will_write,
            "source": self.source.to_dict(),
            "target": self.target.to_dict() if self.target else None,
            "steps": [s.to_dict() for s in self.steps],
            "guards": [g.to_dict() for g in self.guards],
            "companions": [c.to_dict() for c in self.companions],
            "backup_path": self.backup_path,
            "notes": list(self.notes),
        }


def _companions(
    target_abs: Path,
    source_mtime: float,
    cloud_patterns: Iterable[str],
) -> list[CompanionFile]:
    """扫目标目录, 列出除目标本身以外的文件, 并标注是否走云 / 是否比源旧."""
    parent = target_abs.parent
    if not parent.is_dir():
        return []
    patterns = [p for p in cloud_patterns if p]
    out: list[CompanionFile] = []
    try:
        entries = sorted(parent.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    now = time.time()
    for p in entries:
        if p.name.lower() == target_abs.name.lower():
            continue
        try:
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        synced = any(fnmatch.fnmatch(p.name, pat) for pat in patterns)
        # 源文件 mtime 未知时退化为"相对当前时间", 避免把一切都标成 stale
        ref = source_mtime if source_mtime > 0 else now
        age = (ref - st.st_mtime) / 86400.0
        out.append(
            CompanionFile(
                name=p.name,
                path=str(p),
                exists=True,
                size=st.st_size,
                mtime=st.st_mtime,
                cloud_synced=synced,
                age_days_vs_source=age,
            )
        )
    return out


def _backup_name(target_abs: Path, when: float | None = None) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(when if when else time.time()))
    return f"{target_abs.name}.{ts}.scb-bak"


def build_plan(
    *,
    source: Path | None,
    resolution: TargetResolution,
    appid: str,
    name: str = "",
    accountid: int | None = None,
    steam_is_running: bool | None = None,
    backup_dir: Path | None = None,
    cloud_patterns: Iterable[str] = (),
    channel: Any = None,
) -> TransferPlan:
    """算出一次搬运的完整计划. **只读, 不写任何文件.**

    守卫全绿且源/目标 sha1 不同时 `verdict == "copy"`, 并给出逐步动作;
    目标已一致时 `verdict == "skip-identical"`; 命中 `block` 守卫时 `verdict == "blocked"`.

    `channel` 传入 `cloudchannel.ChannelState` 时, 计划里会带上**会话级**云通道结论.
    """
    from .guard import check_all  # 局部导入, 避免与 guard 的循环引用

    target = resolution.primary if resolution else None

    guards = check_all(
        source=source,
        target=target,
        resolution=resolution,
        accountid=accountid,
        steam_is_running=steam_is_running,
        channel=channel,
    )

    src = SourceInfo(path=Path(source) if source else Path(""))
    if source is not None and src.path.exists() and src.path.is_file():
        try:
            st = src.path.stat()
            src.exists = True
            src.size = st.st_size
            src.mtime = st.st_mtime
        except OSError:
            src.exists = False
        if src.exists:
            src.sha1 = sha1_of(src.path)

    plan = TransferPlan(
        appid=str(appid),
        name=name or str(appid),
        verdict=VERDICT_BLOCKED if any(g.blocking for g in guards) else VERDICT_COPY,
        source=src,
        target=target,
        guards=guards,
    )

    if plan.blocked:
        plan.notes.append("存在阻断项, 已拒绝执行 (默认 dry-run, 不会写任何文件)")
        return plan

    assert target is not None and target.abs_path is not None
    tgt_path = target.abs_path

    # 伴生文件: 无论搬不搬都要报, 这是 4b 的真风险点
    plan.companions = _companions(tgt_path, src.mtime, cloud_patterns)
    stale = [c for c in plan.companions if c.exists and not c.cloud_synced and c.stale]
    if stale:
        plan.notes.append(
            "目标目录存在比源档更旧的伴生文件 (不走云, 不会被自动更新): "
            + ", ".join(c.name for c in stale)
            + " —— 若游戏报存档异常, 优先怀疑它们"
        )

    # 目标已存在且内容一致 -> 无需搬运
    if target.exists and src.sha1 and target.remote_sha and src.sha1 == target.remote_sha:
        plan.verdict = VERDICT_SKIP
        plan.notes.append("源与云端登记的 sha 一致, 无需搬运")
        return plan
    if target.exists and src.sha1:
        local_sha = sha1_of(tgt_path)
        if local_sha and local_sha == src.sha1:
            plan.verdict = VERDICT_SKIP
            plan.notes.append("源与目标本地文件 sha 一致, 无需搬运")
            return plan
        if local_sha and target.remote_sha and local_sha != target.remote_sha:
            plan.notes.append(
                "注意: 目标本地文件与云端登记的 sha 不一致 —— 目标机本地已被改动过"
            )

    # ---- 生成动作序列 ----
    if backup_dir is not None and target.exists:
        plan.backup_path = str(Path(backup_dir) / _backup_name(tgt_path))

    if target.exists:
        plan.steps.append(
            PlanStep(
                "backup",
                f"备份现有目标 -> {plan.backup_path or '(未指定备份目录, 将跳过备份)'}",
                touches_disk=False if plan.backup_path is None else True,
            )
        )
    else:
        plan.notes.append("目标文件当前不存在 —— 本次是新建而非覆盖")

    plan.steps.append(
        PlanStep(
            "write",
            f"写入 {tgt_path} (临时文件 + 原子替换, 并显式刷新 mtime)",
            touches_disk=True,
        )
    )
    plan.steps.append(
        PlanStep(
            "verify",
            f"回读 sha1 校验, 期望 {src.sha1 or '(源不可读)'}",
            touches_disk=False,
        )
    )
    plan.steps.append(
        PlanStep(
            "trigger-sync",
            f"[M3] 触发云上行 —— `apply` 默认不执行; 加 `--then-sync` 即自动完成 "
            f"(启动 Steam -> 点亮本会话通道 -> 发 cloud_sync_up -> 校验上传, 见 "
            f"`src/transport/trigger.py`); 或事后单独跑 `bridge.py sync --appid {plan.appid}`",
            touches_disk=False,
        )
    )
    return plan
