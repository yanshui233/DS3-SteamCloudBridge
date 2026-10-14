#!/usr/bin/env python3
"""`trigger-sync` 编排 (`src/transport/trigger.py`) 的单元测试.

全部外部动作都是**注入的假实现** —— 不碰真机 Steam, 也不碰真实文件系统:

* `steam_running_fn`  —— 可控的"Steam 在不在跑"序列 (测试"从关到开"的转变)
* `start_steam_fn`    —— 记录"有没有尝试启动"
* `detect_channel_fn` —— 可控的通道点亮状态 (支持"点亮前/点亮后"两次不同结果)
* `enable_fn`         —— 记录"有没有试图点亮"
* `send` / `check`    —— 记录发出去的命令 / 返回预设的判定
* `sha1_fn`           —— 返回预设 sha, 用来模拟"文件被云端覆盖"
* `sleep`             —— 全零, 让等待逻辑可断言而不真的睡

另外覆盖 `src/steam/launch.py` 的纯逻辑部分 (路径解析 / 投递失败 / 轮询超时).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.steam import launch as launch_mod  # noqa: E402
from src.transport import trigger, verify  # noqa: E402

APPID = "374320"
REL = "DarkSoulsIII/01100001499602d2/DS30000.sl2"


# ---------------------------------------------------------------- 假件


class FakeChannel:
    def __init__(self, lit: bool | None) -> None:
        self.lit = lit


class Recorder:
    """记录被调用过的动作."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.started: int = 0
        self.enabled: int = 0
        self.slept: list[float] = []


def ok_check() -> verify.UploadCheck:
    c = verify.UploadCheck(appid=APPID, rel_path=REL)
    c.uploaded_ok = True
    c.verdict = verify.VERDICT_OK
    c.remote_remotetime = 1791792450
    return c


def dead_check() -> verify.UploadCheck:
    c = verify.UploadCheck(appid=APPID, rel_path=REL)
    c.verdict = verify.VERDICT_NONE
    return c


class Harness:
    """把一次 `trigger_sync` 需要的全部注入点打包."""

    def __init__(
        self,
        *,
        running: list[bool | None] | bool | None = True,
        channel: list[bool | None] | bool | None = True,
        checks: list[verify.UploadCheck] | None = None,
        sha_after: str = "",
        start_ok: bool = True,
        enable_ok: bool = True,
        expected_sha1: str = "",
        **kw,
    ) -> None:
        self.rec = Recorder()
        self._running = list(running) if isinstance(running, list) else None
        self._running_fixed = None if self._running else running
        self._channel = list(channel) if isinstance(channel, list) else None
        self._channel_fixed = None if self._channel else channel
        self._checks = list(checks) if checks else [ok_check()]
        self._sha_after = sha_after
        self._start_ok = start_ok
        self._enable_ok = enable_ok
        self.kw = kw
        self.expected_sha1 = expected_sha1

    # --- 注入点
    def steam_running(self) -> bool | None:
        if self._running is None:
            return self._running_fixed
        if len(self._running) > 1:
            return self._running.pop(0)
        return self._running[0]

    def start_steam(self) -> bool:
        self.rec.started += 1
        return self._start_ok

    def detect_channel(self) -> FakeChannel:
        if self._channel is None:
            return FakeChannel(self._channel_fixed)
        if len(self._channel) > 1:
            return FakeChannel(self._channel.pop(0))
        return FakeChannel(self._channel[0])

    def enable(self) -> bool:
        self.rec.enabled += 1
        return self._enable_ok

    def send(self, command: str) -> bool:
        self.rec.commands.append(command)
        return True

    def check(self) -> verify.UploadCheck:
        if len(self._checks) > 1:
            return self._checks.pop(0)
        return self._checks[0]

    def sha1(self, _path) -> str:
        return self._sha_after

    def sleep(self, seconds: float) -> None:
        self.rec.slept.append(seconds)

    def run(self, **overrides) -> trigger.TriggerOutcome:
        kw = dict(
            steam_running_fn=self.steam_running,
            start_steam_fn=self.start_steam,
            detect_channel_fn=self.detect_channel,
            enable_fn=self.enable,
            send=self.send,
            check=self.check,
            target_path="/fake/target.sl2",
            expected_sha1=self.expected_sha1,
            sha1_fn=self.sha1,
            sleep=self.sleep,
            steam_wait_seconds=4.0,
            poll_seconds=1.0,
            steam_settle_seconds=0.0,
            settle_seconds=0.0,
        )
        kw.update(self.kw)
        kw.update(overrides)
        return trigger.trigger_sync(APPID, REL, **kw)


# ---------------------------------------------------------------- 用例


class TestSteamLifecycle(unittest.TestCase):
    def test_already_running_skips_start(self) -> None:
        h = Harness(running=True, channel=True, expected_sha1="AAA", sha_after="AAA")
        out = h.run()
        self.assertTrue(out.succeeded)
        self.assertFalse(out.steam_started)
        self.assertEqual(h.rec.started, 0, "Steam 已在跑时不该再拉起")
        self.assertTrue(out.steam_ready)

    def test_starts_steam_when_not_running(self) -> None:
        h = Harness(
            running=[False, False, True],  # 第一次探测: 没在跑; 起来后: 在跑
            channel=[False, True],         # 新会话里通道未点亮 -> 点亮后点亮
            expected_sha1="AAA",
            sha_after="AAA",
        )
        out = h.run()
        self.assertTrue(out.succeeded)
        self.assertTrue(out.steam_started)
        self.assertEqual(h.rec.started, 1)
        self.assertEqual(out.channel_was_lit, False)
        self.assertTrue(out.channel_enabled)
        self.assertEqual(out.channel_lit_after, True)

    def test_refuses_to_start_when_not_allowed(self) -> None:
        h = Harness(running=False, channel=True)
        out = h.run(allow_start_steam=False)
        self.assertFalse(out.succeeded)
        self.assertEqual(out.error_code, trigger.ERROR_STEAM_UNEXPECTED)
        self.assertEqual(h.rec.started, 0, "没获准启动时不能拉起")
        self.assertEqual(h.rec.commands, [], "更不该发任何控制台命令")

    def test_steam_never_appears_times_out(self) -> None:
        h = Harness(running=False, channel=True)
        out = h.run(steam_wait_seconds=3.0, poll_seconds=1.0)
        self.assertFalse(out.succeeded)
        self.assertEqual(out.error_code, trigger.ERROR_STEAM_NOT_STARTED)
        self.assertTrue(out.steam_started, "投递成功了但进程一直没出现")
        self.assertGreaterEqual(out.steam_wait_seconds, 3.0)
        self.assertEqual(h.rec.commands, [])

    def test_unknown_steam_state_proceeds_with_warning(self) -> None:
        """探测不出"在不在跑"时**不擅自拉起、也不拒绝** —— 与旧 `sync` 语义一致."""
        h = Harness(running=None, channel=True)
        out = h.run()
        self.assertTrue(out.succeeded)
        self.assertEqual(h.rec.started, 0, "不确定时不能重复启动")
        self.assertIsNone(out.steam_was_running)
        self.assertIn("无法确认", "\n".join(out.steps))

    def test_start_launcher_raising_is_contained(self) -> None:
        """启动函数抛错不能把整个流程掀翻 —— 要变成一次干净的失败."""

        class Boom(Harness):
            def start_steam(self):
                raise OSError("no launcher")

        h = Boom(running=False, channel=True)
        out = h.run(steam_wait_seconds=1.0, poll_seconds=1.0)
        self.assertFalse(out.succeeded)
        self.assertEqual(out.error_code, trigger.ERROR_STEAM_NOT_STARTED)


class TestOverwriteGuard(unittest.TestCase):
    def test_aborts_when_local_file_changed(self) -> None:
        """Steam 启动后本地文件变了 -> 疑似被云端覆盖 -> 中止, 一格命令都不发."""
        h = Harness(running=True, channel=True, expected_sha1="AAA", sha_after="BBB")
        out = h.run()
        self.assertFalse(out.succeeded)
        self.assertTrue(out.overwritten_by_cloud)
        self.assertEqual(out.error_code, trigger.ERROR_CLOUD_OVERWROTE)
        self.assertIsNone(out.sync)
        self.assertEqual(h.rec.commands, [])

    def test_skips_check_when_no_expected_sha(self) -> None:
        h = Harness(running=True, channel=True, sha_after="")
        out = h.run()
        self.assertTrue(out.succeeded)
        self.assertFalse(out.overwritten_by_cloud)

    def test_unchanged_file_passes(self) -> None:
        h = Harness(running=True, channel=True, expected_sha1="AAA", sha_after="AAA")
        out = h.run()
        self.assertTrue(out.succeeded)
        self.assertEqual(out.local_sha_after, "AAA")


class TestChannelGate(unittest.TestCase):
    def test_unlit_without_enable_capability_refuses(self) -> None:
        h = Harness(running=True, channel=False)
        out = h.run(enable_fn=None)
        self.assertFalse(out.succeeded)
        self.assertEqual(out.error_code, trigger.ERROR_CHANNEL_UNLIT)
        self.assertEqual(h.rec.commands, [], "未点亮时不许发同步命令")

    def test_unlit_after_enable_still_refuses(self) -> None:
        """点了 `testappcloudpaths` 但通道仍判未点亮 -> 仍不发同步命令.

        这是实测过的坑: 回执只证明**送达**, 生效要看 `cloud_log` 的活动.
        """
        h = Harness(running=True, channel=False)
        out = h.run()
        self.assertFalse(out.succeeded)
        self.assertEqual(out.error_code, trigger.ERROR_CHANNEL_UNLIT)
        self.assertTrue(out.channel_enabled, "点亮动作发出去了")
        self.assertFalse(out.channel_lit_after)
        self.assertEqual(h.rec.commands, [])

    def test_require_lit_false_proceeds(self) -> None:
        h = Harness(running=True, channel=False)
        out = h.run(require_lit=False)
        self.assertEqual(h.rec.commands, [f"cloud_sync_up {APPID}"])
        self.assertTrue(out.succeeded)

    def test_enable_fn_none_but_require_lit_false_ok(self) -> None:
        h = Harness(running=True, channel=None)
        out = h.run(enable_fn=None, require_lit=False)
        self.assertTrue(out.succeeded)


class TestSyncPhase(unittest.TestCase):
    def test_success_after_retry(self) -> None:
        h = Harness(running=True, channel=True, checks=[dead_check(), ok_check()])
        out = h.run(max_attempts=3)
        self.assertTrue(out.succeeded)
        self.assertEqual(out.sync.attempts, 2)
        self.assertEqual(len(h.rec.commands), 2)

    def test_exhausted_retries_reports_failure(self) -> None:
        h = Harness(running=True, channel=True, checks=[dead_check()])
        out = h.run(max_attempts=2)
        self.assertFalse(out.succeeded)
        self.assertEqual(out.error_code, trigger.ERROR_SYNC_FAILED)
        self.assertEqual(out.sync.attempts, 2)
        self.assertEqual(len(h.rec.commands), 2)

    def test_command_is_cloud_sync_up(self) -> None:
        h = Harness(running=True, channel=True)
        h.run()
        self.assertEqual(h.rec.commands, [f"cloud_sync_up {APPID}"])


class TestOutcomeShape(unittest.TestCase):
    def test_to_dict_has_all_keys(self) -> None:
        h = Harness(running=True, channel=True, expected_sha1="AAA", sha_after="AAA")
        out = h.run()
        d = out.to_dict()
        for k in (
            "appid", "rel_path", "steam_was_running", "steam_started", "steam_ready",
            "channel_was_lit", "channel_enabled", "channel_lit_after",
            "local_sha_before", "local_sha_after", "overwritten_by_cloud",
            "sync", "succeeded", "error_code", "error", "steps",
        ):
            self.assertIn(k, d)
        self.assertTrue(d["succeeded"])
        self.assertEqual(d["appid"], APPID)
        self.assertEqual(d["rel_path"], REL)

    def test_steps_are_human_readable(self) -> None:
        h = Harness(running=[False, True], channel=[False, True])
        out = h.run()
        joined = "\n".join(out.steps)
        self.assertIn("Steam 未运行", joined)
        self.assertIn("点亮", joined)
        self.assertTrue(out.succeeded)

    def test_failure_before_sync_has_none_sync(self) -> None:
        h = Harness(running=False, channel=True)
        out = h.run(allow_start_steam=False)
        d = out.to_dict()
        self.assertIsNone(d["sync"])
        self.assertTrue(d["error"])


# ---------------------------------------------------------------- launch.py


class TestLaunchHelpers(unittest.TestCase):
    def test_steam_exe_path_prefers_existing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "steam.exe"
            p.write_bytes(b"MZ")
            self.assertEqual(launch_mod.steam_exe_path(d), p)

    def test_start_steam_false_when_no_exe(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(launch_mod.start_steam(d, spawn=lambda *a, **k: 1234))

    def test_start_steam_true_when_spawn_reports_pid(self) -> None:
        import tempfile

        calls: list[list[str]] = []

        def fake_spawn(argv, cwd=None):
            calls.append(list(argv))
            return 4321

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "steam.exe").write_bytes(b"MZ")
            self.assertTrue(launch_mod.start_steam(d, spawn=fake_spawn))
        self.assertEqual(len(calls), 1)
        # ⚠ 不许偷偷加会改变行为的开关 (-offline / -offlinemode 实测都被忽略)
        self.assertEqual(len(calls[0]), 1, f"不该附加参数: {calls[0]}")

    def test_wait_for_process_returns_immediately_when_present(self) -> None:
        slept: list[float] = []
        ok, waited = launch_mod.wait_for_process(
            "steam.exe", find_pids=lambda n: [1], sleep=slept.append
        )
        self.assertTrue(ok)
        self.assertEqual(waited, 0.0)
        self.assertEqual(slept, [])

    def test_wait_for_process_times_out(self) -> None:
        ok, waited = launch_mod.wait_for_process(
            "steam.exe",
            timeout_seconds=3.0,
            poll_seconds=1.0,
            find_pids=lambda n: [],
            sleep=lambda s: None,
        )
        self.assertFalse(ok)
        self.assertGreaterEqual(waited, 3.0)

    def test_wait_for_process_appears_later(self) -> None:
        state = {"n": 0}

        def finder(name):
            state["n"] += 1
            return [7] if state["n"] >= 2 else []

        ok, waited = launch_mod.wait_for_process(
            "steam.exe", timeout_seconds=5.0, poll_seconds=1.0, find_pids=finder,
            sleep=lambda s: None,
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(waited, 1.0)


if __name__ == "__main__":
    unittest.main()
