"""云通道守卫 (M3): 判断"**本会话**该游戏的云通道是否点亮", 并给出点亮/触发动作.

## 为什么必须按会话判定

`testappcloudpaths <appid>` 是开发者命令, **每次重启 Steam 都会失效**
(Steamworks 文档原文: "You can restart the Steam client to get rid of the console tab").
所以"这台机器上 DS3 的云通道"不是一个稳定属性, 而是**随 Steam 会话变化的状态**.

`src/steam/cloud_log.py` 的 `cloud_state` 是**全文件统计**: `toggle_effective` 只要看到
`Watching file` 行晚于旧的禁用行就判 `enabled`. 而 `Watching file` 会**累积历史** ——
上一个会话点亮过, 它的行永远留在日志里.

实测 (2026-10-12 11:42, 本机):

    文件级 cloud_state      : enabled      <- 假阳性
    toggle_effective        : True         <- 假阳性
    本会话 (11:37 起) 的 374320 行: 0 条     <- 真相: 未点亮

**未点亮时 Steam 的启动评估会把该 appid 整个跳过** —— 连
`AutoCloud is disabled` 都不打印. 所以"本会话零活动"本身就是一个有效信号.

## 会话边界

`cloud_log.txt` 里每次 Steam 启动会打印一行 `Client version: <build>`.
**最后一次 `Client version:` 之后**即当前会话.

⚠ 但这一行是**懒惰**的: Steam 启动时不一定立刻写 —— 实测 (2026-10-12 14:12)
`steam.exe -shutdown` 重启后 `cloud_log.txt` **零增长**, 没有新的 `Client version:`.
此时日志里的"最后一次边界"其实**已经是过去会话**, 上一会话的 `Watching file` /
点亮回执会被当成"本会话已点亮" → **假阳性**. 因此本模块额外吃一个
`steam_start_epoch` (来自 `src/steam/processes.py`): 只要它晚于日志里最后一条会话边界,
就认定"日志会话已过期", 本会话按零活动处理 (console 回执也必须晚于 `steam_start_epoch`).

## 本模块只做纯逻辑

"把命令敲进 Steam 控制台"是 GUI 操作, 由调用方以可注入的 `Sender` 提供
(真实实现见 `tools/bridge.py`, 复用 `tools/steam_console_send.py` 的 ctypes 通路).
这样"判定 / 编排"与"发送"解耦, 前者可在沙箱里被完整测试.
"""

from __future__ import annotations

import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "ChannelState",
    "ConsoleAck",
    "Sender",
    "command_enable",
    "command_sync_up",
    "command_sync_down",
    "command_sync_app",
    "detect_channel",
    "detect_channel_from_file",
    "detect_channel_from_files",
    "console_ack",
    "console_ack_from_file",
    "sync_markers",
    "SESSION_MARKER",
    "DISABLED_MARKER",
    "DISABLED_FLAVOUR",
]

# ---------------------------------------------------------------- 常量

SESSION_MARKER = "Client version:"
"""每次 Steam 启动打印一次 —— 会话边界. 实测本机 4 次启动 ↔ 4 条."""

DISABLED_MARKER = "AutoCloud is disabled"
"""该 appid 的云被明确关闭. **只在部分时机打印**, 不能只靠它判未点亮."""

DISABLED_FLAVOUR = "Sync Disabled"
"""`Starting sync (AC Launch,Sync Disabled,)` 里的标记位 —— 表示**这次同步不做网络传输**.

⚠ 2026-10-13 更正: `plan-v3.md` 早先把它读成"每个 AutoCloud 游戏都有的常规标记",
`how-steam-cloud-works.md` 已勘误 —— 它是**真信号**. 本项目在朋友机上就栽在这里:
日志里明写着 `Sync Disabled` + `AutoCloud is disabled`, 判据却报"已点亮",
于是**跳过了点亮那一步**, 白跑一整轮.
"""

ACTIVE_MARKERS = (
    "Attempting to sync",
    "Successfully synced to ChangeNumber",
    "Upload batch initiated",
    "Upload OK for file",
    "Download OK for file",
    "HTTP upload",
    "HTTP download",
)
"""**只有传输真的被启用**才可能出现的行.

⚠ 2026-10-13 重大更正: 这里原本放的是 `Starting sync` / `Evaluating rule` / `Watching file` /
`Running AutoCloud` / `AutoCloud complete` / `Persisting file` / `Found `, 并注着
"禁用期不会出现任何一个" —— **那句是错的**. 拿本仓 `cases/local-4b-2026-10-12` 的基线日志实测,
一个**禁用态** burst 会把这些一条不少地照打:

    Starting sync (AC Launch,Sync Disabled,)
    Running AutoCloud on launch. Looking for existing files to watch:
        Watching file C:\\...\\Settings.json
    AutoCloud done. Watching 1 files

⇒ 照旧判据会把"未点亮"读成"已点亮". 真正的分水岭只有两处, 见 `sync_markers()`:
`Starting sync` 的标记位 (`down`/`up` 会传输 vs `Sync Disabled` 不传输),
以及 "Need to sync … **but not attempting now**"(禁用) vs "**Attempting to sync**"(启用).
"""

CONSOLE_SESSION_MARKER = "Console Log Start"
"""`console_log.txt` 的会话边界 (Steam 启动时写一条)."""

_APPID_RE = re.compile(r"\[AppID\s+(\d+)\]")
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


# ---------------------------------------------------------------- 控制台发送抽象

Sender = Callable[[str], bool]
"""把一行命令送进 Steam 控制台. 返回是否确认送达 (不代表命令执行成功)."""


def command_enable(appid: str) -> str:
    """点亮该 appid 的云通道 (开发者命令, 重启即失效)."""
    return f"testappcloudpaths {appid}"


def command_sync_up(appid: str) -> str:
    """手动触发**上行**(等价于游戏退出后的 after-game sync). 不必启动游戏."""
    return f"cloud_sync_up {appid}"


def command_sync_down(appid: str) -> str:
    """手动触发**下行**(等价于游戏启动时的 on-launch sync)."""
    return f"cloud_sync_down {appid}"


def command_sync_app(appid: str) -> str:
    """让 Steam 自己决定方向地触发一次后台同步."""
    return f"cloud_sync_app {appid}"


# ---------------------------------------------------------------- 行解析小工具


def _ts_of(line: str) -> str:
    m = _TS_RE.match(line)
    return m.group(1) if m else ""


def _parse_ts(line: str) -> float | None:
    """`[YYYY-MM-DD HH:MM:SS]` -> epoch (本地). 解析不了返回 `None`."""
    raw = _ts_of(line)
    if not raw:
        return None
    try:
        return _time.mktime(_time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError, OSError):
        return None


def _appid_of(line: str) -> str:
    m = _APPID_RE.search(line)
    return m.group(1) if m else ""


_SYNC_RE = re.compile(r"Starting sync \(([^)]*)\)")

SYNC_MARKER_DOWN = "down"
SYNC_MARKER_UP = "up"


def sync_markers(line: str) -> list[str]:
    """取 `Starting sync (a,b,c)` 括号里的标记位; 不是这种行就返回 `[]`.

    这串标记位是**唯一区分"这次同步做不做网络传输"**的地方 (实测):

        (AC Launch,down,)                   -> ['AC Launch', 'down']    会传输
        (up,AC Exit,)                       -> ['up', 'AC Exit']        会传输
        (AC Launch,Sync Disabled,)          -> [..., 'Sync Disabled']   不传输
        (init,eval,)                        -> ['init', 'eval']         中性 (启动评估)

    ⚠ 最后一种要当**中性**: Steam 启动时对每个 AutoCloud 游戏都会跑一遍 init/eval,
    跟开关状态无关 —— 把它当"进过云流程"就是假阳性来源之一.
    """
    m = _SYNC_RE.search(line)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _console_lit_evidence(
    appid: str, console_lines: Sequence[str], min_epoch: float | None = None
) -> tuple[bool, str]:
    """在 `console_log.txt` 里找"本会话已执行过点亮命令"的**直接证据**.

    形态 (实测, 2026-10-12 13:32:59):

        [2026-10-12 13:32:59] "testappcloudpaths" = "374320"

    注意它**不是** `find` 的变量清单形态 (`testappcloudpaths = "374320" - `),
    而是**命令回显**: 名字带引号、后面跟 `=` 和值. 只有命令真的被执行才会打印.

    为什么需要它: `testappcloudpaths` 只是设一个控制台变量, **不会立刻产生
    `cloud_log.txt` 活动** (要等一次 sync / 进出游戏). 所以只看 `cloud_log` 会把
    "刚点亮但还没跑过同步"误判成"未点亮", 进而让 `sync` 错拒. console_log 的回执
    是更早、更直接的证据.

    `min_epoch` 给出"Steam 进程启动时间": 回执行必须**晚于**它才算本会话的,
    否则 `Console Log Start` 没写新行时会拿上一会话的回执充数 (假阳性).

    返回 `(是否落在当前会话, 回执行)`.
    """
    lines = list(console_lines)
    last_marker = -1
    for i, ln in enumerate(lines):
        if CONSOLE_SESSION_MARKER in ln:
            last_marker = i
    pat = re.compile(
        rf'"{re.escape("testappcloudpaths")}"\s*=\s*"{re.escape(str(appid))}"'
    )
    hit = ""
    in_session = False
    for i, ln in enumerate(lines):
        if pat.search(ln):
            hit = ln.strip()
            if last_marker >= 0 and i <= last_marker:
                continue
            if min_epoch is not None:
                ts = _parse_ts(ln)
                if ts is not None and ts < min_epoch - 2.0:
                    continue  # 回执属于重启**之前**的会话
            in_session = True
    return in_session, hit


# ---------------------------------------------------------------- 通道状态


@dataclass
class ChannelState:
    """某个 appid 在**当前 Steam 会话**里的云通道状态."""

    appid: str
    lit: bool | None = None
    """`True` 已点亮 / `False` 未点亮 / `None` 无法判断 (缺少会话边界)."""

    session_marker_found: bool = False
    session_started_at: str = ""
    session_start_line: int = -1
    session_line_count: int = 0
    session_stale: bool = False
    """日志里的"最后会话"是否已被 `steam.exe` 启动时间抛在后面 (本会话还没留下记录)."""
    eval_completed: bool = False
    """本会话是否跑过启动评估 (`Starting sync (eval,)`). 有则"零活动"= 确被跳过."""

    active_lines: list[str] = field(default_factory=list)
    disabled_lines: list[str] = field(default_factory=list)
    console_ack_line: str = ""
    """`console_log.txt` 里 `"testappcloudpaths" = "<appid>"` 的回执行 (本会话)."""
    reason: str = ""
    hint: str = ""

    @property
    def needs_enable(self) -> bool:
        return self.lit is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "lit": self.lit,
            "needs_enable": self.needs_enable,
            "session_marker_found": self.session_marker_found,
            "session_started_at": self.session_started_at,
            "session_line_count": self.session_line_count,
            "session_stale": self.session_stale,
            "eval_completed": self.eval_completed,
            "active_count": len(self.active_lines),
            "disabled_count": len(self.disabled_lines),
            "console_ack_line": self.console_ack_line,
            "reason": self.reason,
            "hint": self.hint,
        }


def detect_channel(
    appid: str,
    lines: Sequence[str],
    console_lines: Sequence[str] | None = None,
    steam_start_epoch: float | None = None,
) -> ChannelState:
    """按会话判定 `appid` 的云通道是否点亮.

    规则 (`Sync Disabled` 与"枚举行"两态通吃这件事在 2026-10-13 更正过):

    * 本会话内见到 `AutoCloud is disabled` 或 `Sync Disabled`, 且**不比**传输证据
      更旧 -> **未点亮** (优先于下面两条; 同一秒也算未点亮)
    * 本会话内见到"会传输"的同步 (`Starting sync` 带 `down`/`up`) 或真实传输行
      (`Attempting to sync` / `Successfully synced to ChangeNumber` / `Upload OK for file` …)
      -> **已点亮**
    * 本会话 `console_log` 回执确认点亮过   -> **已点亮** (见 `_console_lit_evidence`)
    * 本会话内该 appid **零活动**          -> **未点亮** (被启动评估跳过)
    * `steam.exe` 启动晚于日志最后一条会话边界 -> **未点亮** (会话已过期, 见 `session_stale`)
    * 找不到会话边界 (`Client version:`)   -> 退回全文件; 有活动才敢说点亮, 否则 `None`

    ⚠ 中性的 `Starting sync (init,eval,)`、以及 `Running AutoCloud` / `Watching file` /
    `AutoCloud done` 这类**枚举行**, 两态都会打, **一律不作为活动证据** (见 `ACTIVE_MARKERS`).
    """
    appid = str(appid)
    lines = list(lines)
    st = ChannelState(appid=appid)

    # ---- 1) 定位最后一次会话边界 ----
    last_idx = -1
    for i, ln in enumerate(lines):
        if SESSION_MARKER in ln:
            last_idx = i
            st.session_started_at = _ts_of(ln)
    st.session_marker_found = last_idx >= 0
    st.session_start_line = last_idx

    if st.session_marker_found:
        session = lines[last_idx + 1 :]
        st.session_line_count = len(session)
        st.eval_completed = any("Starting sync (eval,)" in ln or "Eval complete" in ln for ln in session)
    else:
        session = lines  # 降级: 全文件
        st.session_line_count = len(session)

    # ---- 1b) 兜底边界: `steam.exe` 的启动时间 ----
    # `Client version:` 是**懒惰**的: Steam 重启后不一定立刻写. 只要 steam.exe 起得
    # 比日志里最后一条边界还晚, 日志会话就**已过期** —— 本会话按零活动处理,
    # 不吃上一会话的 `Watching file` / 点亮回执 (那是**假阳性**来源).
    if last_idx >= 0 and steam_start_epoch is not None:
        boundary_ts = _parse_ts(lines[last_idx])
        if boundary_ts is None or steam_start_epoch > boundary_ts + 1.0:
            st.session_stale = True
            session = []
            st.session_line_count = 0
            st.eval_completed = False

    # ---- 2) 本会话内该 appid 的禁用/活跃证据 ----
    # 分类是**互斥**的: 一条行要么是禁用证据、要么是传输已启用的证据、要么两者都不是
    # (中性行). 早先的写法是两个独立 `if`, 于是 `Starting sync (…,Sync Disabled,)`
    # 既进 disabled 又进 active, 而判定块先看 active ⇒ 禁用态被读成"已点亮".
    for ln in session:
        if _appid_of(ln) != appid:
            continue
        marks = sync_markers(ln)
        if DISABLED_MARKER in ln or DISABLED_FLAVOUR in marks:
            st.disabled_lines.append(ln)
        elif SYNC_MARKER_DOWN in marks or SYNC_MARKER_UP in marks:
            st.active_lines.append(ln)
        elif any(k in ln for k in ACTIVE_MARKERS):
            st.active_lines.append(ln)

    # ---- 3) 判定 ----
    console_lit = False
    if console_lines is not None:
        console_lit, ack_line = _console_lit_evidence(
            appid, console_lines, min_epoch=steam_start_epoch
        )
        if console_lit:
            # 只记**本会话**的回执行; 上一会话的残留行不显示 (避免误导)
            st.console_ack_line = ack_line

    def _last_ts(bucket: list[str]) -> float:
        return max((_parse_ts(x) or 0.0) for x in bucket) if bucket else -1.0

    last_dis = _last_ts(st.disabled_lines)
    # 控制台回执 (`"testappcloudpaths" = "<appid>"`) 是**当场可读**的生效状态,
    # 所以它也参与"谁更晚"的比较 —— 否则"刚点亮、还没跑同步"会被本会话里
    # 更早的那条禁用行一直压着.
    last_ack = (_parse_ts(st.console_ack_line) or 0.0) if console_lit else -1.0
    last_lit = max(_last_ts(st.active_lines), last_ack)

    # 同一会话里三种证据可能同时存在 (先被禁用、会话中途点亮). **谁更晚谁说了算**;
    # 时间戳相同或无法比较时一律按"未点亮"处理 —— 判断错的代价不对称:
    #   多点亮一次 = 幂等、无害 (点亮命令只设一个控制台变量, 不碰存档);
    #   把"未点亮"读成"已点亮" = 跳过点亮 ⇒ 整轮白跑 (2026-10-13 就是这么栽的).
    if last_dis >= 0.0 and last_dis >= last_lit:
        st.lit = False
        st.reason = (
            "本会话内出现 `AutoCloud is disabled` / `Sync Disabled` "
            "—— 这次同步不做网络传输"
        )
    elif last_lit >= 0.0:
        st.lit = True
        if st.active_lines:
            st.reason = f"本会话内观察到 {len(st.active_lines)} 条 Auto-Cloud 活动记录"
        else:
            st.reason = (
                "本会话控制台回执确认已点亮 (`\"testappcloudpaths\" = \"appid\"`) —— "
                "尚未产生 cloud_log 活动 (要等一次 sync 或进出游戏)"
            )
    elif st.session_stale:
        st.lit = False
        st.reason = (
            "Steam 重启后 `cloud_log` 还没写过新的会话边界 (`Client version:`) —— "
            "日志里最后那个'已点亮'的会话**已经过去**, 本会话零活动 "
            "(`testappcloudpaths` 重启即失效, 必须在本会话内重新点亮)"
        )
    elif st.session_marker_found:
        st.lit = False
        st.reason = (
            "本会话内该 appid 零活动 —— Steam 的启动评估把它整个跳过了 "
            "(未点亮时连 `AutoCloud is disabled` 都不打印)"
        )
    else:
        st.lit = None
        st.reason = "找不到会话边界 (`Client version:`), 且本 appid 无任何活动记录, 无法判断"

    if st.lit is False:
        st.hint = (
            f"在 Steam 控制台执行 `{command_enable(appid)}` 点亮; "
            f"⚠ 每次重启 Steam 都会失效, 必须在**当前会话**内点亮"
        )
    return st


def detect_channel_from_file(
    appid: str, path: str | Path, steam_start_epoch: float | None = None
) -> ChannelState:
    """从 `cloud_log.txt` 读并判定. 文件不存在时返回 `lit=None` 的空状态."""
    p = Path(path)
    if not p.is_file():
        st = ChannelState(appid=str(appid))
        st.reason = f"日志不存在: {p}"
        return st
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        st = ChannelState(appid=str(appid))
        st.reason = f"日志不可读: {exc}"
        return st
    return detect_channel(appid, text.splitlines(), steam_start_epoch=steam_start_epoch)


def detect_channel_from_files(
    appid: str,
    cloud_log_path: str | Path,
    console_log_path: str | Path | None = None,
    steam_start_epoch: float | None = None,
) -> ChannelState:
    """同时读 `cloud_log.txt` 与 `console_log.txt` 判定.

    `console_log` 缺省时退化为只看 `cloud_log` (等价 `detect_channel_from_file`).
    存在的意义: 点亮命令 (`testappcloudpaths`) 的痕迹**只写在 console_log 里**,
    只看 cloud_log 会把"刚点亮、还没跑同步"误判成未点亮.

    `steam_start_epoch` 是兜底会话边界 (见 `detect_channel`): 重启后 cloud_log 没写新
    `Client version:` 时, 它能让"上一会话的点亮证据"不被当成本会话的.
    """
    st = detect_channel_from_file(
        appid, cloud_log_path, steam_start_epoch=steam_start_epoch
    )
    if console_log_path is None:
        return st
    p = Path(console_log_path)
    if not p.is_file():
        return st
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return st
    lit, ack_line = _console_lit_evidence(
        appid, text.splitlines(), min_epoch=steam_start_epoch
    )
    if lit:
        st.console_ack_line = ack_line
    if lit and st.lit is not True:
        st.lit = True
        st.reason = (
            "本会话控制台回执确认已点亮 (`\"testappcloudpaths\" = \"appid\"`) —— "
            "尚未产生 cloud_log 活动 (要等一次 sync 或进出游戏)"
        )
        st.hint = ""
    return st


# ---------------------------------------------------------------- 控制台回执


@dataclass
class ConsoleAck:
    """`console_log.txt` 里对某条命令的回执."""

    command: str
    found: bool = False
    last_line: str = ""
    count: int = 0
    in_session: bool = False
    """回执是否落在**当前**控制台会话 (`Console Log Start` 之后)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "found": self.found,
            "count": self.count,
            "in_session": self.in_session,
            "last_line": self.last_line,
        }


def console_ack(command_name: str, value: str, lines: Sequence[str]) -> ConsoleAck:
    """在 `console_log.txt` 里找命令的**送达/生效**痕迹.

    实测有两种形态, 都要认:

    * 命令行回显: `[ts] "testappcloudpaths" = "374320"`
    * 变量列表 (`find <kw>` 的输出): `[ts] testappcloudpaths = "374320" - `

    第二种尤其重要: `testappcloudpaths` 本质是个**控制台变量**
    (实测 `find cloud` 会把它作为变量列出来), 所以 `find testappcloudpaths`
    的输出就是"这个值现在是什么"的权威答案.
    """
    ack = ConsoleAck(command=f"{command_name} {value}")
    lines = list(lines)

    last_marker = -1
    for i, ln in enumerate(lines):
        if CONSOLE_SESSION_MARKER in ln:
            last_marker = i

    quoted = f'"{command_name}"'
    bare_re = re.compile(rf"(?<![\w.]){re.escape(command_name)}\s*=")
    val_needle = f'"{value}"' if value else ""

    for i, ln in enumerate(lines):
        if val_needle and val_needle not in ln:
            continue
        if quoted not in ln and not bare_re.search(ln):
            continue
        ack.count += 1
        ack.last_line = ln.strip()
        ack.found = True
        if last_marker < 0 or i > last_marker:
            ack.in_session = True
    return ack


def console_var_value(name: str, lines: Sequence[str]) -> str | None:
    """从 `find <name>` 的输出里取变量当前值 (`name = "value" - ` 形态).

    返回**最后一次**出现的值; 没有则 `None`.
    """
    pat = re.compile(rf"(?<![\w.]){re.escape(name)}\s*=\s*\"([^\"]*)\"")
    found: str | None = None
    for ln in lines:
        m = pat.search(ln)
        if m:
            found = m.group(1)
    return found


def console_ack_from_file(
    command_name: str, value: str, path: str | Path
) -> ConsoleAck:
    p = Path(path)
    if not p.is_file():
        return ConsoleAck(command=f"{command_name} {value}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ConsoleAck(command=f"{command_name} {value}")
    return console_ack(command_name, value, text.splitlines())
