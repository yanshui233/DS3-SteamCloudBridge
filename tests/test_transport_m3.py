#!/usr/bin/env python3
"""M3 (云通道守卫 / 快照回滚 / 上传校验) 的回归测试.

重点锁住四件实测踩出来的事:

1. **云状态必须按 Steam 会话判** —— `cloud_log` 的全文件统计会被旧会话的
   `Watching file` 行骗过 (实测: `cloud_state=enabled` 而本会话其实没点亮).
   这是本文件里最重要的一条回归 (见 `test_full_file_stats_is_fooled_but_session_is_not`).
2. **未点亮时 Steam 连 `AutoCloud is disabled` 都不打** —— 本会话"零活动"本身就是信号.
3. **回滚绝不能靠删文件** —— 实测删本地会被云端灌回来; 只有"快照时目标不存在"才允许删.
4. **上传会静默失败** —— `Upload Timeout` 且没有 `HTTP upload ... beginning`, 必须重试.

全部不接触真机数据: 要么是纯文本/临时目录, 要么是 `make_fake_steam.py` 造的假环境.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import make_fake_steam as mfs  # noqa: E402
from src.steam import cloud_log, discovery  # noqa: E402
from src.transport import cloudchannel as cc  # noqa: E402
from src.transport import guard, snapshot, verify  # noqa: E402
from src.transport.target import SaveTarget  # noqa: E402

DS3 = "374320"

# ---------------------------------------------------------------- 日志素材

OLD_SESSION = [
    '[2026-10-11 20:48:50] [AppID 374320] AutoCloud is disabled. Run "testappcloudpaths 374320" in the console to enable it',
    "[2026-10-12 01:23:07] Client version: 1788652215",
    "[2026-10-12 01:24:14] [AppID 374320] Starting sync (AC Launch,down,)",
    "[2026-10-12 01:24:14] [AppID 374320]     Evaluating rule 0 with root=\"WinAppDataRoaming\" path=\"DarkSoulsIII\" pattern=\"*.sl2\" (recursive)",
    "[2026-10-12 01:24:14] [AppID 374320]     Watching file C:\\Users\\x\\AppData\\Roaming\\DarkSoulsIII\\01100001499602d2\\DS30000.sl2",
]

NEW_SESSION_QUIET = [
    "[2026-10-12 11:37:24] Client version: 1788652215",
    "[2026-10-12 11:37:28] [AppID 7] Starting sync (eval,)",
    "[2026-10-12 11:37:28] [AppID 7] Eval complete",
    "[2026-10-12 11:37:28] [AppID 241100] Starting sync (eval,)",
    "[2026-10-12 11:37:28] [AppID 241100] Eval complete",
]


def _write(lines: list[str]) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
    )
    tmp.write("\n".join(lines) + "\n")
    tmp.close()
    return Path(tmp.name)


# ================================================================ 云通道


class TestDetectChannel(unittest.TestCase):
    def test_active_evidence_means_lit(self) -> None:
        st = cc.detect_channel(DS3, OLD_SESSION)
        self.assertTrue(st.lit)
        self.assertFalse(st.needs_enable)
        self.assertEqual(st.session_started_at, "2026-10-12 01:23:07")
        self.assertGreater(len(st.active_lines), 0)

    def test_explicit_disabled_means_unlit(self) -> None:
        lines = [
            "[2026-10-12 01:23:07] Client version: 1788652215",
            '[2026-10-12 01:23:30] [AppID 374320] AutoCloud is disabled. Run "testappcloudpaths 374320"',
        ]
        st = cc.detect_channel(DS3, lines)
        self.assertFalse(st.lit)
        self.assertIn("AutoCloud is disabled", st.reason)

    def test_zero_activity_in_session_means_unlit(self) -> None:
        """未点亮时 Steam 把该 appid 整个跳过 —— 连 disabled 行都不打."""
        st = cc.detect_channel(DS3, NEW_SESSION_QUIET)
        self.assertFalse(st.lit)
        self.assertTrue(st.eval_completed)
        self.assertEqual(st.active_lines, [])
        self.assertEqual(st.disabled_lines, [])
        self.assertIn("零活动", st.reason)
        self.assertIn("testappcloudpaths 374320", st.hint)

    def test_no_session_marker_and_no_activity_is_unknown(self) -> None:
        st = cc.detect_channel(DS3, ["[2026-10-12 01:00:00] [AppID 7] Starting sync (init,)"])
        self.assertIsNone(st.lit)
        self.assertFalse(st.session_marker_found)

    def test_no_session_marker_but_activity_still_lit(self) -> None:
        st = cc.detect_channel(DS3, OLD_SESSION[2:])  # 去掉 Client version 行
        self.assertTrue(st.lit)
        self.assertFalse(st.session_marker_found)

    def test_full_file_stats_is_fooled_but_session_is_not(self) -> None:
        """★ 核心回归: 全文件统计判 enabled, 会话感知判 unlit.

        这正是 M3 要堵的洞 —— 沿用 `cloud_state` 会把**没点亮**当成**已点亮**放行,
        结果文件搬进去了、云却根本不同步.
        """
        lines = OLD_SESSION + NEW_SESSION_QUIET
        path = _write(lines)
        self.addCleanup(os.unlink, path)

        # 旧判定 (全文件统计): 被旧会话的 Watching 行骗过
        entry = cloud_log.parse_cloud_log(path).entries[DS3]
        self.assertEqual(entry.cloud_state, "enabled")  # 假阳性

        # 新判定 (会话感知): 正确
        st = cc.detect_channel(DS3, lines)
        self.assertFalse(st.lit)
        self.assertEqual(st.session_started_at, "2026-10-12 11:37:24")

    def test_missing_file_is_unknown(self) -> None:
        st = cc.detect_channel_from_file(DS3, Path("C:/definitely/not/here.txt"))
        self.assertIsNone(st.lit)
        self.assertIn("日志不存在", st.reason)


class TestDisabledFlavourIsNotActivity(unittest.TestCase):
    """★ 2026-10-13 朋友机实测回归: 禁用态被读成"已点亮", 于是**跳过点亮**、整轮白跑.

    真凶: 旧 `ACTIVE_MARKERS` 把 `Starting sync` / `Running AutoCloud` / `Watching file` /
    `AutoCloud complete` 都当"进过云流程"的证据, 还注着"禁用期不会出现任何一个" ——
    **那句是错的**. 第一组是朋友机回传包里的 6 行**原样**日志 (同一时刻 Steam 正打印
    `AutoCloud is disabled`), 第二组是本仓 `cases/local-4b-2026-10-12` 基线日志里的
    禁用态 burst. 两组都必须判未点亮; 最后一组是同一天的真实启用态, 不许误伤.
    """

    FRIEND_RETURNED = [
        "[2026-10-13 20:03:29] [AppID 374320] Starting sync (AC Launch,Sync Disabled,)",
        "[2026-10-13 20:03:29] [AppID 374320] AutoCloud checking local state for user 1234567890",
        "[2026-10-13 20:03:29] [AppID 374320] Need to sync from local change number '0' "
        "to global change number '11' (full), but not attempting now",
        "[2026-10-13 20:03:29] [AppID 374320] Need to download file "
        "DarkSoulsIII/01100001499602d2/DS30000.sl2",
        "[2026-10-13 20:03:29] [AppID 374320] AutoCloud is disabled. Run "
        '"testappcloudpaths 374320" in the console to enable it',
        "[2026-10-13 20:03:30] [AppID 374320] YldWriteCacheDirectoryToFile - saved to "
        "'C:\\Program Files (x86)\\Steam/userdata/1234567890/374320/remotecache.vdf'",
    ]

    BASELINE_DISABLED = [
        "[2026-10-11 14:16:40] [AppID 632470] Starting sync (AC Launch,Sync Disabled,)",
        "[2026-10-11 14:16:40] [AppID 632470] AutoCloud checking local state for user 1234567890",
        "[2026-10-11 14:16:40] [AppID 632470] Need to sync from local change number '0' "
        "to global change number '0' (full), but not attempting now",
        "[2026-10-11 14:16:40] [AppID 632470] Need to upload file "
        "ZAUM Studio/Disco Elysium/Settings/Settings.json",
        "[2026-10-11 14:16:40] [AppID 632470] Running AutoCloud on launch. "
        "Looking for existing files to watch:",
        "[2026-10-11 14:16:40] [AppID 632470]     Watching file "
        "C:\\Users\\x\\AppData\\LocalLow\\ZAUM Studio\\Disco Elysium\\Settings\\Settings.json",
        "[2026-10-11 14:16:40] [AppID 632470] AutoCloud done. Watching 1 files",
    ]

    def test_friend_machine_burst_is_unlit(self) -> None:
        st = cc.detect_channel(DS3, self.FRIEND_RETURNED)
        self.assertFalse(st.lit, "禁用态被读成已点亮 ⇒ 会跳过点亮, 整轮白跑")
        self.assertTrue(st.needs_enable)
        self.assertEqual(st.active_lines, [], "禁用态不该产生任何活动证据")
        self.assertEqual(len(st.disabled_lines), 2)
        self.assertIn("AutoCloud is disabled", st.reason)

    def test_baseline_disabled_burst_is_unlit(self) -> None:
        st = cc.detect_channel("632470", self.BASELINE_DISABLED)
        self.assertFalse(st.lit, "枚举行两态通吃 ⇒ 禁用 burst 被当成活动")
        self.assertEqual(st.active_lines, [])
        self.assertEqual(len(st.disabled_lines), 1)

    def test_enumeration_lines_alone_are_neutral(self) -> None:
        """`Running AutoCloud` / `Watching file` / `AutoCloud done` / `Persisting file`
        两态都会打 ⇒ 一律不算证据."""
        lines = [
            "[2026-10-11 14:16:40] Client version: 1788652215",
            "[2026-10-11 14:16:40] [AppID 632470] Running AutoCloud on launch. "
            "Looking for existing files to watch:",
            "[2026-10-11 14:16:40] [AppID 632470]     Watching file C:\\x\\Settings.json",
            "[2026-10-11 14:16:40] [AppID 632470] AutoCloud done. Watching 1 files",
            "[2026-10-11 14:16:40] [AppID 632470] Persisting file "
            "C:\\x\\Settings.json to the cloud",
        ]
        st = cc.detect_channel("632470", lines)
        self.assertEqual(st.active_lines, [])
        self.assertFalse(st.lit)

    def test_init_eval_alone_is_neutral(self) -> None:
        """Steam 启动时对每个 AutoCloud 游戏都跑一遍 init/eval, 与开关无关."""
        lines = [
            "[2026-10-13 18:28:51] Client version: 1788652215",
            "[2026-10-13 18:28:51] [AppID 374320] Starting sync (init,eval,)",
            "[2026-10-13 18:28:51] [AppID 374320] Eval complete",
        ]
        st = cc.detect_channel(DS3, lines)
        self.assertEqual(st.active_lines, [])
        self.assertFalse(st.lit)

    def test_lit_burst_is_not_harmed(self) -> None:
        """真实启用态 burst (带 `down` + 真传输行) 必须仍判已点亮."""
        lines = [
            "[2026-10-11 21:08:57] Client version: 1788652215",
            "[2026-10-11 21:08:57] [AppID 374320] Starting sync (AC Launch,down,)",
            "[2026-10-11 21:08:58] [AppID 374320] Attempting to sync from local change "
            "number '0' to global change number '0' (full)",
            "[2026-10-11 21:08:58] [AppID 374320] Successfully synced to ChangeNumber 0",
            "[2026-10-11 21:08:58] [AppID 374320] Running AutoCloud on launch. "
            "Looking for existing files to watch:",
        ]
        st = cc.detect_channel(DS3, lines)
        self.assertTrue(st.lit)
        self.assertIn("活动记录", st.reason)

    def test_later_lightup_beats_earlier_disabled(self) -> None:
        """会话中途点亮: 更晚的证据说了算 (回执晚于禁用行 ⇒ 已点亮)."""
        lines = self.BASELINE_DISABLED + [
            "[2026-10-11 14:30:00] [AppID 632470] AutoCloud is disabled. Run "
            '"testappcloudpaths 632470"',
        ]
        st = cc.detect_channel(
            "632470", lines,
            console_lines=['[2026-10-11 14:40:00] "testappcloudpaths" = "632470"'],
        )
        self.assertTrue(st.lit)

    def test_disabled_later_beats_earlier_lit(self) -> None:
        """反过来也要有确定结论: 禁用行更晚 ⇒ 未点亮."""
        lines = [
            "[2026-10-11 21:08:57] Client version: 1788652215",
            "[2026-10-11 21:08:57] [AppID 374320] Starting sync (AC Launch,down,)",
            "[2026-10-11 21:20:00] [AppID 374320] Starting sync (AC Launch,Sync Disabled,)",
        ]
        st = cc.detect_channel(DS3, lines)
        self.assertFalse(st.lit)

    def test_sync_markers_tokens(self) -> None:
        self.assertEqual(
            cc.sync_markers("[t] [AppID 1] Starting sync (AC Launch,down,)"),
            ["AC Launch", "down"],
        )
        self.assertEqual(
            cc.sync_markers(
                "[t] [AppID 1] Starting sync (init,eval,AC Launch,Sync Disabled,)"
            ),
            ["init", "eval", "AC Launch", "Sync Disabled"],
        )
        self.assertEqual(cc.sync_markers("[t] [AppID 1] Need to upload file x"), [])


class TestSessionStaleBoundary(unittest.TestCase):
    """`Client version:` 是**懒惰**的: Steam 重启后 `cloud_log` 不一定立刻写新边界.

    实测 (2026-10-12 14:12 本机): `steam.exe -shutdown` 重启后 `cloud_log.txt` 零增长,
    最后一条 `Client version:` 仍是**上一会话**的 11:37:24. 没有兜底边界时, 上一会话的
    "已点亮"会被当成本会话的 -> **假阳性** (`testappcloudpaths` 重启即失效).
    兜底 = `steam.exe` 的进程启动时间 (`src/steam/processes.py`).
    """

    @staticmethod
    def _epoch(s: str) -> float:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))

    def test_restart_after_boundary_makes_session_stale(self) -> None:
        st = cc.detect_channel(
            DS3, OLD_SESSION, steam_start_epoch=self._epoch("2026-10-12 11:37:24")
        )
        self.assertTrue(st.session_stale)
        self.assertFalse(st.lit)
        self.assertEqual(st.active_lines, [])
        self.assertIn("已经过去", st.reason)

    def test_fresh_boundary_is_not_stale(self) -> None:
        st = cc.detect_channel(
            DS3, NEW_SESSION_QUIET, steam_start_epoch=self._epoch("2026-10-12 11:30:00")
        )
        self.assertFalse(st.session_stale)
        self.assertFalse(st.lit)  # 仍是零活动 -> 未点亮

    def test_console_ack_before_steam_start_is_ignored(self) -> None:
        cloud = _write(OLD_SESSION)
        cons = _write(['[2026-10-12 13:32:59] "testappcloudpaths" = "374320"'])
        st = cc.detect_channel_from_files(
            DS3, cloud, cons, steam_start_epoch=self._epoch("2026-10-12 14:12:00")
        )
        self.assertFalse(st.lit)
        self.assertEqual(st.console_ack_line, "")
        self.assertTrue(st.session_stale)

    def test_console_ack_after_steam_start_is_honored(self) -> None:
        """重启后**重新点亮**: 回执晚于 steam 启动 -> 算本会话, 判已点亮."""
        cloud = _write(OLD_SESSION)
        cons = _write(['[2026-10-12 14:20:00] "testappcloudpaths" = "374320"'])
        st = cc.detect_channel_from_files(
            DS3, cloud, cons, steam_start_epoch=self._epoch("2026-10-12 14:12:00")
        )
        self.assertTrue(st.lit)
        self.assertIn("testappcloudpaths", st.console_ack_line)


class TestConsoleCommands(unittest.TestCase):
    def test_command_builders(self) -> None:
        self.assertEqual(cc.command_enable(DS3), f"testappcloudpaths {DS3}")
        self.assertEqual(cc.command_sync_up(DS3), f"cloud_sync_up {DS3}")
        self.assertEqual(cc.command_sync_down(DS3), f"cloud_sync_down {DS3}")
        self.assertEqual(cc.command_sync_app(DS3), f"cloud_sync_app {DS3}")


class TestConsoleAck(unittest.TestCase):
    def test_quoted_echo_form(self) -> None:
        lines = ['[2026-10-12 01:19:34] "testappcloudpaths" = "374320"']
        ack = cc.console_ack("testappcloudpaths", DS3, lines)
        self.assertTrue(ack.found)
        self.assertTrue(ack.in_session)  # 没有 Console Log Start, 视为当前

    def test_variable_listing_form(self) -> None:
        """`find cloud` 输出的变量列表形态 (实测) —— 必须也能认."""
        lines = ['[2026-10-12 01:34:02] testappcloudpaths = "374320" - ']
        ack = cc.console_ack("testappcloudpaths", DS3, lines)
        self.assertTrue(ack.found)
        self.assertEqual(cc.console_var_value("testappcloudpaths", lines), "374320")

    def test_not_in_current_console_session(self) -> None:
        lines = [
            '[2026-10-12 01:34:02] testappcloudpaths = "374320" - ',
            "[2026-10-12 11:37:20] Console Log Start",
        ]
        ack = cc.console_ack("testappcloudpaths", DS3, lines)
        self.assertTrue(ack.found)
        self.assertFalse(ack.in_session)  # 回执属于**上一个**会话

    def test_wrong_value_not_matched(self) -> None:
        lines = ['[2026-10-12 11:40:00] testappcloudpaths = "999" - ']
        ack = cc.console_ack("testappcloudpaths", DS3, lines)
        self.assertFalse(ack.found)

    def test_var_value_missing(self) -> None:
        self.assertIsNone(cc.console_var_value("testappcloudpaths", ["nothing here"]))


class TestConsoleLogEvidence(unittest.TestCase):
    """点亮命令只写 `console_log`, **不写** `cloud_log` (2026-10-12 真机实测).

    只看 cloud_log 会把"刚 `testappcloudpaths` 点亮、还没跑过同步"判成未点亮,
    于是 `sync` 错拒 (实测踩到, 必须修). 回执行形态:

        [2026-10-12 13:32:59] "testappcloudpaths" = "374320"
    """

    def _files(self, *, cloud_lines, console_lines):
        d = Path(tempfile.mkdtemp())
        c = d / "cloud_log.txt"
        c.write_text("\n".join(cloud_lines) + "\n", encoding="utf-8")
        k = d / "console_log.txt"
        k.write_text("\n".join(console_lines) + "\n", encoding="utf-8")
        return c, k

    _CLOUD = [
        "[2026-10-12 11:37:24] Client version: 1234567890",
        "[2026-10-12 11:37:30] [AppID 1281930] Starting sync (eval,)",
    ]

    def test_console_ack_alone_means_lit(self) -> None:
        c, k = self._files(
            cloud_lines=self._CLOUD,
            console_lines=[
                "[2026-10-12 11:37:20] Console Log Start",
                '[2026-10-12 13:32:59] "testappcloudpaths" = "374320"',
            ],
        )
        st = cc.detect_channel_from_files(DS3, c, k)
        self.assertTrue(st.lit)
        self.assertFalse(st.needs_enable)
        self.assertIn("testappcloudpaths", st.console_ack_line)
        self.assertEqual(st.active_lines, [])  # cloud_log 确实没有活动
        self.assertIn("控制台回执", st.reason)

    def test_without_console_log_it_still_says_unlit(self) -> None:
        """不传 console_log 时保持旧行为 —— 不能凭空变亮."""
        c, _k = self._files(
            cloud_lines=self._CLOUD,
            console_lines=['[2026-10-12 13:32:59] "testappcloudpaths" = "374320"'],
        )
        st = cc.detect_channel_from_file(DS3, c)
        self.assertFalse(st.lit)

    def test_ack_from_previous_console_session_ignored(self) -> None:
        c, k = self._files(
            cloud_lines=self._CLOUD,
            console_lines=[
                '[2026-10-12 09:00:00] "testappcloudpaths" = "374320"',
                "[2026-10-12 11:37:20] Console Log Start",
            ],
        )
        st = cc.detect_channel_from_files(DS3, c, k)
        self.assertFalse(st.lit)
        self.assertEqual(st.console_ack_line, "")  # 记了行但不算本会话

    def test_ack_for_other_appid_ignored(self) -> None:
        c, k = self._files(
            cloud_lines=self._CLOUD,
            console_lines=[
                "[2026-10-12 11:37:20] Console Log Start",
                '[2026-10-12 13:32:59] "testappcloudpaths" = "1245620"',
            ],
        )
        st = cc.detect_channel_from_files(DS3, c, k)
        self.assertFalse(st.lit)

    def test_cloud_log_activity_still_wins(self) -> None:
        c, k = self._files(
            cloud_lines=[
                "[2026-10-12 11:37:24] Client version: 1234567890",
                f"[2026-10-12 13:46:08] [AppID {DS3}] Starting sync (up,AC Exit,)",
            ],
            console_lines=["[2026-10-12 11:37:20] Console Log Start"],
        )
        st = cc.detect_channel_from_files(DS3, c, k)
        self.assertTrue(st.lit)
        self.assertIn("活动记录", st.reason)


# ================================================================ 快照 / 回滚


def _target(path: Path, *, remote_sha: str = "", remotetime: int = 0) -> SaveTarget:
    return SaveTarget(
        appid=DS3,
        rel_path=f"DarkSoulsIII/01100001499602d2/{path.name}",
        abs_path=path,
        root_enum="4",
        root_label="%APPDATA%",
        confidence="confirmed-by-cloud-log",
        remote_sha=remote_sha,
        remote_size=path.stat().st_size if path.is_file() else 0,
        remotetime=remotetime,
    )


class TestSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scb-snap-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.save = self.tmp / "save" / "DS30000.sl2"
        self.save.parent.mkdir(parents=True)
        self.save.write_bytes(b"ORIGINAL-SAVE-DATA" * 10)
        self.copy_dir = self.tmp / "copies"

    def test_take_snapshot_existing(self) -> None:
        snap = snapshot.take_snapshot(_target(self.save), appid=DS3, copy_dir=self.copy_dir)
        self.assertTrue(snap.exists)
        self.assertEqual(snap.size, self.save.stat().st_size)
        self.assertTrue(snap.sha1)
        self.assertTrue(snap.has_copy)

    def test_take_snapshot_missing_file(self) -> None:
        gone = self.tmp / "save" / "new.sl2"
        snap = snapshot.take_snapshot(_target(gone), appid=DS3, copy_dir=self.copy_dir)
        self.assertFalse(snap.exists)
        self.assertFalse(snap.has_copy)

    def test_save_load_roundtrip(self) -> None:
        snap = snapshot.take_snapshot(_target(self.save), appid=DS3, copy_dir=self.copy_dir)
        p = snapshot.save_snapshot(snap, self.tmp / "s.json")
        again = snapshot.load_snapshot(p)
        self.assertEqual(again.to_dict(), snap.to_dict())

    def test_take_snapshot_requires_abs_path(self) -> None:
        t = SaveTarget(
            appid=DS3, rel_path="x", abs_path=None, root_enum="?", root_label="?", confidence="unresolved"
        )
        with self.assertRaises(ValueError):
            snapshot.take_snapshot(t, appid=DS3)

    def test_restore_dry_run_touches_nothing(self) -> None:
        snap = snapshot.take_snapshot(_target(self.save), appid=DS3, copy_dir=self.copy_dir)
        self.save.write_bytes(b"CHANGED-BY-TRANSFER")
        before = self.save.read_bytes()

        res = snapshot.restore_snapshot(snap, apply=False)
        self.assertTrue(res.dry_run)
        self.assertFalse(res.restored)
        self.assertEqual(self.save.read_bytes(), before)  # 一个字节都没动

    def test_restore_writes_content_back_and_refreshes_mtime(self) -> None:
        snap = snapshot.take_snapshot(_target(self.save), appid=DS3, copy_dir=self.copy_dir)
        original_sha = snap.sha1

        time.sleep(0.01)
        os.utime(self.save, (1000, 1000))  # 旧 mtime
        self.save.write_bytes(b"WRONG-SAVE-FROM-OTHER-MACHINE")

        res = snapshot.restore_snapshot(snap, apply=True)
        self.assertTrue(res.restored)
        self.assertTrue(res.verified)
        self.assertEqual(res.sha1_after, original_sha)
        # 必须刷新 mtime —— 否则 Steam 会判 `Skipping un-modified file`
        self.assertGreater(self.save.stat().st_mtime, 1000)
        self.assertFalse(res.removed)

    def test_restore_removes_when_snapshot_saw_no_file(self) -> None:
        """快照时目标不存在 -> 回滚走删除路径, 并**明确提示**必须触发上行."""
        gone = self.tmp / "save" / "added.sl2"
        snap = snapshot.take_snapshot(_target(gone), appid=DS3, copy_dir=self.copy_dir)
        gone.write_bytes(b"NEWLY-ADDED")  # 模拟搬运新建

        res = snapshot.restore_snapshot(snap, apply=True)
        self.assertTrue(res.removed)
        self.assertFalse(gone.exists())
        self.assertTrue(any("cloud_sync_up" in a for a in res.actions))

    def test_restore_without_copy_errors_instead_of_overwriting(self) -> None:
        snap = snapshot.take_snapshot(_target(self.save), appid=DS3, copy=False)
        self.assertFalse(snap.has_copy)
        res = snapshot.restore_snapshot(snap, apply=True)
        self.assertFalse(res.restored)
        self.assertIn("没有可用副本", res.error)

    def test_restore_of_existing_never_deletes(self) -> None:
        """红线: 快照时目标存在, 回滚**只能写回**, 绝不许删."""
        snap = snapshot.take_snapshot(_target(self.save), appid=DS3, copy_dir=self.copy_dir)
        res = snapshot.restore_snapshot(snap, apply=True)
        self.assertTrue(self.save.exists())
        self.assertFalse(res.removed)


# ================================================================ 上传校验


class TestCheckUpload(unittest.TestCase):
    REL = "DarkSoulsIII/01100001499602d2/DS30000.sl2"

    def test_upload_ok(self) -> None:
        lines = [
            "[2026-10-12 01:31:06] Client version: 1",
            f"[2026-10-12 01:31:07] [AppID {DS3}] HTTP upload for file '{self.REL}' beginning . . .",
            f"[2026-10-12 01:31:07] [AppID {DS3}] Upload OK for file {self.REL}",
        ]
        chk = verify.check_upload(DS3, self.REL, lines, remote_remotetime=1791737449)
        self.assertEqual(chk.verdict, verify.VERDICT_OK)
        self.assertTrue(chk.uploaded_ok)
        self.assertTrue(chk.http_started)

    def test_timeout_without_http_beginning(self) -> None:
        """实测静默失败形态: 有 Timeout, **没有** HTTP beginning."""
        lines = [
            "[2026-10-12 01:25:54] Client version: 1",
            f"[2026-10-12 01:25:54] [AppID {DS3}] Upload batch initiated, ChangeNumber: 2",
            f"[2026-10-12 01:26:14] [AppID {DS3}] Upload Timeout for file {self.REL}",
            f"[2026-10-12 01:26:15] [AppID {DS3}] Upload complete, result Timeout",
        ]
        chk = verify.check_upload(DS3, self.REL, lines)
        self.assertEqual(chk.verdict, verify.VERDICT_TIMEOUT)
        self.assertFalse(chk.http_started)
        self.assertEqual(chk.timeouts, 1)

    def test_skipped_unmodified_with_remote_copy_is_in_sync(self) -> None:
        lines = [
            "[2026-10-12 11:00:00] Client version: 1",
            f"[2026-10-12 11:00:01] [AppID {DS3}]\tSkipping un-modified file {self.REL}",
        ]
        chk = verify.check_upload(DS3, self.REL, lines, remote_remotetime=123)
        self.assertEqual(chk.verdict, verify.VERDICT_IN_SYNC)

    def test_session_scoping_ignores_old_success(self) -> None:
        """★ 旧会话的成功记录不能当成本次成功."""
        lines = [
            f"[2026-10-11 21:33:12] [AppID {DS3}] Upload OK for file {self.REL}",
            "[2026-10-12 11:37:24] Client version: 1788652215",
        ]
        scoped = verify.check_upload(DS3, self.REL, lines, session_scoped=True)
        self.assertEqual(scoped.verdict, verify.VERDICT_NONE)
        unscoped = verify.check_upload(DS3, self.REL, lines, session_scoped=False)
        self.assertTrue(unscoped.uploaded_ok)

    def test_forget_is_not_upload(self) -> None:
        lines = [
            "[2026-10-12 01:34:36] Client version: 1",
            f"[2026-10-12 01:34:37] [AppID {DS3}] Need to forget file {self.REL}",
            f"[2026-10-12 01:34:39] [AppID {DS3}] Forget OK for file {self.REL}",
        ]
        chk = verify.check_upload(DS3, self.REL, lines)
        self.assertEqual(chk.verdict, verify.VERDICT_FORGOTTEN)
        self.assertFalse(chk.uploaded_ok)

    def test_removing_from_cloud_recognized(self) -> None:
        lines = [
            "[2026-10-12 01:34:36] Client version: 1",
            f"[2026-10-12 01:34:36] [AppID {DS3}]     File "
            f"C:\\Users\\x\\AppData\\Roaming\\{self.REL.replace('/', chr(92))} "
            f"no longer matches any patterns (2). Removing from cloud",
        ]
        chk = verify.check_upload(DS3, self.REL, lines)
        self.assertEqual(chk.removed_from_cloud, 1)

    def test_backslash_path_still_matches(self) -> None:
        rel_win = self.REL.replace("/", "\\")
        lines = [
            "[2026-10-12 01:31:00] Client version: 1",
            f"[2026-10-12 01:31:07] [AppID {DS3}] Upload OK for file {rel_win}",
        ]
        self.assertEqual(verify.check_upload(DS3, rel_win, lines).verdict, verify.VERDICT_OK)

    def test_norm_rel(self) -> None:
        self.assertEqual(verify.norm_rel("a\\b\\c.sl2"), "a/b/c.sl2")
        self.assertEqual(verify.norm_rel("/a/b/"), "a/b")


class TestSyncAndVerify(unittest.TestCase):
    def test_succeeds_first_attempt(self) -> None:
        sent: list[str] = []
        chk = verify.UploadCheck(appid=DS3, rel_path="a/b.sl2")
        chk.verdict, chk.uploaded_ok = verify.VERDICT_OK, True

        out = verify.sync_and_verify(
            DS3, "a/b.sl2",
            send=lambda c: sent.append(c) or True,
            check=lambda: chk,
            settle_seconds=0,
        )
        self.assertTrue(out.succeeded)
        self.assertEqual(out.attempts, 1)
        self.assertEqual(sent, [f"cloud_sync_up {DS3}"])

    def test_retries_after_timeout_then_succeeds(self) -> None:
        """实测: 超时后重试即成功 —— 这正是 M3 需要重试的原因."""
        seq = [
            verify.UploadCheck(appid=DS3, rel_path="a/b.sl2", verdict=verify.VERDICT_TIMEOUT),
            verify.UploadCheck(appid=DS3, rel_path="a/b.sl2", verdict=verify.VERDICT_TIMEOUT),
            verify.UploadCheck(
                appid=DS3, rel_path="a/b.sl2", verdict=verify.VERDICT_OK, uploaded_ok=True
            ),
        ]
        calls = {"n": 0}

        def _check() -> verify.UploadCheck:
            r = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return r

        out = verify.sync_and_verify(
            DS3, "a/b.sl2", send=lambda c: True, check=_check, settle_seconds=0
        )
        self.assertTrue(out.succeeded)
        self.assertEqual(out.attempts, 3)

    def test_exhausts_attempts(self) -> None:
        chk = verify.UploadCheck(appid=DS3, rel_path="a/b.sl2", verdict=verify.VERDICT_TIMEOUT)
        out = verify.sync_and_verify(
            DS3, "a/b.sl2", send=lambda c: True, check=lambda: chk,
            max_attempts=2, settle_seconds=0,
        )
        self.assertFalse(out.succeeded)
        self.assertEqual(out.attempts, 2)
        self.assertIn("未确认上传成功", out.error)

    def test_in_sync_counts_as_success(self) -> None:
        chk = verify.UploadCheck(
            appid=DS3, rel_path="a/b.sl2",
            verdict=verify.VERDICT_IN_SYNC, remote_remotetime=999,
        )
        out = verify.sync_and_verify(
            DS3, "a/b.sl2", send=lambda c: True, check=lambda: chk, settle_seconds=0
        )
        self.assertTrue(out.succeeded)


# ================================================================ 守卫接线


class TestChannelGuard(unittest.TestCase):
    def test_unlit_channel_warns(self) -> None:
        st = cc.detect_channel(DS3, NEW_SESSION_QUIET)
        gs = guard._guard_channel(st, DS3)
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0].code, "CLOUD_CHANNEL_UNLIT")
        self.assertFalse(gs[0].blocking)  # 不阻断 —— 搬运本身仍安全
        self.assertIn(f"testappcloudpaths {DS3}", gs[0].remedy)

    def test_lit_channel_is_silent(self) -> None:
        st = cc.detect_channel(DS3, OLD_SESSION)
        self.assertEqual(guard._guard_channel(st, DS3), [])

    def test_unknown_channel_warns(self) -> None:
        st = cc.detect_channel(DS3, ["[2026-10-12 01:00:00] noise"])
        gs = guard._guard_channel(st, DS3)
        self.assertEqual(gs[0].code, "CLOUD_CHANNEL_UNKNOWN")

    def test_no_channel_passed_keeps_m2_behaviour(self) -> None:
        self.assertEqual(guard._guard_channel(None, DS3), [])


class TestChannelGuardInFakeEnv(unittest.TestCase):
    """在假 Steam 环境里跑完整 `check_all`, 确认新守卫接得上."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scb-m3-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.manifest = mfs.build(
            self.tmp / "env", "synced", mfs.DEFAULT_ACCOUNT_ID, 8, force=False
        )
        env = mock.patch.dict(os.environ, self.manifest["env"])
        env.start()
        self.addCleanup(env.stop)
        self.install = discovery.SteamInstall(
            root=Path(self.manifest["steam_root"]), source="test"
        )

    def test_check_all_surfaces_unlit_channel(self) -> None:
        """假环境跑通整条守卫链: 新会话无 DS3 活动 -> 报告 CLOUD_CHANNEL_UNLIT."""
        from src.transport import target as target_mod

        # 假环境的日志里 DS3 是有活动的 (synced 场景) -> 先确认它被认成"点亮"
        log_path = self.install.logs / "cloud_log.txt"
        self.assertTrue(cc.detect_channel_from_file(DS3, log_path).lit)

        # 追加一条更新的会话边界: 模拟"Steam 重启了, 本会话还没点亮"
        log_path.write_text(
            log_path.read_text(encoding="utf-8")
            + "\n[2026-10-12 11:37:24] Client version: 1788652215\n"
            + "[2026-10-12 11:37:28] [AppID 7] Starting sync (eval,)\n"
            + "[2026-10-12 11:37:28] [AppID 7] Eval complete\n",
            encoding="utf-8",
        )
        st = cc.detect_channel_from_file(DS3, log_path)
        self.assertFalse(st.lit)

        cloud = cloud_log.parse_cloud_log(log_path)
        res = target_mod.resolve_targets(
            self.install, DS3, mfs.DEFAULT_ACCOUNT_ID, cloud=cloud
        )

        # 源文件另存一份, 免得撞上 SAME_FILE 守卫
        src = self.tmp / "incoming.sl2"
        src.write_bytes(Path(self.manifest["save_file"]).read_bytes())

        gs = guard.check_all(
            source=src,
            target=res.primary,
            resolution=res,
            accountid=mfs.DEFAULT_ACCOUNT_ID,
            steam_is_running=False,
            channel=st,
        )
        codes = [g.code for g in gs]
        self.assertIn("CLOUD_CHANNEL_UNLIT", codes)
        self.assertNotIn("STEAM_RUNNING", codes)
        self.assertNotIn("SAME_FILE", codes)

        # 不给 channel 时, 这条守卫不出现 (M2 行为不被改变)
        gs2 = guard.check_all(
            source=src,
            target=res.primary,
            resolution=res,
            accountid=mfs.DEFAULT_ACCOUNT_ID,
            steam_is_running=False,
        )
        self.assertNotIn("CLOUD_CHANNEL_UNLIT", [g.code for g in gs2])


if __name__ == "__main__":
    unittest.main()
