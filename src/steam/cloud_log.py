"""`<Steam>\\logs\\cloud_log.txt` 解析 —— 云存真相的最可靠来源.

## 为什么用它

Steam 客户端在每次启动 / 退出游戏时, 把 Auto-Cloud 的每一步写进 cloud_log.txt.
里面**直接给出被监视文件的绝对路径**:

    [AppID 1245620] Running AutoCloud on launch. Looking for existing files to watch:
    [AppID 1245620]     Watching file C:\\Users\\...\\EldenRing\\76561199194833618\\ER0000.sl2
    [AppID 1245620] AutoCloud done. Watching 1 files

这比扫 `steam_autocloud.vdf` 可靠得多 (声明文件存在 != 真在同步), 且**跨机器通用**.

## 提取的信号

| 正则目标 | 含义 |
|---|---|
| `Watching file <abs>` | 该 appid 实际被同步的文件 (绝对路径) |
| **`Evaluating rule N with root=.. path=.. pattern=..`** | **Auto-Cloud 的同步规则本身 —— 这才是自动云游戏的真相源** |
| **`Found N files that match <glob>`** | 该规则在本机命中的文件数 (按账号目录逐条列出) |
| **`Persisting file <abs> to the cloud`** | 本次真正被推入云的文件 |
| **`Need to upload file <云相对路径>`** | 待上传队列 (含"未修改"跳过项) |
| **`Successfully synced to ChangeNumber N`** | 当前状态已对齐到 CN N —— **通用语句, 不表示"本次上传成功"**. 上传路径里它反而出现在 `Upload batch initiated` **之前** (实测 3590/105600/219740/431960); Auto-Cloud 型 (DS3) 的上传路径从不打印它. 判断某文件是否上云请看 `remotecache` 的 `remotetime` |
| **`Upload batch initiated, ID: .., ChangeNumber: N`** | **真正向服务器发起了一次上传批次** —— 上传"有没有动手"的权威判据, 且跨机制通用 |
| `Upload complete in build list` | 上传完成 —— 实测只见于 AppID 7 (Steam 客户端自身) |
| `Starting sync (AC Launch,Sync Disabled,)` | Auto-Cloud 每次同步的**常规标记**, 与云开关无关 (全库每个自动云游戏都有) |
| `AutoCloud is disabled` | 该 appid 的云功能真被关闭 (本机全库仅 DS3 出现过 2 次) |
| `Running AutoCloud on launch/on exit` | 同步时机 |
| `from local change number 'X' to global change number 'Y'` | 两侧同步进度; 双 0 = 从未同步 |
| `detected incomplete sync after previous run` | Auto-Cloud 静默失败 |
| `GetAppQuotaUsage - Success` | 配额 (附带用量) |

## 为什么 `Watching file` 不够 (2026-10-05 实测更正)

`Watching file` **只在本机已有云端缓存时才会打印**. DS3 这类"云从未通过"的游戏
在启动阶段只会打印 `AutoCloud done. Watching 0 files`, 到退出阶段才**按规则扫盘**:

    [AppID 374320] Running AutoCloud on exit. Looking for new and updated files
    [AppID 374320]     Evaluating rule 0 with root="WinAppDataRoaming" path="DarkSoulsIII" pattern="*.sl2" (recursive)
    [AppID 374320]         Found 1 files that match C:\\Users\\...\\DarkSoulsIII\\01100001499602d2\\*.sl2
    [AppID 374320]     Persisting file C:\\Users\\...\\DS30000.sl2 to the cloud

所以只看 `Watching file` 会把"从未同步过的游戏"误判成"无云存".
规则行 (`Evaluating rule`) 与落盘行 (`Persisting file`) 才是跨机器通用的确证.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["CloudLogEntry", "CloudLog", "parse_cloud_log", "parse_cloud_logs"]

_APPID_RE = re.compile(r"\[AppID\s+(\d+)\]")

_PATTERNS: dict[str, re.Pattern[str]] = {
    "watching": re.compile(r"\[AppID\s+(\d+)\]\s+Watching file\s+(.+?)\s*$"),
    "watching_count": re.compile(r"\[AppID\s+(\d+)\]\s+AutoCloud done\.\s+Watching\s+(\d+)\s+files?"),
    # --- Auto-Cloud 规则引擎 (自动云游戏的真相源) ---
    "rule": re.compile(
        r'\[AppID\s+(\d+)\]\s+Evaluating rule\s+(\d+)\s+with\s+'
        r'root="([^"]*)"\s+path="([^"]*)"\s+pattern="([^"]+)"'
        r"(?P<recursive>\s+\(recursive\))?"
    ),
    "rule_found": re.compile(
        r"\[AppID\s+(\d+)\]\s+Found\s+(\d+)\s+files? that match\s+(.+?)\s*$"
    ),
    "persisting": re.compile(
        r"\[AppID\s+(\d+)\]\s+Persisting file\s+(.+?)\s+to the cloud\s*$"
    ),
    "skipping": re.compile(r"\[AppID\s+(\d+)\]\s+Skipping un-modified file\s+(.+?)\s*$"),
    "need_upload": re.compile(r"\[AppID\s+(\d+)\]\s+Need to upload file\s+(.+?)\s*$"),
    "synced_cn": re.compile(
        r"\[AppID\s+(\d+)\]\s+Successfully synced to ChangeNumber\s+(\d+)"
    ),
    "already_synced": re.compile(
        r"\[AppID\s+(\d+)\]\s+Currently already synced to global change number\s+'(\d+)'"
    ),
    # --- 开关 / 时机 / 进度 ---
    "disabled": re.compile(r"\[AppID\s+(\d+)\]\s+AutoCloud is disabled"),
    "enabled": re.compile(r"\[AppID\s+(\d+)\]\s+AutoCloud is enabled"),
    "on_launch": re.compile(r"\[AppID\s+(\d+)\]\s+Running AutoCloud on launch"),
    "on_exit": re.compile(r"\[AppID\s+(\d+)\]\s+Running AutoCloud on exit"),
    "incomplete": re.compile(
        r"\[AppID\s+(\d+)\]\s+AutoCloud detected incomplete sync after previous run"
    ),
    "change_numbers": re.compile(
        r"\[AppID\s+(\d+)\]\s+Need to sync from local change number '(\d+)' "
        r"to global change number '(\d+)'"
    ),
    "quota": re.compile(r"\[AppID\s+(\d+)\]\s+GetAppQuotaUsage\s*-\s*Success"),
    "testcmd": re.compile(r"testappcloudpaths\s+(\d+)"),
    "upload_batch": re.compile(
        r"\[AppID\s+(\d+)\]\s+Upload batch initiated,?\s*ID:\s*(\d+),?\s*"
        r"ChangeNumber:\s*(\d+)"
    ),
    "upload_complete": re.compile(r"\[AppID\s+(\d+)\]\s+Upload complete in build list"),
    # 真正表示"某个文件传上去了". 与上面的 build-list 语句不是一回事 ——
    # `Upload complete in build list` 在**下载**路径上也会打印, 不能当上传成功证据.
    "upload_ok_file": re.compile(
        r"\[AppID\s+(\d+)\]\s+Upload OK for file\s+(.+?)\s*$"
    ),
    # 云端删除成功. ⚠️ 与上传是**相反**的语义: 出现它说明云端副本被删掉了.
    # 实测 PEAK (3527290) 就是 `Need to delete file` → `Delete OK for file` →
    # `Upload complete, result OK` → remotecache 被清空 (`empty vector, deleting`),
    # 所以拿 `result OK` 当"上传成功"会把"云端副本被删"误判成"已有副本".
    "delete_ok_file": re.compile(
        r"\[AppID\s+(\d+)\]\s+Delete OK for file\s+(.+?)\s*$"
    ),
    "upload_result_ok": re.compile(
        r"\[AppID\s+(\d+)\]\s+Upload complete,\s*result OK"
    ),
}


@dataclass
class CloudLogEntry:
    """单个 appid 的云日志聚合结果."""

    appid: str
    watching_files: list[str] = field(default_factory=list)
    watching_count: int | None = None       # 日志自报的监视文件数
    disabled_count: int = 0
    enabled_count: int = 0
    on_launch_count: int = 0
    on_exit_count: int = 0
    incomplete_sync_count: int = 0
    change_numbers: list[tuple[int, int]] = field(default_factory=list)
    quota_seen: bool = False

    # ---- Auto-Cloud 规则引擎 (自动云游戏的真相源) ----
    # 每条: {"index": int, "root": str, "path": str, "pattern": str, "recursive": bool,
    #        "hits": [{"count": int, "glob": str}, ...]}
    rules: list[dict[str, Any]] = field(default_factory=list)
    # 真正被 Persisting 到云的文件 (绝对路径)
    persisted_files: list[str] = field(default_factory=list)
    # 云端相对路径 (来自 "Need to upload file <rel>")
    cloud_rel_paths: list[str] = field(default_factory=list)
    # 已存在云端且未改动而被跳过的 (云端相对路径)
    skipped_unmodified: list[str] = field(default_factory=list)
    # 云端已完成往返时记录到的 change number
    synced_change_number: int | None = None

    # ---- 上传**真正发起**的证据 (2026-10-05 实测确证) ----
    # `Upload batch initiated, ID: <id>, ChangeNumber: <n>` 是 Steam 向服务器
    # 提交上传批次的行. 它比 `remotetime` 更早可见: 前者说明"已经动手传",
    # 后者说明"服务器已收下". 只有 API 型 / 已启用云同步的 app 才会打印.
    upload_batches: list[tuple[int, int]] = field(default_factory=list)   # (batch_id, change_number)
    upload_complete_count: int = 0    # `Upload complete in build list` 出现次数
    # ⚠️ 上面的 `upload_complete_count` 只是 build-list 语句, **下载路径也会打印**,
    # 不能当上传成功证据. 下列三项才是"文件级上传/删除结果":
    uploaded_files: list[str] = field(default_factory=list)   # `Upload OK for file <path>`
    deleted_files: list[str] = field(default_factory=list)    # `Delete OK for file <path>`
    upload_result_ok_count: int = 0   # `Upload complete, result OK` 出现次数

    # 行内序证据: `testappcloudpaths` 生效**不会**打印 "AutoCloud is enabled",
    # 只会让 `AutoCloud is disabled. Run "..."` 从此消失, 同时开始出现规则评估.
    # 所以必须记住这两类行的**最后出现位置**, 用先后关系判定开关是否真的点上了.
    disabled_last_line: int = -1
    active_last_line: int = -1
    toggle_effective: bool = False      # 单文件内判定; 多文件合并时取"或"

    # ---- 派生结论 ----

    @property
    def rule_patterns(self) -> list[str]:
        """规则里声明的文件模式 (去重, 保序)."""
        out: list[str] = []
        for r in self.rules:
            pat = r.get("pattern")
            if pat and pat not in out:
                out.append(pat)
        return out

    @property
    def rule_file_names(self) -> list[str]:
        """按规则推出的**云端文件集** (云相对路径的 basename, 去重)."""
        names: list[str] = []
        for rel in self.cloud_rel_paths:
            base = rel.rsplit("/", 1)[-1]
            if base and base not in names:
                names.append(base)
        for abs_ in self.persisted_files:
            base = abs_.replace("\\", "/").rsplit("/", 1)[-1]
            if base and base not in names:
                names.append(base)
        return names

    @property
    def declared_file_set(self) -> list[str]:
        """被同步文件集的**统一视图**: 优先 Watching file, 退回规则/落盘证据."""
        if self.watching_files:
            return list(self.watching_files)
        return list(self.persisted_files)

    @property
    def has_rule_evidence(self) -> bool:
        """观察到过 Auto-Cloud 规则评估 —— 证明该 appid 真走自动云."""
        return bool(self.rules)

    @property
    def cloud_roundtrip_ok(self) -> bool:
        """**日志层面**观察到过 `Successfully synced to ChangeNumber`.

        ⚠️ 局限 (两轮实测):
        - 它是**通用同步完成语句**, 描述"当前状态已对齐到 CN N", **不是**"这批上传成功".
          上传路径里它出现在 `Upload batch initiated` **之前** (实测 3590 / 105600 /
          219740 / 431960):
              Need to upload file X
              Successfully synced to ChangeNumber 16      <- 当前状态到 16
              Upload batch initiated, ..., ChangeNumber: 17   <- 新批次目标是 17
        - Auto-Cloud 型 (DS3) 的上传路径**从不打印**它; 下载方向与 Steam 客户端自身
          (AppID 7) 也大量出现.

        结论: 本字段**不能**用来判断某个文件/某个 appid 是否上传成功.
        判"有没有动手传"用 `upload_initiated`; 判"云端有没有副本"用
        `remotecache.RemoteCache.upload_verified` (即 `remotetime > 0`).
        本字段保留, 仅表达"日志里确实见到了这句同步语句"这一较弱事实.
        """
        return self.synced_change_number is not None

    @property
    def queued_not_uploaded(self) -> bool:
        """本地已入队但日志层面**没看到云往返** —— 仅在真出现 `Persisting file ... to the cloud` 时成立.

        不能用 `Need to upload file` 判定: 那行在 ISteamRemoteStorage 型游戏里也会出现,
        会把"API 云正常但日志没打 ChangeNumber"的游戏误报成未上传.

        ⚠️ 对 Auto-Cloud 上传方向无判别力 (见 `cloud_roundtrip_ok` 的说明):
        本机对照 241100 也会命中 True, 而它其实早已上云. 最终结论以
        `remotecache` 的 `remotetime` 为准 (由 machine_probe 覆盖该字段).
        """
        return bool(self.persisted_files) and not self.cloud_roundtrip_ok

    @property
    def upload_initiated(self) -> bool:
        """日志里见到过 `Upload batch initiated` —— **上传真的动手了**.

        这是比 `remotetime` 更早可见的证据, 且**跨机制通用**:
        - API 型 / 已启用云同步的 app → 会打印
        - 云同步被禁用的 app → 从不打印 (即使文件已 Persisting 到本地缓存)

        2026-10-05 实测: 同一台机器上, 账号 987654321 的 730 (CS2) 有 8 次
        `Upload batch initiated, ..., ChangeNumber: 10/11`; 而账号 1234567890
        在两个日志文件覆盖期内**一次都没有** ⇒ 该账号的游戏云存上传已停止工作.

        **2026-10-11 已解除**: 开启账号级云同步后同轮出现 11 个游戏 appid 的
        `Upload batch initiated`; DS3 (374320) 亦于 21:33:12 首次出现.
        """
        return bool(self.upload_batches)

    @property
    def upload_confirmed(self) -> bool:
        """有**文件级**上传成功记录 (`Upload OK for file <path>`).

        区别三件容易混的事:
          - `Upload batch initiated`   → 动手传了 (可能失败)
          - `Upload OK for file <p>`   → **该文件真的传上去了** ✅
          - `Delete OK for file <p>`   → 云端那份被**删掉**了 (相反语义) ⚠️
          - `Upload complete, result OK` → 整批结束, 但**删除批**也会打印,
            单看它会把"云端副本被删"误判成"已有副本"

        实测 PEAK (3527290): `Need to delete file ...quicksave.peak` →
        `Delete OK for file ...` → `Upload complete, result OK` →
        `empty vector, deleting remotecache.vdf`. 它**没有**云端副本, 且
        `upload_result_ok_count > 0` —— 所以判据必须落在 `uploaded_files` 上.
        """
        return bool(self.uploaded_files)

    @property
    def cloud_deleted(self) -> bool:
        """云端副本被删除过 (`Delete OK for file <path>`)."""
        return bool(self.deleted_files)

    @property
    def last_upload_change_number(self) -> int | None:
        """最后一次上传批次携带的 change number."""
        return self.upload_batches[-1][1] if self.upload_batches else None

    @property
    def local_cn(self) -> int | None:
        """最后一次记录的本地 change number."""
        return self.change_numbers[-1][0] if self.change_numbers else None

    @property
    def global_cn(self) -> int | None:
        """最后一次记录的云端 change number."""
        return self.change_numbers[-1][1] if self.change_numbers else None

    @property
    def change_numbers_all_zero(self) -> bool:
        """所有 change number 记录都是 0 -> 0."""
        if not self.change_numbers:
            return False
        return all(l == 0 and g == 0 for l, g in self.change_numbers)

    @property
    def ever_advanced(self) -> bool:
        """观察到过任一侧非 0 的 change number."""
        return any(l > 0 or g > 0 for l, g in self.change_numbers)

    @property
    def never_synced(self) -> bool:
        """**强证据**判定: 云开关被禁用 + 两侧 change number 全为 0.

        只说"全 0"不够 —— 日志会轮转, 早期记录可能已丢失. 因此必须叠加
        `AutoCloud is disabled` 才下这个结论, 否则降级为 `no_change_advance`.
        """
        return self.change_numbers_all_zero and self.cloud_state == "disabled"

    @property
    def no_change_advance(self) -> bool:
        """**弱提示**: 两侧 change number 全为 0, 但没有明确的禁用记录.

        可能原因: 日志已轮转 (更早的同步记录被覆盖) / 云端确实为空.
        因此措辞必须是"未观察到", 不能断言"从未同步".
        """
        return self.change_numbers_all_zero and self.cloud_state != "disabled"

    @property
    def sync_direction(self) -> str:
        """本地相对云端的落后/领先关系."""
        if not self.change_numbers:
            return "unknown"
        l, g = self.change_numbers[-1]
        if l == 0 and g == 0:
            return "both-zero"
        if l < g:
            return "local-behind"      # 需要从云端下拉
        if l > g:
            return "local-ahead"       # 有本地变更待上传
        return "in-sync"

    @property
    def cloud_state(self) -> str:
        """云开关状态判定.

        `toggle_effective` 会推翻 `disabled`: `testappcloudpaths` 生效后
        `AutoCloud is disabled` 那行不再打印, 但日志里**旧的禁用行永远留在文件中**,
        只数出现次数会把已点亮的游戏一直报成"禁用".
        """
        if self.toggle_effective and not self.enabled_count:
            return "enabled"
        if self.disabled_count and not self.enabled_count:
            return "disabled"
        if self.enabled_count and not self.disabled_count:
            return "enabled"
        if self.disabled_count and self.enabled_count:
            return "mixed"
        return "unknown"

    @property
    def mechanism(self) -> str:
        """机制推断: 有 rule / watching / 开关记录 = Auto-Cloud; 否则未知."""
        if self.rules or self.watching_files:
            return "auto-cloud"
        if self.disabled_count or self.enabled_count or self.on_launch_count:
            return "auto-cloud"
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "mechanism": self.mechanism,
            "cloud_state": self.cloud_state,
            "toggle_effective": self.toggle_effective,
            "watching_files": self.watching_files,
            "watching_count_reported": self.watching_count,
            "rules": self.rules,
            "rule_patterns": self.rule_patterns,
            "declared_file_set": self.declared_file_set,
            "rule_file_names": self.rule_file_names,
            "persisted_files": self.persisted_files,
            "cloud_rel_paths": self.cloud_rel_paths,
            "skipped_unmodified": self.skipped_unmodified,
            "synced_change_number": self.synced_change_number,
            "upload_initiated": self.upload_initiated,
            "upload_batches": [list(b) for b in self.upload_batches],
            "last_upload_change_number": self.last_upload_change_number,
            "upload_complete_count": self.upload_complete_count,
            "uploaded_files": self.uploaded_files,
            "deleted_files": self.deleted_files,
            "upload_result_ok_count": self.upload_result_ok_count,
            "upload_confirmed": self.upload_confirmed,
            "cloud_deleted": self.cloud_deleted,
            "cloud_roundtrip_ok": self.cloud_roundtrip_ok,
            "queued_not_uploaded": self.queued_not_uploaded,
            "never_synced": self.never_synced,
            "no_change_advance": self.no_change_advance,
            "ever_advanced": self.ever_advanced,
            "sync_direction": self.sync_direction,
            "local_change_number": self.local_cn,
            "global_change_number": self.global_cn,
            "incomplete_sync_count": self.incomplete_sync_count,
            "disabled_count": self.disabled_count,
            "enabled_count": self.enabled_count,
            "on_launch_count": self.on_launch_count,
            "on_exit_count": self.on_exit_count,
        }


@dataclass
class CloudLog:
    """一次日志解析的总体结果."""

    files_read: list[str] = field(default_factory=list)
    lines_total: int = 0
    entries: dict[str, CloudLogEntry] = field(default_factory=dict)
    unknown_app_lines: int = 0

    @property
    def disabled_appids(self) -> list[str]:
        return sorted(a for a, e in self.entries.items() if e.cloud_state == "disabled")

    @property
    def never_synced_appids(self) -> list[str]:
        return sorted(a for a, e in self.entries.items() if e.never_synced)

    @property
    def no_change_advance_appids(self) -> list[str]:
        return sorted(a for a, e in self.entries.items() if e.no_change_advance)

    @property
    def incomplete_appids(self) -> list[str]:
        return sorted(a for a, e in self.entries.items() if e.incomplete_sync_count)

    @property
    def testcmd_appids(self) -> list[str]:
        return sorted(self._testcmd_hits)

    _testcmd_hits: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_read": self.files_read,
            "lines_total": self.lines_total,
            "appid_count": len(self.entries),
            "disabled_appids": self.disabled_appids,
            "never_synced_appids": self.never_synced_appids,
            "no_change_advance_appids": self.no_change_advance_appids,
            "incomplete_sync_appids": self.incomplete_appids,
            "testappcloudpaths_seen": self.testcmd_appids,
            "entries": {a: e.to_dict() for a, e in sorted(self.entries.items())},
        }


def _entry(log: CloudLog, appid: str) -> CloudLogEntry:
    e = log.entries.get(appid)
    if e is None:
        e = CloudLogEntry(appid=appid)
        log.entries[appid] = e
    return e


def _append_unique(target: list[str], value: str) -> None:
    if value and value not in target:
        target.append(value)


def parse_cloud_log(path: str | Path) -> CloudLog:
    """解析单个 cloud_log 文件 (只读)."""
    log = CloudLog()
    p = Path(path)
    if not p.is_file():
        return log
    log.files_read.append(str(p))

    text = p.read_text(encoding="utf-8", errors="replace")
    watching: dict[str, set[str]] = {}
    watching_last: dict[str, int] = {}

    for lineno, line in enumerate(text.splitlines()):
        if not line:
            continue
        log.lines_total += 1
        if "AppID" not in line and "testappcloudpaths" not in line:
            continue

        m = _PATTERNS["watching"].search(line)
        if m:
            watching.setdefault(m.group(1), set()).add(m.group(2).strip())
            watching_last[m.group(1)] = lineno
            continue

        m = _PATTERNS["watching_count"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.watching_count = int(m.group(2))
            e.active_last_line = lineno
            continue

        m = _PATTERNS["rule"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.active_last_line = lineno
            e.rules.append(
                {
                    "index": int(m.group(2)),
                    "root": m.group(3),
                    "path": m.group(4),
                    "pattern": m.group(5),
                    "recursive": bool(m.group("recursive")),
                    "hits": [],
                }
            )
            continue

        m = _PATTERNS["rule_found"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.active_last_line = lineno
            hit = {"count": int(m.group(2)), "glob": m.group(3).strip()}
            if e.rules:
                e.rules[-1]["hits"].append(hit)
            else:
                # 规则行被日志轮转切掉, 仍保留命中证据 (降级为无规则头)
                e.rules.append(
                    {"index": None, "root": None, "path": None,
                     "pattern": None, "recursive": False, "hits": [hit]}
                )
            continue

        m = _PATTERNS["persisting"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.active_last_line = lineno
            _append_unique(e.persisted_files, m.group(2).strip())
            continue

        m = _PATTERNS["skipping"].search(line)
        if m:
            _append_unique(_entry(log, m.group(1)).skipped_unmodified, m.group(2).strip())
            continue

        m = _PATTERNS["need_upload"].search(line)
        if m:
            _append_unique(_entry(log, m.group(1)).cloud_rel_paths, m.group(2).strip())
            continue

        m = _PATTERNS["synced_cn"].search(line)
        if m:
            _entry(log, m.group(1)).synced_change_number = int(m.group(2))
            continue

        m = _PATTERNS["already_synced"].search(line)
        if m:
            e = _entry(log, m.group(1))
            if e.synced_change_number is None:
                e.synced_change_number = int(m.group(2))
            continue

        m = _PATTERNS["disabled"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.disabled_count += 1
            e.disabled_last_line = lineno
            continue

        m = _PATTERNS["enabled"].search(line)
        if m:
            _entry(log, m.group(1)).enabled_count += 1
            continue

        m = _PATTERNS["on_launch"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.on_launch_count += 1
            e.active_last_line = lineno
            continue

        m = _PATTERNS["on_exit"].search(line)
        if m:
            e = _entry(log, m.group(1))
            e.on_exit_count += 1
            e.active_last_line = lineno
            continue

        m = _PATTERNS["incomplete"].search(line)
        if m:
            _entry(log, m.group(1)).incomplete_sync_count += 1
            continue

        m = _PATTERNS["change_numbers"].search(line)
        if m:
            _entry(log, m.group(1)).change_numbers.append((int(m.group(2)), int(m.group(3))))
            continue

        m = _PATTERNS["quota"].search(line)
        if m:
            _entry(log, m.group(1)).quota_seen = True
            continue

        m = _PATTERNS["upload_batch"].search(line)
        if m:
            _entry(log, m.group(1)).upload_batches.append(
                (int(m.group(2)), int(m.group(3)))
            )
            continue

        m = _PATTERNS["upload_complete"].search(line)
        if m:
            _entry(log, m.group(1)).upload_complete_count += 1
            continue

        m = _PATTERNS["upload_ok_file"].search(line)
        if m:
            _append_unique(_entry(log, m.group(1)).uploaded_files, m.group(2).strip())
            continue

        m = _PATTERNS["delete_ok_file"].search(line)
        if m:
            _append_unique(_entry(log, m.group(1)).deleted_files, m.group(2).strip())
            continue

        m = _PATTERNS["upload_result_ok"].search(line)
        if m:
            _entry(log, m.group(1)).upload_result_ok_count += 1
            continue

        m = _PATTERNS["testcmd"].search(line)
        if m:
            log._testcmd_hits.add(m.group(1))
            continue

        if _APPID_RE.search(line):
            log.unknown_app_lines += 1

    for appid, files in watching.items():
        e = _entry(log, appid)
        e.watching_files = sorted(files)
        # `Watching file` 是"开关已生效"的**直接证据**: 禁用期 Steam 只打印
        # `AutoCloud is disabled` 而**不会**进入 `Running AutoCloud on launch/exit`
        # 的文件枚举, 因此不会有 Watching 行. 用行号并入 active_last_line,
        # 让 toggle_effective 能推翻文件里残留的旧 `AutoCloud is disabled` 行.
        if appid in watching_last:
            e.active_last_line = max(e.active_last_line, watching_last[appid])

    for e in log.entries.values():
        # 单文件内: 规则/落盘/监视/进出游戏行出现在最后一条禁用行之后
        #   => testappcloudpaths 已生效.
        # 只在单文件内比较 —— 跨文件的相对顺序不可靠, 合并时改为取"或".
        e.toggle_effective = (
            e.active_last_line >= 0 and e.active_last_line > e.disabled_last_line
        )
    return log


def parse_cloud_logs(paths: Iterable[str | Path]) -> CloudLog:
    """解析多个 cloud_log 文件并合并 (Steam 会轮转: cloud_log.txt / .previous.txt)."""
    merged = CloudLog()
    for path in paths:
        part = parse_cloud_log(path)
        merged.files_read.extend(part.files_read)
        merged.lines_total += part.lines_total
        merged.unknown_app_lines += part.unknown_app_lines
        merged._testcmd_hits |= part._testcmd_hits
        for appid, e in part.entries.items():
            tgt = _entry(merged, appid)
            seen = set(tgt.watching_files)
            tgt.watching_files.extend(f for f in e.watching_files if f not in seen)
            if e.watching_count is not None:
                tgt.watching_count = e.watching_count
            tgt.disabled_count += e.disabled_count
            tgt.enabled_count += e.enabled_count
            tgt.on_launch_count += e.on_launch_count
            tgt.on_exit_count += e.on_exit_count
            tgt.incomplete_sync_count += e.incomplete_sync_count
            tgt.change_numbers.extend(e.change_numbers)
            tgt.quota_seen = tgt.quota_seen or e.quota_seen
            tgt.toggle_effective = tgt.toggle_effective or e.toggle_effective
            tgt.upload_batches.extend(
                b for b in e.upload_batches if b not in tgt.upload_batches
            )
            tgt.upload_complete_count += e.upload_complete_count
            tgt.upload_result_ok_count += e.upload_result_ok_count
            for f in e.uploaded_files:
                _append_unique(tgt.uploaded_files, f)
            for f in e.deleted_files:
                _append_unique(tgt.deleted_files, f)

            existing_rules = {
                (r.get("index"), r.get("root"), r.get("path"), r.get("pattern"))
                for r in tgt.rules
            }
            for r in e.rules:
                key = (r.get("index"), r.get("root"), r.get("path"), r.get("pattern"))
                if key in existing_rules:
                    for t in tgt.rules:
                        if (
                            t.get("index"), t.get("root"), t.get("path"), t.get("pattern")
                        ) == key:
                            for hit in r.get("hits") or []:
                                if hit not in t["hits"]:
                                    t["hits"].append(hit)
                            break
                else:
                    tgt.rules.append(dict(r))
                    existing_rules.add(key)

            for f in e.persisted_files:
                _append_unique(tgt.persisted_files, f)
            for f in e.cloud_rel_paths:
                _append_unique(tgt.cloud_rel_paths, f)
            for f in e.skipped_unmodified:
                _append_unique(tgt.skipped_unmodified, f)
            if e.synced_change_number is not None:
                cur = tgt.synced_change_number
                tgt.synced_change_number = (
                    e.synced_change_number if cur is None
                    else max(cur, e.synced_change_number)
                )
    # 变更号按文件顺序合并, 但多文件时顺序不严格 —— 排序保证 deterministic 输出
    for e in merged.entries.values():
        e.watching_files = sorted(set(e.watching_files))
    return merged
