"""前置守卫: 会破坏存档的操作, 一律先被拦下.

## 为什么要单独一层

`docs/plan-v3.md` 记录的事故 D3 是"把**游戏安装目录**当成存档目录写入". 搬运器
一旦往错的地方写文件, 用户丢的是真存档 —— 这类错误靠"小心一点"是防不住的, 必须
变成**可测试的显式检查**.

守卫分两级:

* `block` —— 命中即拒绝执行, 计划判为 `blocked`.
* `warn`  —— 可以继续, 但必须在报告里出现.

## 红线

1. **Steam 运行中禁止改写被监视文件** —— 实测确认云端仲裁会与被监视文件抢,
   启动期的下行会把本地删除的文件灌回来 (`cases/local-4b-2026-10-12/ANALYSIS.md`).
2. **禁止跨账号导入** —— 目标存档根的 `steam_autocloud.vdf` 记录了归属账号.
3. **落点不明拒绝执行**.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cloudchannel import ChannelState, command_enable
from .target import SaveTarget, TargetResolution, autocloud_accountid, save_root_of

__all__ = [
    "Guard",
    "steam_running",
    "check_all",
    "BLOCK",
    "WARN",
]

BLOCK = "block"
WARN = "warn"


@dataclass
class Guard:
    """一条前置检查结论."""

    code: str
    level: str
    message: str
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        return self.level == BLOCK

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "remedy": self.remedy,
        }


_ENV_STEAM_STATE = "SCB_STEAM_RUNNING"


def steam_running() -> bool | None:
    """Steam 客户端是否在运行.

    返回 `True` / `False` / `None`(无法判断). **判断不出时返回 None 而不是 False** ——
    让调用方有机会把它当成"不确定"来提示, 而不是默默放行.

    自动化测试钩子: 环境变量 `SCB_STEAM_RUNNING` (`0`/`1`) 可显式钉死结果.
    隔离端到端测试里没法让真机 Steam 停下, 只能这样把这一项确定下来.
    **它只影响"Steam 是否在运行"这一项检查, 不绕过任何其他守卫.**
    """
    raw = os.environ.get(_ENV_STEAM_STATE)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no", "off")
    try:
        if os.name == "nt":
            exe = shutil.which("tasklist")
            if not exe:
                return None
            proc = subprocess.run(
                [exe, "/FI", "IMAGENAME eq steam.exe", "/NH"],
                capture_output=True,
                timeout=10,
            )
            text = proc.stdout.decode("gbk", "replace").lower()
            return "steam.exe" in text
        proc = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, timeout=10)
        return proc.returncode == 0
    except Exception:
        return None


def _guard_source(source: Path | None) -> list[Guard]:
    if source is None:
        return [
            Guard("SOURCE_MISSING", BLOCK, "未指定源存档文件", "用 --source <文件路径> 指定要搬运的存档")
        ]
    if not source.exists():
        return [Guard("SOURCE_MISSING", BLOCK, f"源文件不存在: {source}", "检查路径是否正确")]
    if not source.is_file():
        return [Guard("SOURCE_NOT_FILE", BLOCK, f"源不是普通文件: {source}", "指定具体文件, 不要用目录")]

    guards: list[Guard] = []
    try:
        size = source.stat().st_size
    except OSError as exc:
        return [Guard("SOURCE_UNREADABLE", BLOCK, f"无法读取源文件属性: {exc}", "检查文件权限")]
    if size == 0:
        guards.append(
            Guard("SOURCE_EMPTY", BLOCK, "源文件是空的 (0 字节)", "确认导出/拷贝是否完整")
        )
    return guards


def _guard_steam(running: bool | None) -> list[Guard]:
    if running is True:
        return [
            Guard(
                "STEAM_RUNNING",
                BLOCK,
                "Steam 正在运行 —— 此时改写被监视文件会与云端仲裁抢文件",
                "完全退出 Steam (右下角托盘 -> 退出) 后重试",
            )
        ]
    if running is None:
        return [
            Guard(
                "STEAM_RUNNING_UNKNOWN",
                WARN,
                "无法判断 Steam 是否在运行 (tasklist 不可用)",
                "请自行确认 Steam 已完全退出, 再决定是否继续",
            )
        ]
    return []


def _guard_target(target: SaveTarget | None, resolution: TargetResolution) -> list[Guard]:
    if target is None:
        detail = "; ".join(resolution.notes) or "没有任何落点证据"
        return [
            Guard(
                "TARGET_UNRESOLVED",
                BLOCK,
                f"无法确定该游戏的云存落点 ({detail})",
                "先在本机启动一次该游戏, 让 Steam 在 cloud_log / remotecache 里留下落点记录",
            )
        ]

    guards: list[Guard] = []
    p = target.abs_path
    if p is None:
        return [
            Guard(
                "TARGET_UNRESOLVED",
                BLOCK,
                f"落点 {target.rel_path!r} 无法展开成本机绝对路径 (root={target.root_enum})",
                "把该机的 cloud_log.txt 与 remotecache.vdf 一起回传, 以便补齐 root 语义",
            )
        ]

    if target.confidence == "unresolved":
        guards.append(
            Guard(
                "TARGET_UNRESOLVED",
                BLOCK,
                f"落点 {p} 只是语义推断, 未经磁盘或日志证实",
                "拒绝按猜出来的路径写入",
            )
        )

    parent = p.parent
    if not parent.is_dir():
        guards.append(
            Guard(
                "TARGET_PARENT_MISSING",
                BLOCK,
                f"目标父目录不存在: {parent}",
                "确认游戏已在该机运行过并生成了存档目录",
            )
        )

    if p.exists() and not p.is_file():
        guards.append(
            Guard("TARGET_NOT_FILE", BLOCK, f"目标路径已存在且不是普通文件: {p}", "人工检查该路径")
        )

    if target.root_enum == "1":
        guards.append(
            Guard(
                "TARGET_IS_INSTALL_DIR",
                BLOCK,
                "落点被解析到「游戏安装目录」(root=1) —— 这正是事故 D3 的形态",
                "拒绝写入; 请核实适配器与云日志",
            )
        )
    return guards


def _guard_same_path(source: Path | None, target: SaveTarget | None) -> list[Guard]:
    if source is None or target is None or target.abs_path is None:
        return []
    if os.path.normcase(os.path.abspath(str(source))) == os.path.normcase(
        os.path.abspath(str(target.abs_path))
    ):
        return [
            Guard(
                "SAME_FILE",
                BLOCK,
                f"源与目标是同一个文件: {source}",
                "把源文件拷到别处再执行 (原地覆盖无意义且危险)",
            )
        ]
    return []


def _guard_cross_account(target: SaveTarget | None, accountid: int | None) -> list[Guard]:
    """跨账号红线: 目标存档根记录的归属账号必须与本机当前账号一致."""
    if target is None or target.abs_path is None:
        return []
    root = save_root_of(target.abs_path)
    if root is None:
        return [
            Guard(
                "SAVE_ROOT_UNKNOWN",
                WARN,
                "目标存档根没有 steam_autocloud.vdf, 无法核对归属账号",
                "确认这份存档确实属于本机当前账号",
            )
        ]
    owner = autocloud_accountid(root)
    if owner is None:
        return [
            Guard(
                "SAVE_OWNER_UNKNOWN",
                WARN,
                f"存档根的 steam_autocloud.vdf 里读不到 accountid ({root})",
                "确认这份存档确实属于本机当前账号",
            )
        ]
    if accountid is None:
        # 本机当前账号判不出来时**不能静默放过**: 跨账号是硬红线,
        # "检查没跑" 必须看得见, 否则会被读成"检查通过".
        return [
            Guard(
                "ACCOUNT_UNKNOWN",
                WARN,
                f"本机当前账号未知 (注册表 ActiveUser=0 且无唯一 userdata 线索) —— "
                f"无法核对存档归属账号 {owner} 是否一致, 跨账号红线本次**未生效**",
                "用 `--account <accountid>` 显式指定, 或确认 Steam 已登录且 userdata 下有该 appid 的目录",
            )
        ]
    if owner != int(accountid):
        return [
            Guard(
                "CROSS_ACCOUNT",
                BLOCK,
                f"目标存档根归属账号 {owner}, 与本机当前账号 {accountid} 不一致 —— 跨账号导入被禁止",
                "只做同账号自己存档的搬运; 跨账号导入既违规又可能触发封号",
            )
        ]
    return []


def _guard_channel(channel: ChannelState | None, appid: str) -> list[Guard]:
    """**会话级**云通道检查 (M3 新增).

    为什么不能只看 `resolution.cloud_disabled`: 那是**全文件统计**, 会被上一个
    Steam 会话留下的 `Watching file` 行骗过 (实测 `cloud_state=enabled` 而本会话
    实际未点亮). 这里用 `cloudchannel` 的会话感知判定, 结论才可用.

    未点亮**不阻断搬运** —— 搬运本身仍是安全的 (只是不会被同步上云).
    但必须**显式提示**, 否则用户会以为搬完就完事, 结果是白搬.
    """
    if channel is None:
        return []
    if channel.lit is False:
        return [
            Guard(
                "CLOUD_CHANNEL_UNLIT",
                WARN,
                f"本 Steam 会话内该游戏的云通道**未点亮** ({channel.reason}) —— "
                f"文件搬进去了, 云也不会同步",
                channel.hint or f"在 Steam 控制台执行 `{command_enable(appid)}`, 然后触发一次同步",
            )
        ]
    if channel.lit is None:
        return [
            Guard(
                "CLOUD_CHANNEL_UNKNOWN",
                WARN,
                f"无法判定本会话云通道状态 ({channel.reason})",
                "确认 Steam 已启动并登录, 再重新诊断",
            )
        ]
    return []


def _guard_cloud(resolution: TargetResolution, appid: str) -> list[Guard]:
    guards: list[Guard] = []
    if resolution.cloud_disabled:
        guards.append(
            Guard(
                "CLOUD_DISABLED",
                WARN,
                f"该游戏的云通道**曾**处于禁用状态 (cloud_log **全文件**里存在 "
                f"`AutoCloud is disabled`; 这是历史统计, 不代表本会话 —— "
                f"本会话是否点亮以 `CLOUD_CHANNEL_*` 为准)",
                f"在 Steam 控制台执行 `testappcloudpaths {appid}` 点亮; "
                f"注意每次重启 Steam 都会失效",
            )
        )
    first = resolution.primary
    if first is not None and not first.has_remote_copy and first.exists:
        guards.append(
            Guard(
                "CLOUD_NO_REMOTE_COPY",
                WARN,
                "云端没有该文件的副本 (remotetime=0) —— 上传尚未成功过",
                "点亮云通道后进出游戏一次, 确认云端出现副本再依赖它做跨机搬运",
            )
        )
    return guards


def _guard_source_name(source: Path | None, target: SaveTarget | None) -> list[Guard]:
    """源文件名与目标名不一致时提示 —— 常是"拿错了文件"的前兆."""
    if source is None or target is None or target.abs_path is None:
        return []
    if source.name.lower() != target.abs_path.name.lower():
        return [
            Guard(
                "SOURCE_NAME_DIFFERS",
                WARN,
                f"源文件名 {source.name!r} 与目标名 {target.abs_path.name!r} 不同 —— 将按目标名写入",
                "确认这是同一份存档 (例如同一次导出的副本)",
            )
        ]
    return []


def check_all(
    *,
    source: Path | None,
    target: SaveTarget | None,
    resolution: TargetResolution,
    accountid: int | None,
    steam_is_running: bool | None = None,
    channel: ChannelState | None = None,
) -> list[Guard]:
    """跑完整套前置检查, 保序返回.

    参数 `steam_is_running` 可显式传入以便测试; 传 `None` 会真的去探测进程.
    参数 `channel` 传入会话级云通道状态 (`cloudchannel.ChannelState`) 时,
    会追加一条精确的通道检查; 传 `None` 则跳过 (保持 M2 的旧行为).
    """
    running = steam_running() if steam_is_running is None else steam_is_running
    guards: list[Guard] = []
    guards += _guard_steam(running)
    guards += _guard_source(source)
    guards += _guard_target(target, resolution)
    guards += _guard_same_path(source, target)
    guards += _guard_cross_account(target, accountid)
    guards += _guard_cloud(resolution, resolution.appid)
    guards += _guard_channel(channel, resolution.appid)
    guards += _guard_source_name(source, target)
    return guards
