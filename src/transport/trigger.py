"""`apply` 之后的"触发云上行"编排（M3 · **trigger-sync**）。

## 为什么需要一层独立编排，而不是让 `apply` 直接调 `sync`

两个动作的运行前提是**互斥**的：

| 动作 | 前提 |
|---|---|
| `apply` 写盘 | **Steam 必须没在跑** —— `guard.STEAM_RUNNING` 会拦下（避免与云端仲裁抢文件，实测启动期下行会把本地删掉的文件灌回来） |
| 触发云上传 | **Steam 必须在跑** —— 同步是 Steam 的职责 |

所以"写完就自动上云"**必然跨过一道 Steam 启停**。
而且更关键的：**游戏级云开关是会话级、易失的**（控制台 `testappcloudpaths <appid>`，
每次重启 Steam 失效）—— 新起的 Steam 会话里通道是**未点亮**的，
此时直接发 `cloud_sync_up` 不会生效（`sync` 的前置检查也会正确地拒发）。

本模块把这几步串起来：

    apply 写完（Steam 关闭）
      -> 启动 Steam，等它就绪
      -> **校验刚写的文件没被云端下行覆盖**（见下）
      -> 点亮本会话云通道（`testappcloudpaths`）
      -> `cloud_sync_up` + 校验 + 失败重试（复用 `verify.sync_and_verify`）

## 一条安全前提：Steam 起来之后先查"我的文件还在不在"

Steam 启动会做一次云同步（`Starting sync (init,)` / `(eval,)`，必要时 `(full)`）。
如果云端那份与本地不同，存在把**刚写进去的文件覆盖掉**的可能 —— 那语义就反了：
不是"我把档推上去"，而是"云端把我的档盖掉了"。
所以起来后先比 sha1；不符就**中止**，让用户走 `rollback`，
**绝不**把"云端的旧档又被传上去"报成成功。

## 可测试性

所有外部动作（进程探测 / 启动 / 通道判定 / 点亮 / 发键 / 读日志 / 睡）都是**注入的**，
所以整条编排能在沙箱里跑完，不碰真机 Steam。默认参数才是真实实现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import guard as guard_mod
from . import plan as plan_mod
from . import verify as verify_mod

__all__ = ["TriggerOutcome", "trigger_sync"]

ERROR_STEAM_NOT_STARTED = "STEAM_NOT_STARTED"
ERROR_STEAM_UNEXPECTED = "STEAM_UNEXPECTED"
ERROR_CLOUD_OVERWROTE = "CLOUD_OVERWROTE"
ERROR_CHANNEL_UNLIT = "CHANNEL_UNLIT"
ERROR_SYNC_FAILED = "SYNC_FAILED"


@dataclass
class TriggerOutcome:
    """一次 trigger-sync 的完整结果."""

    appid: str
    rel_path: str

    steam_was_running: bool | None = None
    steam_started: bool = False
    steam_ready: bool = False
    steam_wait_seconds: float = 0.0

    channel_was_lit: bool | None = None
    channel_enabled: bool = False
    channel_lit_after: bool | None = None

    local_sha_before: str = ""
    local_sha_after: str = ""
    overwritten_by_cloud: bool = False

    sync: Any = None
    """`verify.SyncOutcome`（成功与失败都带回来，便于打印判据）."""

    succeeded: bool = False
    error_code: str = ""
    error: str = ""
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "rel_path": self.rel_path,
            "steam_was_running": self.steam_was_running,
            "steam_started": self.steam_started,
            "steam_ready": self.steam_ready,
            "steam_wait_seconds": round(self.steam_wait_seconds, 1),
            "channel_was_lit": self.channel_was_lit,
            "channel_enabled": self.channel_enabled,
            "channel_lit_after": self.channel_lit_after,
            "local_sha_before": self.local_sha_before,
            "local_sha_after": self.local_sha_after,
            "overwritten_by_cloud": self.overwritten_by_cloud,
            "sync": self.sync.to_dict() if self.sync is not None else None,
            "succeeded": self.succeeded,
            "error_code": self.error_code,
            "error": self.error,
            "steps": list(self.steps),
        }


def _wait_until_up(
    running_fn: Callable[[], bool | None],
    deadline_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
) -> tuple[bool, float]:
    """轮询等 Steam 起来. 返回 `(是否已起来, 实际等待秒数)`."""
    waited = 0.0
    if running_fn() is True:
        return True, 0.0
    while waited < deadline_seconds:
        step = min(poll_seconds, deadline_seconds - waited) if poll_seconds > 0 else deadline_seconds
        sleep(step)
        waited += step
        if running_fn() is True:
            return True, waited
    return False, waited


def trigger_sync(
    appid: str,
    rel_path: str,
    *,
    target_path: str | Path | None = None,
    expected_sha1: str = "",
    steam_running_fn: Callable[[], bool | None] = guard_mod.steam_running,
    start_steam_fn: Callable[[], bool] | None = None,
    detect_channel_fn: Callable[[], Any],
    enable_fn: Callable[[], bool] | None = None,
    send: Callable[[str], bool],
    check: Callable[[], verify_mod.UploadCheck],
    allow_start_steam: bool = True,
    require_lit: bool = True,
    max_attempts: int = 3,
    settle_seconds: float = 20.0,
    steam_wait_seconds: float = 150.0,
    steam_settle_seconds: float = 18.0,
    poll_seconds: float = 2.0,
    sha1_fn: Callable[[Any], str] = plan_mod.sha1_of,
    sleep: Callable[[float], None] = time.sleep,
) -> TriggerOutcome:
    """执行"启动 Steam -> 点亮通道 -> 触发上传并校验"这一串动作.

    参数全部可注入（见模块 docstring 末节）。返回 `TriggerOutcome`；判据：
    `succeeded` 为真 ⇔ 上传已被确认（`verify` 的日志/`remotecache` 双证据）。
    """
    out = TriggerOutcome(appid=str(appid), rel_path=verify_mod.norm_rel(rel_path))

    def _step(msg: str) -> None:
        out.steps.append(msg)

    # ---------------------------------------------------------------- 0) 起点快照
    if expected_sha1 and target_path is not None:
        out.local_sha_before = sha1_fn(target_path)
    out.steam_was_running = steam_running_fn()

    # ---------------------------------------------------------------- 1) 让 Steam 处于运行态
    if out.steam_was_running is None:
        # 探测不出"在不在跑"时**不擅自拉起**(可能已经开着, 再拉一次等于重复启动),
        # 也不拒绝(旧 `sync` 的语义就是"不确定也继续, 但明确提示自行核对").
        out.steam_ready = True
        _step("! 无法确认 Steam 是否在运行 -> 继续, 请自行核对")
    elif out.steam_was_running is not True:
        if not allow_start_steam:
            out.error_code = ERROR_STEAM_UNEXPECTED
            out.error = (
                "Steam 未在运行，而本次不允许自动启动（去掉 --no-start-steam 可让它自己拉起）"
            )
            _step("跳过: Steam 未运行且未获准启动 -> 中止")
            return out
        if start_steam_fn is None:
            out.error_code = ERROR_STEAM_UNEXPECTED
            out.error = "Steam 未在运行，且没有可用的启动器"
            _step("跳过: 无启动器 -> 中止")
            return out

        _step("Steam 未运行 -> 启动（分离进程）")
        started = False
        try:
            started = bool(start_steam_fn())
        except Exception as exc:  # pragma: no cover - 平台相关
            _step(f"! 启动调用抛错: {exc}")
            started = False
        out.steam_started = started
        _step(f"启动调用返回: {'已投递' if started else '失败'}")

        out.steam_ready, out.steam_wait_seconds = _wait_until_up(
            steam_running_fn, steam_wait_seconds, poll_seconds, sleep
        )
        _step(f"等待 steam.exe 出现: {'是' if out.steam_ready else '否'}"
              f"（{out.steam_wait_seconds:.0f}s）")
        if not out.steam_ready:
            out.error_code = ERROR_STEAM_NOT_STARTED
            out.error = f"{steam_wait_seconds:.0f}s 内没有等到 steam.exe 起来"
            return out
        # 起来 ≠ 就绪（登录 / 初始化日志还要一会儿）
        if steam_settle_seconds > 0:
            _step(f"等 Steam 初始化（{steam_settle_seconds:.0f}s）")
            sleep(steam_settle_seconds)
    else:
        out.steam_ready = True
        _step("Steam 已在运行")

    # ---------------------------------------------------------------- 2) 我的文件还在不在
    if expected_sha1 and target_path is not None:
        out.local_sha_after = sha1_fn(target_path)
        if out.local_sha_after != expected_sha1:
            out.overwritten_by_cloud = True
            out.error_code = ERROR_CLOUD_OVERWROTE
            out.error = (
                "Steam 启动后目标文件已变 —— 疑似被云端下行覆盖（本次搬运未生效）。"
                f"写入时 sha1={expected_sha1}，现在={out.local_sha_after or '(不可读)'}"
            )
            _step("!! 本地文件与写入时不一致 -> 中止，未触发上传")
            return out
        _step(f"本地文件未被覆盖（sha1={out.local_sha_after[:12]}…）")

    # ---------------------------------------------------------------- 3) 点亮本会话云通道
    st = detect_channel_fn()
    out.channel_was_lit = getattr(st, "lit", None)
    _step(f"云通道（本会话）: {_lit_label(out.channel_was_lit)}")

    if out.channel_was_lit is not True:
        if enable_fn is None:
            if require_lit:
                out.error_code = ERROR_CHANNEL_UNLIT
                out.error = "云通道未点亮，且本次未获准点亮它"
                return out
        else:
            _step(f"发 testappcloudpaths {out.appid} 点亮")
            ok = False
            try:
                ok = bool(enable_fn())
            except Exception as exc:  # pragma: no cover - GUI 相关
                _step(f"! 点亮调用抛错: {exc}")
                ok = False
            out.channel_enabled = ok
            _step(f"点亮{'已受理' if ok else '未确认'}")
            st = detect_channel_fn()
            out.channel_lit_after = getattr(st, "lit", None)
            _step(f"重判云通道: {_lit_label(out.channel_lit_after)}")
            if require_lit and out.channel_lit_after is not True:
                out.error_code = ERROR_CHANNEL_UNLIT
                out.error = (
                    "点亮后仍未确认本会话通道已点亮 —— 不盲发同步命令"
                    "（可先 bridge.py channel --enable 手工确认）"
                )
                return out
    else:
        out.channel_lit_after = True

    # ---------------------------------------------------------------- 4) 触发 + 校验 + 重试
    _step(f"触发上行同步（最多 {max_attempts} 次，每次等 {settle_seconds:g}s）")
    syn = verify_mod.sync_and_verify(
        out.appid,
        out.rel_path,
        send=send,
        check=check,
        max_attempts=max_attempts,
        settle_seconds=settle_seconds,
        sleep=sleep,
    )
    out.sync = syn
    for a in syn.actions:
        _step(a)
    out.succeeded = bool(syn.succeeded)
    if not out.succeeded:
        out.error_code = ERROR_SYNC_FAILED
        out.error = syn.error or "上传未确认成功"
    return out


def _lit_label(lit: bool | None) -> str:
    return {True: "已点亮", False: "未点亮", None: "无法判断"}[lit]
