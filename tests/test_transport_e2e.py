#!/usr/bin/env python3
"""搬运器 (M2) 的隔离端到端测试: **走 CLI 入口**, 在假 Steam 环境里跑完整流程.

与 `test_transport.py` 的分工:

* `test_transport.py` 直接调 Python API, 覆盖分支与边界.
* 本文件跑 `tools/bridge.py` 子进程, 覆盖"参数解析 -> 环境准备 -> 打印 -> 落盘"
  这条真实链路 —— 单元测试很容易漏掉 CLI 层的接错线.

全程用假环境 + 环境变量重定向, **不接触任何真机 Steam 数据**.
`SCB_STEAM_RUNNING=0` 用于把"Steam 是否在运行"这一项钉死 (隔离环境里没法让真机
Steam 停下), 它只影响这一项检查.
`SCB_STEAM_START_EPOCH` 同理钉死"steam.exe 启动时间"这个兜底会话边界 (`0` = 关掉);
个别用例会显式设成一个值来验证"重启后不许假报已点亮".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import make_fake_steam as mfs  # noqa: E402

BRIDGE = PROJECT_ROOT / "tools" / "bridge.py"
DS3 = "374320"


class BridgeCLIBase(unittest.TestCase):
    scenario = "synced"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scb-bridge-e2e-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.manifest = mfs.build(
            self.tmp / "env", self.scenario, mfs.DEFAULT_ACCOUNT_ID, 8, force=False
        )
        self.env = dict(os.environ)
        self.env.update(self.manifest["env"])
        self.env["SCB_STEAM_RUNNING"] = "0"
        # 会话边界兜底也钉死: 假环境里的 cloud_log 会话时间与真机 Steam 启动时间无关.
        # "0" = 关掉兜底, 退回纯日志判定 (与本次修复前的行为一致).
        self.env["SCB_STEAM_START_EPOCH"] = "0"
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.save = Path(self.manifest["save_file"])

    def run_cli(self, *args: str, expect_rc: int | None = None):
        proc = subprocess.run(
            [sys.executable, str(BRIDGE), *args],
            capture_output=True,
            timeout=180,
            env=self.env,
        )
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        if expect_rc is not None:
            self.assertEqual(
                proc.returncode,
                expect_rc,
                f"rc={proc.returncode}\n--- STDOUT ---\n{out}\n--- STDERR ---\n{err}",
            )
        return proc.returncode, out, err

    def base_args(self) -> list[str]:
        return [
            "--steam-root",
            self.manifest["steam_root"],
            "--account",
            str(mfs.DEFAULT_ACCOUNT_ID),
        ]

    def make_source(self, name: str, payload: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(payload)
        return p


class TestBridgeStatus(BridgeCLIBase):
    def test_status_reports_confirmed_location(self) -> None:
        _, out, _ = self.run_cli(*self.base_args(), "status", "--appid", DS3, expect_rc=0)
        self.assertIn("confirmed-by-cloud-log", out)
        self.assertIn(self.manifest["hex_dir"], out)
        self.assertIn(self.manifest["save_sha1"], out)

    def test_status_json_is_valid(self) -> None:
        out_json = self.tmp / "status.json"
        self.run_cli(
            *self.base_args(), "status", "--appid", DS3, "--json", str(out_json), expect_rc=0
        )
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(data["status"]["resolved"])
        self.assertEqual(data["status"]["appid"], DS3)


class TestBridgePlan(BridgeCLIBase):
    def test_plan_does_not_touch_disk(self) -> None:
        """dry-run 是默认行为 —— 跑完 plan 存档必须一字节没变."""
        before = self.save.read_bytes()
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        _, out, _ = self.run_cli(
            *self.base_args(), "plan", "--appid", DS3, "--source", str(src), expect_rc=0
        )
        self.assertIn("copy", out)
        self.assertEqual(self.save.read_bytes(), before)
        self.assertEqual(list(self.save.parent.glob("*.scb-bak")), [])

    def test_plan_identical_reports_skip(self) -> None:
        src = self.make_source("same.sl2", self.save.read_bytes())
        _, out, _ = self.run_cli(
            *self.base_args(), "plan", "--appid", DS3, "--source", str(src), expect_rc=0
        )
        self.assertIn("skip-identical", out)

    def test_plan_blocked_when_steam_running(self) -> None:
        self.env["SCB_STEAM_RUNNING"] = "1"
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        rc, out, _ = self.run_cli(
            *self.base_args(), "plan", "--appid", DS3, "--source", str(src)
        )
        self.assertEqual(rc, 2, out)
        self.assertIn("STEAM_RUNNING", out)

    def test_plan_json_written(self) -> None:
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        out_json = self.tmp / "plan.json"
        self.run_cli(
            *self.base_args(),
            "plan",
            "--appid",
            DS3,
            "--source",
            str(src),
            "--json",
            str(out_json),
            expect_rc=0,
        )
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["plan"]["verdict"], "copy")


class TestBridgeApply(BridgeCLIBase):
    def test_apply_requires_yes(self) -> None:
        before = self.save.read_bytes()
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        rc, out, _ = self.run_cli(
            *self.base_args(), "apply", "--appid", DS3, "--source", str(src)
        )
        self.assertEqual(rc, 3, out)
        self.assertIn("--yes", out)
        self.assertEqual(self.save.read_bytes(), before)

    def test_apply_full_chain(self) -> None:
        """完整链路: 备份 -> 写入 -> 校验 -> journal."""
        original = self.save.read_bytes()
        payload = b"SCB-APPLIED\n" + bytes(range(256)) * 3
        src = self.make_source("mod.sl2", payload)
        bak = self.tmp / "bak"
        journal = self.tmp / "journal.jsonl"

        _, out, _ = self.run_cli(
            *self.base_args(),
            "apply",
            "--appid",
            DS3,
            "--source",
            str(src),
            "--backup-dir",
            str(bak),
            "--journal",
            str(journal),
            "--yes",
            expect_rc=0,
        )
        self.assertIn("校验通过", out)

        # 目标已被替换
        self.assertEqual(self.save.read_bytes(), payload)
        # 备份保留原始内容
        backups = list(bak.glob("*.scb-bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        # journal 记录了这次搬运
        records = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines() if x]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["appid"], DS3)
        self.assertTrue(records[0]["verified"])
        # 无临时文件残留
        self.assertEqual(list(self.save.parent.glob("*.scb-tmp")), [])

    def test_apply_blocked_when_steam_running(self) -> None:
        self.env["SCB_STEAM_RUNNING"] = "1"
        before = self.save.read_bytes()
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        rc, out, _ = self.run_cli(
            *self.base_args(),
            "apply",
            "--appid",
            DS3,
            "--source",
            str(src),
            "--backup-dir",
            str(self.tmp / "bak"),
            "--yes",
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.save.read_bytes(), before)

    def test_apply_survives_second_run(self) -> None:
        """幂等性: 同一份源连跑两次, 第二次应报"无需搬运"且不破坏文件."""
        payload = b"SCB-IDEMPOTENT\n" + b"K" * 96
        src = self.make_source("mod.sl2", payload)
        args = (
            *self.base_args(),
            "apply",
            "--appid",
            DS3,
            "--source",
            str(src),
            "--backup-dir",
            str(self.tmp / "bak"),
            "--yes",
        )
        self.run_cli(*args, expect_rc=0)
        first = self.save.read_bytes()
        self.run_cli(*args, expect_rc=0)
        self.assertEqual(self.save.read_bytes(), first)
        self.assertEqual(self.save.read_bytes(), payload)


class TestBridgeDisabledScenario(BridgeCLIBase):
    """云开关关着时: 落点仍能定位 (靠 cloud_log), 但必须给出点亮提示."""

    scenario = "disabled"

    def test_status_warns_about_disabled_cloud(self) -> None:
        _, out, _ = self.run_cli(*self.base_args(), "status", "--appid", DS3, expect_rc=0)
        self.assertIn("testappcloudpaths", out)

    def test_plan_warns_but_proceeds(self) -> None:
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        rc, out, _ = self.run_cli(
            *self.base_args(), "plan", "--appid", DS3, "--source", str(src), expect_rc=0
        )
        self.assertIn("CLOUD_DISABLED", out)


# ---------------------------------------------------------------- M3


class BridgeM3Base(BridgeCLIBase):
    """M3 用: 追加会话边界 / 塞命令钩子."""

    steam_state = "0"

    def setUp(self) -> None:
        super().setUp()
        self.env["SCB_STEAM_RUNNING"] = self.steam_state

    def log_path(self) -> Path:
        return Path(self.manifest["steam_root"]) / "logs" / "cloud_log.txt"

    def append_log(self, *lines: str) -> None:
        p = self.log_path()
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            for ln in lines:
                fh.write(ln + "\n")

    def start_new_session(self) -> None:
        """追加一条 `Client version:` 让之前的活动全部作废 (模拟 Steam 重启)."""
        self.append_log(
            "[2026-10-12 11:37:24] Client version: 1788652215",
            "[2026-10-12 11:37:28] [AppID 7] Starting sync (eval,)",
            "[2026-10-12 11:37:28] [AppID 7] Eval complete",
        )

    def boundary_epoch(self) -> float:
        """`start_new_session()` 写的那条会话边界的本地 epoch."""
        return time.mktime(time.strptime("2026-10-12 11:37:24", "%Y-%m-%d %H:%M:%S"))

    def rel_path(self) -> str:
        return verify_norm(
            f"{mfs.DS3_SAVE_DIRNAME}/{self.manifest['hex_dir']}/{mfs.DS3_SAVE_FILE}"
        )

    def enable_console_hook(self) -> Path:
        fake = self.tmp / "console-sent.txt"
        self.env["SCB_CONSOLE_FAKE"] = str(fake)
        return fake


def verify_norm(s: str) -> str:
    return s.replace("\\", "/")


class TestBridgeChannel(BridgeM3Base):
    def test_reports_lit_when_log_has_activity(self) -> None:
        rc, out, _ = self.run_cli(
            *self.base_args(), "channel", "--appid", DS3, expect_rc=0
        )
        self.assertIn("本会话云通道", out)
        self.assertIn("已点亮", out)

    def test_reports_unlit_after_new_session(self) -> None:
        self.start_new_session()
        rc, out, _ = self.run_cli(
            *self.base_args(), "channel", "--appid", DS3, expect_rc=0
        )
        self.assertIn("未点亮", out)
        self.assertIn("testappcloudpaths 374320", out)

    def test_steam_restart_after_log_boundary_is_not_false_lit(self) -> None:
        """steam.exe 启动**晚于**日志最后一条会话边界 -> 判未点亮, 不许假阳性.

        实测场景 (2026-10-12 14:12): `-shutdown` 重启后 `cloud_log` 没写新的
        `Client version:`, 上一会话的"已点亮"证据还留在文件里. 只靠日志会假报已点亮.
        """
        self.start_new_session()  # 上一会话看起来点亮过
        self.append_log("[2026-10-12 11:38:00] [AppID 374320] Starting sync (up,AC Exit,)")
        self.env["SCB_STEAM_START_EPOCH"] = str(int(self.boundary_epoch()) + 3600)
        rc, out, _ = self.run_cli(
            *self.base_args(), "channel", "--appid", DS3, expect_rc=0
        )
        self.assertIn("未点亮", out)
        self.assertIn("已经过去", out)

    def test_steam_start_before_log_boundary_stays_lit(self) -> None:
        """兜底只在"启动晚于边界"时生效; 早于边界时不该干扰正常判定."""
        self.start_new_session()
        self.append_log("[2026-10-12 11:38:00] [AppID 374320] Starting sync (up,AC Exit,)")
        self.env["SCB_STEAM_START_EPOCH"] = str(int(self.boundary_epoch()) - 3600)
        rc, out, _ = self.run_cli(
            *self.base_args(), "channel", "--appid", DS3, expect_rc=0
        )
        self.assertIn("已点亮", out)

    def test_channel_json_written(self) -> None:
        self.start_new_session()
        js = self.tmp / "ch.json"
        self.run_cli(
            *self.base_args(), "channel", "--appid", DS3, "--json", str(js), expect_rc=0
        )
        data = json.loads(js.read_text(encoding="utf-8"))
        self.assertFalse(data["channel"]["lit"])
        self.assertTrue(data["channel"]["needs_enable"])

    def test_status_includes_channel_block(self) -> None:
        self.start_new_session()
        _, out, _ = self.run_cli(*self.base_args(), "status", "--appid", DS3, expect_rc=0)
        self.assertIn("本会话云通道 (M3)", out)
        # 回归 (D7): 顶层云状态来自**全文件统计**, 必须与会话判定区分开.
        # 否则 `enabled (已观察到开关生效)` 与本会话 `未点亮` 并列出现, 会被读成"现在已点亮".
        self.assertIn("云通道(历史统计)", out)
        self.assertIn("不代表本会话已点亮", out)

    def test_plan_carries_unlit_channel_guard(self) -> None:
        self.start_new_session()
        src = self.make_source("mod.sl2", b"SCB-NEW\n" + b"Z" * 64)
        rc, out, _ = self.run_cli(
            *self.base_args(), "plan", "--appid", DS3, "--source", str(src), expect_rc=0
        )
        self.assertIn("CLOUD_CHANNEL_UNLIT", out)


class TestBridgeSync(BridgeM3Base):
    # 同步是 Steam 的职责 —— 隔离环境里把"Steam 在跑"钉死为真
    steam_state = "1"

    def test_refuses_when_channel_unlit(self) -> None:
        """未点亮时必须**先拦住**, 而且一个命令都不能发出去."""
        self.start_new_session()
        fake = self.enable_console_hook()
        rc, out, _ = self.run_cli(
            *self.base_args(), "sync", "--appid", DS3, expect_rc=4
        )
        self.assertIn("未点亮", out)
        self.assertFalse(fake.exists(), "不该发出任何控制台命令")

    def test_sends_command_and_verifies_when_lit(self) -> None:
        """点亮后: 发 cloud_sync_up, 并确认上传 (假环境日志里有 Upload OK)."""
        self.start_new_session()
        self.append_log(
            f"[2026-10-12 11:40:00] [AppID {DS3}] Starting sync (up,AC Exit,)",
            f"[2026-10-12 11:40:00] [AppID {DS3}] Running AutoCloud on exit. Looking for new and updated files",
            f"[2026-10-12 11:40:01] [AppID {DS3}] Upload batch initiated, ChangeNumber: 9",
            f"[2026-10-12 11:40:02] [AppID {DS3}] Upload OK for file {self.rel_path()}",
        )
        fake = self.enable_console_hook()
        rc, out, _ = self.run_cli(
            *self.base_args(), "sync", "--appid", DS3, "--attempts", "2", "--settle", "0",
            expect_rc=0,
        )
        self.assertIn("上传已确认成功", out)
        self.assertTrue(fake.is_file())
        sent = fake.read_text(encoding="utf-8")
        self.assertIn(f"cloud_sync_up {DS3}", sent)

    def test_force_bypasses_channel_gate(self) -> None:
        self.start_new_session()
        fake = self.enable_console_hook()
        rc, out, _ = self.run_cli(
            *self.base_args(), "sync", "--appid", DS3, "--force", "--attempts", "1",
            "--settle", "0", expect_rc=1,
        )
        # 通道被强制跳过 -> 命令真的发出去了; 但本会话没有上传记录 -> 判定失败
        self.assertTrue(fake.is_file())
        self.assertIn(f"cloud_sync_up {DS3}", fake.read_text(encoding="utf-8"))


class TestBridgeSyncHeal(BridgeM3Base):
    """`sync --enable` / `--start-steam`: 自愈式同步的 CLI 接线."""

    steam_state = "1"

    def test_enable_flag_sends_toggle_before_sync(self) -> None:
        """`--enable` 要先发 `testappcloudpaths`; 通道仍判未点亮时**不许**再发同步命令."""
        self.start_new_session()
        fake = self.enable_console_hook()
        rc, out, _ = self.run_cli(
            *self.base_args(), "sync", "--appid", DS3, "--enable", "--settle", "0",
            "--attempts", "1", expect_rc=4,
        )
        sent = fake.read_text(encoding="utf-8") if fake.is_file() else ""
        self.assertIn(f"testappcloudpaths {DS3}", sent)
        self.assertNotIn(f"cloud_sync_up {DS3}", sent, "未点亮时不该发同步命令")
        self.assertIn("点亮", out)

    def test_start_steam_flag_reports_launcher_failure(self) -> None:
        """假环境里 steam_root 没有 steam.exe -> 拉起失败要如实报, 而不是假装成功."""
        self.env["SCB_STEAM_RUNNING"] = "0"
        self.start_new_session()
        fake = self.enable_console_hook()
        rc, out, _ = self.run_cli(
            *self.base_args(), "sync", "--appid", DS3, "--start-steam",
            "--steam-wait", "0", "--settle", "0", expect_rc=3,
        )
        self.assertFalse(fake.exists(), "没起来就不该发任何命令")
        self.assertIn("steam.exe", out)

    def test_sync_still_needs_steam_without_start_flag(self) -> None:
        self.env["SCB_STEAM_RUNNING"] = "0"
        rc, out, _ = self.run_cli(
            *self.base_args(), "sync", "--appid", DS3, "--settle", "0", expect_rc=3,
        )
        self.assertIn("Steam 未运行", out)


class TestBridgeApplyThenSync(BridgeM3Base):
    """`apply --then-sync`: 写完即触发云上行 (M3 trigger-sync)."""

    steam_state = "0"

    def _apply(self, *extra: str, expect_rc: int):
        src = self.make_source("incoming.sl2", b"SCB-IMPORTED\n" + b"Q" * 128)
        rc, out, err = self.run_cli(
            *self.base_args(),
            "apply", "--appid", DS3, "--source", str(src),
            "--backup-dir", str(self.tmp / "bak"),
            "--snapshot-dir", str(self.tmp / "snaps"),
            *extra, "--yes",
            expect_rc=expect_rc,
        )
        return rc, out, err, src

    def test_banner_printed_and_no_command_sent_when_start_forbidden(self) -> None:
        self.start_new_session()
        fake = self.enable_console_hook()
        _rc, out, _err, _src = self._apply(
            "--then-sync", "--no-start-steam", expect_rc=3
        )
        self.assertIn("[trigger-sync] 已启用", out)
        self.assertIn("── trigger-sync", out)
        self.assertIn("--no-start-steam", out)
        self.assertFalse(fake.exists(), "Steam 没在跑时不该发出任何控制台命令")

    def test_apply_still_writes_when_trigger_cannot_start_steam(self) -> None:
        """trigger 失败**不掩盖**搬运本身: 文件要真的落盘, 快照要真的留下."""
        _rc, out, _err, src = self._apply(
            "--then-sync", "--steam-wait", "0", "--sync-settle", "0", expect_rc=3
        )
        self.assertEqual(self.save.read_bytes(), src.read_bytes(), "搬运必须已生效")
        self.assertEqual(len(list((self.tmp / "snaps").glob("*.snapshot.json"))), 1)
        self.assertIn("校验通过", out)
        self.assertIn("[trigger-sync] 已启用", out)
        # 备份也要真的落了 (覆盖前的那一份)
        self.assertTrue(list((self.tmp / "bak").glob("*.scb-bak")))

    def test_trigger_skipped_when_plan_blocked(self) -> None:
        """Steam 在跑 -> plan 被 STEAM_RUNNING 拦住 -> trigger 一步都不能走."""
        self.env["SCB_STEAM_RUNNING"] = "1"
        before = self.save.read_bytes()
        _rc, out, _err, _src = self._apply("--then-sync", expect_rc=1)
        self.assertIn("STEAM_RUNNING", out)
        self.assertNotIn("── trigger-sync", out)
        self.assertEqual(self.save.read_bytes(), before, "被拦下时不许改文件")

    def test_without_then_sync_behaviour_unchanged(self) -> None:
        """不加 `--then-sync` 时, apply 仍是"只搬运, 不上云", 只提示下一步."""
        _rc, out, _err, src = self._apply(expect_rc=0)
        self.assertEqual(self.save.read_bytes(), src.read_bytes())
        self.assertNotIn("── trigger-sync", out)
        self.assertIn("下一步", out)


class TestBridgeRollback(BridgeM3Base):
    def _apply_with_snapshot(self) -> Path:
        src = self.make_source("incoming.sl2", b"SCB-IMPORTED-SAVE\n" + b"Q" * 128)
        snap_dir = self.tmp / "snaps"
        self.run_cli(
            *self.base_args(),
            "apply", "--appid", DS3, "--source", str(src),
            "--backup-dir", str(self.tmp / "bak"),
            "--snapshot-dir", str(snap_dir),
            "--yes",
            expect_rc=0,
        )
        snaps = list(snap_dir.glob("*.snapshot.json"))
        self.assertEqual(len(snaps), 1, f"应恰好产出一份快照: {snaps}")
        return snaps[0]

    def test_apply_writes_snapshot(self) -> None:
        snap = self._apply_with_snapshot()
        data = json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(data["appid"], DS3)
        self.assertTrue(data["exists"])
        self.assertEqual(data["sha1"], self.manifest["save_sha1"])

    def test_rollback_dry_run_touches_nothing(self) -> None:
        snap = self._apply_with_snapshot()
        after_apply = self.save.read_bytes()
        self.assertNotEqual(after_apply, b"ORIGINAL")  # apply 确实改了

        _, out, _ = self.run_cli(
            *self.base_args(), "rollback", "--snapshot", str(snap), expect_rc=0
        )
        self.assertIn("[dry-run]", out)
        self.assertEqual(self.save.read_bytes(), after_apply)

    def test_rollback_restores_original_content(self) -> None:
        snap = self._apply_with_snapshot()
        self.run_cli(
            *self.base_args(), "rollback", "--snapshot", str(snap), "--yes", expect_rc=0
        )
        import hashlib

        now = hashlib.sha1(self.save.read_bytes()).hexdigest()
        self.assertEqual(now, self.manifest["save_sha1"])
        self.assertTrue(self.save.exists())

    # ---- 删除分支 (快照时目标不存在): 真机场景 = "本机从未玩过该游戏" ----

    def _apply_onto_fresh_machine(self) -> Path:
        """造"本机没有该存档"的形态 -> apply 是**新建**, 快照应记 exists=False."""
        self.save.unlink()
        self.assertFalse(self.save.exists())
        src = self.make_source("incoming.sl2", b"SCB-IMPORTED-SAVE\n" + b"Z" * 128)
        snap_dir = self.tmp / "snaps"
        self.run_cli(
            *self.base_args(),
            "apply", "--appid", DS3, "--source", str(src),
            "--backup-dir", str(self.tmp / "bak"),
            "--snapshot-dir", str(snap_dir),
            "--yes",
            expect_rc=0,
        )
        snaps = list(snap_dir.glob("*.snapshot.json"))
        self.assertEqual(len(snaps), 1, f"应恰好产出一份快照: {snaps}")
        return snaps[0]

    def test_snapshot_records_absent_target_as_not_existing(self) -> None:
        snap = self._apply_onto_fresh_machine()
        data = json.loads(snap.read_text(encoding="utf-8"))
        self.assertFalse(data["exists"], "新建场景的快照必须记 exists=False")
        self.assertEqual(data["copy_path"], "", "没有原件就不该有备份副本")
        self.assertTrue(self.save.is_file(), "apply 应把文件建出来")

    def test_rollback_delete_branch_dry_run_touches_nothing(self) -> None:
        snap = self._apply_onto_fresh_machine()
        _, out, _ = self.run_cli(
            *self.base_args(), "rollback", "--snapshot", str(snap), expect_rc=0
        )
        self.assertIn("快照时目标不存在", out)
        self.assertIn("[dry-run]", out)
        self.assertTrue(self.save.is_file(), "dry-run 不许删任何东西")

    def test_rollback_deletes_newly_created_target(self) -> None:
        snap = self._apply_onto_fresh_machine()
        _, out, _ = self.run_cli(
            *self.base_args(), "rollback", "--snapshot", str(snap), "--yes", expect_rc=0
        )
        self.assertFalse(self.save.exists(), "回滚应把 apply 新建的文件删掉")
        # 删除路径必须**显式**提醒紧接着触发上行 (否则下次启动会被云端灌回来)
        self.assertIn("cloud_sync_up", out)
        self.assertIn("中间不要启动游戏", out)

    def test_rollback_delete_branch_reports_removed_in_json(self) -> None:
        snap = self._apply_onto_fresh_machine()
        out_json = self.tmp / "rollback.json"
        self.run_cli(
            *self.base_args(),
            "rollback", "--snapshot", str(snap), "--yes", "--json", str(out_json),
            expect_rc=0,
        )
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(payload["rollback"]["removed"])
        self.assertTrue(payload["rollback"]["restored"])


if __name__ == "__main__":
    unittest.main()
