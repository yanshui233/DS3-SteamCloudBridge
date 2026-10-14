#!/usr/bin/env python3
"""跨机往返 (第 ④ 项) 的回归测试 —— 锁住三件**实测踩出来**的事:

1. **下行判据是 `Download OK for file <rel>`**, 与上传侧的 `Upload OK for file <p>` 对称.
   `Download complete, result OK` **不能**单独当判据 ——
   和 `Upload complete, result OK` 一样, 它在"无事可做"的批次里也打印.
   (见 `test_complete_alone_is_not_success`.)
2. **`expected.json` 是外部输入** —— 必须防路径穿越, 且 schema/字段不合法时**拒绝继续**,
   而不是"猜一个路径出来". (见 `TestLocateTarget` / `TestExpectedValidation`.)
3. **"Steam 说无事可做" ≠ 通过** —— 那就意味着本机与云端不一致, 必须报出来给人看.
   (见 `test_nothing_to_do_is_not_pass`.)

日志行全部取自本机真实 `logs/cloud_log.txt` (2026-10-12 01:31:43 的诱饵下行), 不是编的.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import make_fake_steam as mfs  # noqa: E402

from src.transport import crossmachine as cm  # noqa: E402

BRIDGE = PROJECT_ROOT / "tools" / "bridge.py"
DS3 = "374320"

REL = "DarkSoulsIII/01100001499602d2/DS30000.sl2"
SHA_OK = "58a0a42322bbf70b50674a10d72dcc23020e2216"
SHA_OTHER = "21572ec3" + "0" * 32

# 逐字取自本机 logs/cloud_log.txt (2026-10-12 01:31:43 诱饵下行)
REAL_LINES = [
    "[2026-10-12 01:30:15] [AppID 374320] Currently already synced to global change "
    "number '2', should be nothing to download",
    "[2026-10-12 01:31:43] [AppID 374320] Need to download file "
    "DarkSoulsIII/01100001499602d2/scb-bait-20261012-005051-c64f.sl2",
    "[2026-10-12 01:31:43] [AppID 374320] HTTP download for file "
    "'DarkSoulsIII/01100001499602d2/scb-bait-20261012-005051-c64f.sl2' from host "
    "'steamcloud-sgp.oss-accelerate.aliyuncs.com', path "
    "/CF/18/2D/3F/374320/240_9_1B3D86A2_5B5C6_1.dat, beginning. . .",
    "[2026-10-12 01:31:43] [AppID 374320] HTTP download for file "
    "'DarkSoulsIII/01100001499602d2/scb-bait-20261012-005051-c64f.sl2' from host "
    "'steamcloud-sgp.oss-accelerate.aliyuncs.com' - Success.",
    "[2026-10-12 01:31:43] [AppID 374320] Download OK for file "
    "DarkSoulsIII/01100001499602d2/scb-bait-20261012-005051-c64f.sl2",
    "[2026-10-12 01:31:43] [AppID 374320] Download complete, result OK",
    "[2026-10-11 20:49:12] [AppID 2358720] Download OK for file "
    "b1/Saved/SaveGames/7656/ArchiveSaveFile.1.sav",
]


class TestDownEvidenceParsing(unittest.TestCase):
    def test_real_lines_are_parsed(self) -> None:
        ev = cm.parse_down_evidence(REAL_LINES, "374320")
        rel = "DarkSoulsIII/01100001499602d2/scb-bait-20261012-005051-c64f.sl2"
        self.assertIn(rel, ev.need)
        self.assertIn(rel, ev.http_started)
        self.assertIn(rel, ev.http_ok)
        self.assertIn(rel, ev.download_ok)
        self.assertEqual(ev.download_complete, 1)
        self.assertEqual(ev.already_synced_cn, 2)
        self.assertEqual(ev.already_synced_count, 1)

    def test_appid_filter_keeps_other_games_out(self) -> None:
        ev = cm.parse_down_evidence(REAL_LINES, "374320")
        self.assertFalse(any("ArchiveSaveFile" in x for x in ev.download_ok))
        ev2 = cm.parse_down_evidence(REAL_LINES, "2358720")
        self.assertTrue(any("ArchiveSaveFile" in x for x in ev2.download_ok))
        self.assertEqual(ev2.download_complete, 0)

    def test_backslashes_are_normalized(self) -> None:
        lines = [
            "[2026-10-12 01:31:43] [AppID 374320] Download OK for file "
            "DarkSoulsIII\\01100001499602d2\\DS30000.sl2",
        ]
        ev = cm.parse_down_evidence(lines, "374320")
        self.assertTrue(ev.downloaded(REL))

    def test_non_download_lines_are_ignored(self) -> None:
        ev = cm.parse_down_evidence(
            [
                "[2026-10-12 01:31:43] [AppID 374320] Starting sync (down,)",
                "[2026-10-12 01:31:43] [AppID 374320] Evaluating rule 0 with "
                'root="WinAppDataRoaming" path="DarkSoulsIII" pattern="*.sl2" (recursive)',
                "",
            ],
            "374320",
        )
        self.assertFalse(ev.any_activity)


class TestDecide(unittest.TestCase):
    def test_pass_when_bytes_match(self) -> None:
        ev = cm.parse_down_evidence(
            ["[x] [AppID 374320] Download OK for file " + REL], "374320"
        )
        code, msg = cm.decide(REL, SHA_OK, SHA_OK, ev)
        self.assertEqual(code, "pass")
        self.assertIn("逐字节一致", msg)

    def test_byte_mismatch_is_reported_honestly(self) -> None:
        ev = cm.parse_down_evidence(
            ["[x] [AppID 374320] Download OK for file " + REL], "374320"
        )
        code, msg = cm.decide(REL, SHA_OK, SHA_OTHER, ev)
        self.assertEqual(code, "byte-mismatch")
        self.assertIn("不符", msg)

    def test_downloaded_but_missing_on_disk(self) -> None:
        ev = cm.parse_down_evidence(
            ["[x] [AppID 374320] Download OK for file " + REL], "374320"
        )
        code, _msg = cm.decide(REL, SHA_OK, None, ev)
        self.assertEqual(code, "byte-mismatch")

    def test_complete_alone_is_not_success(self) -> None:
        """`Download complete, result OK` 在无事可做的批次里也会打印 —— 不能当判据."""
        ev = cm.parse_down_evidence(
            ["[x] [AppID 374320] Download complete, result OK"], "374320"
        )
        code, _msg = cm.decide(REL, SHA_OK, None, ev)
        self.assertEqual(code, "pending")

    def test_nothing_to_do_is_not_pass(self) -> None:
        ev = cm.parse_down_evidence(
            [
                "[x] [AppID 374320] Currently already synced to global change number "
                "'7', should be nothing to download"
            ],
            "374320",
        )
        code, msg = cm.decide(REL, SHA_OK, None, ev)
        self.assertEqual(code, "nothing-to-do")
        self.assertIn("7", msg)

    def test_pending_when_nothing_seen(self) -> None:
        code, _msg = cm.decide(REL, SHA_OK, None, cm.DownEvidence(appid="374320"))
        self.assertEqual(code, "pending")


class TestLocateTarget(unittest.TestCase):
    def test_joins_under_appdata(self) -> None:
        p = cm.locate_target(r"C:\Users\bob\AppData\Roaming", REL)
        self.assertEqual(p.name, "DS30000.sl2")
        self.assertEqual(p.parent.name, "01100001499602d2")
        self.assertEqual(p.parent.parent.name, "DarkSoulsIII")

    def test_rejects_traversal_and_absolute(self) -> None:
        for bad in (
            "../evil.sl2",
            "DarkSoulsIII/../../evil.sl2",
            r"C:\Windows\System32\evil.sl2",
            "/etc/passwd",
            "",
            "DS30000.sl2",  # 只有一段, 不是云侧相对路径
        ):
            with self.subTest(bad=bad):
                self.assertFalse(cm.is_safe_rel(bad))
                with self.assertRaises(ValueError):
                    cm.locate_target(r"C:\Users\bob\AppData\Roaming", bad)


class TestExpectedRoundTrip(unittest.TestCase):
    def _exp(self) -> cm.Expected:
        return cm.Expected(
            appid="374320",
            rel_path="DarkSoulsIII\\01100001499602d2\\DS30000.sl2",
            sha1=SHA_OK.upper(),
            size=9307472,
            accountid=1234567890,
            account_hex_dir="01100001499602d2",
            account_name="tester",
            cloud_change_number=11,
            remote_remotetime=1791876850,
            remote_sha=SHA_OK,
            tool_version="0.2.7",
            label="test",
        )

    def test_to_dict_normalizes(self) -> None:
        d = self._exp().to_dict()
        self.assertEqual(d["schema"], cm.SCHEMA)
        self.assertEqual(d["rel_path"], REL)
        self.assertEqual(d["sha1"], SHA_OK)

    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._exp().save(Path(td) / "expected.json")
            back = cm.Expected.load(p)
        self.assertEqual(back.rel_path, REL)
        self.assertEqual(back.sha1, SHA_OK)
        self.assertEqual(back.accountid, 1234567890)
        self.assertEqual(back.cloud_change_number, 11)

    def test_load_missing_file(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cm.Expected.load("/definitely/not/here/expected.json")
        self.assertIn("读不到", str(ctx.exception))

    def test_rejects_wrong_schema(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cm.Expected.from_dict({"schema": "scb-xfer/999", "appid": "374320",
                                   "rel_path": REL, "sha1": SHA_OK, "accountid": 1})
        self.assertIn("schema", str(ctx.exception))


class TestExpectedValidation(unittest.TestCase):
    def _raw(self, **over: object) -> dict:
        base = {
            "schema": cm.SCHEMA,
            "appid": "374320",
            "rel_path": REL,
            "sha1": SHA_OK,
            "size": 9307472,
            "accountid": 1234567890,
        }
        base.update(over)
        return base

    def test_rejects_bad_appid(self) -> None:
        with self.assertRaises(ValueError):
            cm.Expected.from_dict(self._raw(appid="DS3"))

    def test_rejects_bad_sha1(self) -> None:
        for bad in ("", "zz", "58a0a423"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cm.Expected.from_dict(self._raw(sha1=bad))

    def test_rejects_bad_accountid(self) -> None:
        for bad in (0, -5, "abc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cm.Expected.from_dict(self._raw(accountid=bad))

    def test_rejects_traversal_rel_path(self) -> None:
        with self.assertRaises(ValueError):
            cm.Expected.from_dict(self._raw(rel_path="../../evil.sl2"))

    def test_toplevel_not_object(self) -> None:
        with self.assertRaises(ValueError):
            cm.Expected.from_dict([])  # type: ignore[arg-type]


class TestSha1(unittest.TestCase):
    def test_sha1_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.bin"
            p.write_bytes(b"abc")
            self.assertEqual(
                cm.sha1_of_file(p), "a9993e364706816aba3e25717850c26c9cd0d89d"
            )

    def test_load_from_disk_reads_json(self) -> None:
        data = {
            "schema": cm.SCHEMA,
            "appid": "374320",
            "rel_path": REL,
            "sha1": SHA_OK,
            "accountid": 1234567890,
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "expected.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(cm.Expected.load(p).sha1, SHA_OK)


class TestCliSurface(unittest.TestCase):
    """两个新子命令必须在 CLI 上真的存在 (打包后朋友只会双击 .bat)."""

    def test_subcommands_registered(self) -> None:
        import bridge  # noqa: PLC0415  (tools/ 已在 sys.path)

        parser = bridge.build_parser()
        push = parser.parse_args(["xfer-push", "--appid", "374320", "--source", "s.sl2"])
        self.assertTrue(callable(push.func))
        ver = parser.parse_args(["xfer-verify", "--expected", "expected.json"])
        self.assertTrue(callable(ver.func))
        self.assertFalse(ver.autoreset)
        hard = parser.parse_args(
            ["xfer-verify", "--expected", "e.json", "--autoreset", "--yes"]
        )
        self.assertTrue(hard.autoreset and hard.yes)
        # v1.0: 日常使用入口
        dly = parser.parse_args(["daily", "--appid", "374320", "--manual", "--launch"])
        self.assertTrue(callable(dly.func))
        self.assertTrue(dly.manual and dly.launch)
        self.assertFalse(dly.start_steam)


class TestXferVerifySandbox(unittest.TestCase):
    """走 **CLI 入口**在假 Steam 环境里跑完整 `xfer-verify`.

    单测只能证明"解析对/判定对"; 真正交付给对方的是一条命令行 + 一个 .bat,
    所以必须把 `参数解析 -> 环境准备 -> 账号核对 -> 目标定位 -> 触发 -> 判定 -> 出包`
    这条链整体跑一遍. 全程不接触真机数据:

    * `--steam-root` 指向 `make_fake_steam` 造的假环境;
    * 它的 `env` 把 `APPDATA` 也重定向到假 profile ⇒ 目标路径落在临时目录里;
    * `SCB_STEAM_RUNNING=1` 钉死"Steam 在跑"(隔离环境里没法让真 Steam 停下);
    * `SCB_CONSOLE_FAKE=<文件>` 让"发命令"这一步不碰 GUI (只替换发送动作, 不绕过守卫);
    * `SCB_CONSOLE_FAKE_FAIL=1` 让自动键入**永远失败** ⇒ 测"转人工"那条回退路径;
    * `manual_script=[(命令, 延迟秒), …]` 模拟"人照屏幕提示依次敲两行" ——
      同时写假控制台文件与 `console_log` 的回执行 (工具等**点亮**认后者);
    * `scenario="disabled"` 造一个**游戏级开关关着**的假环境 —— 对应 2026-10-13
      朋友机的真实现场 (判据当时假阳性报了"已点亮", 于是连点亮都没做);
    * `SCB_STEAM_START_EPOCH=0` 关掉"steam.exe 启动时间"这个会话兜底;
    * 命令真被送达后, 后台的 `_serve_download` 才追加下行日志并把云端副本**真的写到盘上**
      —— 这两件事在真机上是同一个动作的两半, 沙箱里只能自己补. 反过来, 命令没送达
      ⇒ 什么都不发生, 所以"发送失败 ⇒ 判超时"这条性质照样被测到.
    """

    def _env(self, tmp: Path, *, console_ack: bool = True, scenario: str = "synced"):
        man = mfs.build(tmp / "env", scenario, mfs.DEFAULT_ACCOUNT_ID, 8, force=False)
        steam_root = Path(man["steam_root"])
        rel = f"DarkSoulsIII/{man['hex_dir']}/DS30000.sl2"
        # 注意: 下行日志行**不在这里**预置. 工具的 watermark 是在"发命令"之前取的,
        # 预置的行会被切掉、永远看不见 —— 那测的就不是真链路了.
        # 真正的下行证据由 `_serve_download` 在命令送达**之后**追加 (模拟 Steam 的反应).
        if console_ack:
            cl = steam_root / "logs" / "console_log.txt"
            text = cl.read_text(encoding="utf-8", errors="replace")
            if text and not text.endswith("\n"):
                text += "\n"
            cl.write_text(
                text + f'[2026-10-13 16:00:00] "testappcloudpaths" = "{DS3}"\n',
                encoding="utf-8",
                newline="\n",
            )
        exp = cm.Expected(
            appid=DS3,
            rel_path=rel,
            sha1=man["save_sha1"],
            size=int(man["save_bytes"]),
            accountid=int(man["account_id"]),
            account_hex_dir=man["hex_dir"],
            account_name="sandbox",
            cloud_change_number=3,
            remote_remotetime=1,
            tool_version="0.2.7",
            label="sandbox",
        )
        return man, steam_root, exp

    @staticmethod
    def _serve_download(
        fake: Path, clog: Path, target: Path, man: dict, appid: str, rel: str,
        stop: threading.Event,
    ) -> None:
        """模拟 Steam 收到 `cloud_sync_down` 之后的行为: 写日志 + 把云端副本落到本机路径.

        真实 Steam 里这两件事是同一个动作的两半; 沙箱里没有真 Steam, 只能自己补上.
        它**只替换 Steam 的行为**, 不绕过工具的任何守卫 —— 命令仍然先走程序的发送路径,
        所以"发送失败 ⇒ 什么都不发生"这类性质照样被测到.
        """
        mirror = (
            Path(man["steam_root"]) / "userdata" / str(man["account_id"])
            / str(man["appid"]) / "ac" / "WinAppDataRoaming"
            / "DarkSoulsIII" / str(man["hex_dir"]) / "DS30000.sl2"
        )
        want = f"cloud_sync_down {appid}"
        deadline = time.time() + 120.0
        while time.time() < deadline and not stop.is_set():
            try:
                seen = fake.read_text(encoding="utf-8", errors="replace")
            except OSError:
                seen = ""
            if want in seen:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(mirror.read_bytes())
                    rows = [
                        f"[2026-10-13 16:00:01] [AppID {appid}] Need to download file {rel}",
                        f"[2026-10-13 16:00:01] [AppID {appid}] HTTP download for file "
                        f"'{rel}' from host 'steamcloud-sgp.oss-accelerate.aliyuncs.com' "
                        "- Success.",
                        f"[2026-10-13 16:00:01] [AppID {appid}] Download OK for file {rel}",
                        f"[2026-10-13 16:00:01] [AppID {appid}] Download complete, result OK",
                    ]
                    with open(clog, "a", encoding="utf-8", newline="\n") as fh:
                        fh.write("\n".join(rows) + "\n")
                except OSError:
                    pass
                return
            time.sleep(0.05)

    @staticmethod
    def _human_types(fake: Path, appid: str, after: float, stop: threading.Event) -> None:
        """模拟"人自己敲那一行": 等 `after` 秒后把命令写进假控制台文件.

        必须是**延迟**动作 —— 工具的 watermark 在进入轮询之前取, 比它更早落地的
        "外部响应"会被切掉 (这条坑踩过). 现实里人当然也是看到提示之后才敲的.
        """
        time.sleep(after)
        if stop.is_set():
            return
        try:
            fake.parent.mkdir(parents=True, exist_ok=True)
            with open(fake, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"cloud_sync_down {appid}\n")
        except OSError:
            pass

    @staticmethod
    def _human_script(
        console_log: Path, fake: Path, appid: str,
        script: list[tuple[str, float]], stop: threading.Event,
    ) -> None:
        """模拟"人照屏幕提示依次敲两行": 每条 `(命令行, 等待秒数)` 依次落地.

        为什么两处都要写: 真 Steam 里"这一行进过控制台"的回显落在 `console_log.txt`,
        而同步动作的痕迹落在 `cloud_log.txt` —— 工具等**点亮**认的是前者 (回执行),
        等**下行**认的是后者. 所以 `testappcloudpaths` 两边都要写:
        假控制台文件让后台 watcher 看到它到过, `console_log` 让等回执的循环认账.
        `cloud_sync_down` 只写假控制台文件, 其后由 `_serve_download` 补 cloud_log 与落盘.

        同样必须**延迟**落地: 早于工具 watermark 的"外部响应"会被切掉 (踩过这坑).
        """
        for entry, delay in script:
            time.sleep(delay)
            if stop.is_set():
                return
            try:
                fake.parent.mkdir(parents=True, exist_ok=True)
                with open(fake, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"{entry}\n")
            except OSError:
                pass
            if entry.startswith("testappcloudpaths"):
                try:
                    with open(console_log, "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(
                            f'[2026-10-13 16:00:05] "testappcloudpaths" = "{appid}"\n'
                        )
                except OSError:
                    pass

    def _run(
        self, tmp: Path, man, steam_root, exp, *, file_mode: str = "ok",
        simulate_download: bool = True, game_running: bool = False,
        auto_send_fails: bool = False, human_after: float = 0.0,
        manual: bool = False, manual_wait: float | None = None,
        manual_script: list | None = None,
    ):
        exp_path = tmp / "expected.json"
        exp.save(exp_path)
        out = tmp / "out"
        target = Path(man["env"]["APPDATA"]) / exp.rel_norm.replace("/", os.sep)
        if file_mode == "absent":
            if target.exists():
                target.unlink()
        elif file_mode == "wrong":
            target.write_bytes(b"these are not the bytes you are looking for")
        env = dict(os.environ)
        env.update(man["env"])
        env["SCB_STEAM_RUNNING"] = "1"
        env["SCB_STEAM_START_EPOCH"] = "0"
        env["SCB_GAME_RUNNING"] = "1" if game_running else "0"
        if auto_send_fails:
            env["SCB_CONSOLE_FAKE_FAIL"] = "1"
        fake = tmp / "console_fake.txt"
        env["SCB_CONSOLE_FAKE"] = str(fake)
        env["PYTHONIOENCODING"] = "utf-8"
        stop = threading.Event()
        worker = None
        if simulate_download:
            worker = threading.Thread(
                target=self._serve_download,
                args=(fake, steam_root / "logs" / "cloud_log.txt", target, man,
                      exp.appid, exp.rel_norm, stop),
                daemon=True,
            )
            worker.start()
        if manual_script:
            threading.Thread(
                target=self._human_script,
                args=(steam_root / "logs" / "console_log.txt", fake, exp.appid,
                      list(manual_script), stop),
                daemon=True,
            ).start()
        elif human_after > 0:
            threading.Thread(
                target=self._human_types,
                args=(fake, exp.appid, human_after, stop),
                daemon=True,
            ).start()
        argv = [
            sys.executable, str(BRIDGE),
            "--steam-root", str(steam_root),
            "xfer-verify",
            "--expected", str(exp_path),
            "--out-dir", str(out),
            "--attempts", "1", "--wait", "3", "--poll", "1",
        ]
        if manual:
            argv.append("--manual")
        if manual_wait is not None:
            argv += ["--manual-wait", str(manual_wait)]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=240,
            )
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=2.0)
        return proc, out

    # ------------------------------------------------------------ v1.0: daily
    def _run_daily(
        self, tmp: Path, man, steam_root, exp, *, human_script=None,
        game_running: bool = False, manual: bool = True, manual_wait: float = 40.0,
        launch: bool = True,
    ):
        """跑 `bridge daily`。`SCB_LAUNCH_FAKE` 让"启动游戏"落在文件里, 不真启动。"""
        env = dict(os.environ)
        env.update(man["env"])
        env["SCB_STEAM_RUNNING"] = "1"
        env["SCB_STEAM_START_EPOCH"] = "0"
        env["SCB_GAME_RUNNING"] = "1" if game_running else "0"
        env["PYTHONIOENCODING"] = "utf-8"
        fake = tmp / "console_fake.txt"
        env["SCB_CONSOLE_FAKE"] = str(fake)
        launch_log = tmp / "launch_fake.txt"
        env["SCB_LAUNCH_FAKE"] = str(launch_log)
        out_json = tmp / "daily.json"
        stop = threading.Event()
        if human_script:
            threading.Thread(
                target=self._human_script,
                args=(steam_root / "logs" / "console_log.txt", fake, exp.appid,
                      list(human_script), stop),
                daemon=True,
            ).start()
        argv = [
            sys.executable, str(BRIDGE), "--steam-root", str(steam_root), "daily",
            "--appid", exp.appid, "--poll", "1", "--json", str(out_json),
        ]
        if manual:
            argv.append("--manual")
        if manual_wait is not None:
            argv += ["--manual-wait", str(manual_wait)]
        if launch:
            argv.append("--launch")
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=180,
            )
        finally:
            stop.set()
        return proc, out_json, launch_log

    def test_daily_manual_light_up_then_launches(self) -> None:
        """v1.0 日常同步: 手敲点亮 -> 通道就绪 -> 让 Steam 启动游戏.

        `scenario="disabled"` + `console_ack=False` ⇒ 判据一定认为"未点亮",
        所以必然走**人工点亮**那条路 —— 这正是 RUN-SYNC.bat 双击后的第一件事.
        游戏在沙箱里不能真启动, 用 `SCB_LAUNCH_FAKE` 把 URI 记进文件.
        """
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp, console_ack=False, scenario="disabled")
        proc, out_json, launch_log = self._run_daily(
            tmp, man, steam_root, exp,
            human_script=[("testappcloudpaths 374320", 3.0)],
            manual_wait=40.0, launch=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # 必须把**要点亮的那一行**打出来 (不是只让人敲下行)
        self.assertIn("需要你手动敲一行", proc.stdout)
        self.assertIn("testappcloudpaths 374320", proc.stdout)
        self.assertIn("steam://open/console", proc.stdout)
        # 收到回执行
        self.assertIn("收到点亮回执", proc.stdout)
        # 游戏启动交给了 Steam (沙箱里记进文件)
        self.assertTrue(launch_log.is_file(), proc.stdout)
        self.assertIn("steam://rungameid/374320", launch_log.read_text(encoding="utf-8"))
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(payload["daily"]["launched"])
        self.assertEqual(payload["daily"]["appid"], "374320")

    def test_daily_without_launch_only_lights_up(self) -> None:
        """不加 `--launch`: 只点亮, 不启动游戏 (游戏一句都不提启动 URI)."""
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp, console_ack=False, scenario="disabled")
        proc, _out, launch_log = self._run_daily(
            tmp, man, steam_root, exp,
            human_script=[("testappcloudpaths 374320", 3.0)],
            manual_wait=40.0, launch=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("通道已亮", proc.stdout)
        self.assertFalse(launch_log.is_file(), "没加 --launch 就不该去启动游戏")

    def test_daily_refuses_while_game_is_running(self) -> None:
        """游戏在跑 => 硬拒绝 (点亮必须发生在游戏启动**之前**)."""
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp, console_ack=False, scenario="disabled")
        proc, _out, launch_log = self._run_daily(
            tmp, man, steam_root, exp, game_running=True, manual_wait=4.0, launch=True,
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("正在运行", proc.stdout)
        self.assertFalse(launch_log.is_file())
        # 一条命令都没发出去
        self.assertFalse((tmp / "console_fake.txt").exists())

    def _tmp(self) -> Path:
        holder = tempfile.TemporaryDirectory(prefix="scb-xfer-e2e-")
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def test_pass_path(self) -> None:
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(tmp, man, steam_root, exp, file_mode="absent")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("通过", proc.stdout)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"]["code"], "pass")
        self.assertTrue(report["verdict"]["passed"])
        self.assertEqual(report["down"]["evidence"]["download_ok"], [exp.rel_norm])
        # 回传包里不能出现 prior/ (那是对方机器上原文件的字节备份)
        import zipfile

        with zipfile.ZipFile(out / "cross_bundle.zip") as zf:
            names = zf.namelist()
        self.assertIn("cross_report.md", names)
        self.assertFalse(any(n.startswith("prior/") for n in names), names)

    def test_timeout_when_no_download_evidence(self) -> None:
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(
            tmp, man, steam_root, exp, file_mode="absent", simulate_download=False
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("超时", proc.stdout)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"]["code"], "timeout")

    def test_refuses_when_target_exists_with_other_content(self) -> None:
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, _out = self._run(tmp, man, steam_root, exp, file_mode="wrong")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("内容不同", proc.stdout)

    def test_refuses_when_target_already_matches(self) -> None:
        """已存在且内容正好等于期望值 ⇒ 无法区分"本次下发"与"上次残留", 必须拒绝."""
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, _out = self._run(tmp, man, steam_root, exp, file_mode="ok")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("无法证明", proc.stdout)

    def test_refuses_on_account_mismatch(self) -> None:
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        exp.accountid = int(man["account_id"]) + 12345  # 一个本机没有痕迹的账号
        proc, _out = self._run(tmp, man, steam_root, exp, file_mode="absent")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("登录的不是这个账号", proc.stdout)

    def test_clean_start_downloads_and_passes(self) -> None:
        """真正的目标场景: 本机**没有**这个文件, 云端下发后逐字节一致."""
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(tmp, man, steam_root, exp, file_mode="absent")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["target"]["before"]["exists"])
        self.assertTrue(report["target"]["after"]["exists"])
        self.assertEqual(report["target"]["after"]["sha1"], exp.sha1)

    def test_refuses_while_game_is_running(self) -> None:
        """游戏在跑时必须**硬拒绝** —— 它自己也在跑 Auto-Cloud, 会抢同一个文件.

        这一条不靠说明书写"请先关游戏": 说明书会被漏掉, 代码不会.
        而且拒绝要发生在**发任何命令之前** (`test` 里没有 `_serve_download` 的痕迹).
        """
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(
            tmp, man, steam_root, exp, file_mode="absent", game_running=True
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("正在运行", proc.stdout)
        self.assertIn("完全退出游戏", proc.stdout)
        # 命令一条都没发出去
        self.assertFalse((tmp / "console_fake.txt").exists())

    def test_manual_fallback_when_auto_send_fails(self) -> None:
        """自动键入失败 ⇒ **不误发、转人工**, 人敲完照样判 pass (判定逻辑不变).

        这条对应 2026-10-13 真机上的失败: 窗口尺寸/DPI 不同 ⇒ 点输入行的坐标配方
        4 次全打空 ⇒ 工具拒绝回车 ⇒ 整轮白跑. 现在它把那一行打出来让人敲, 自己继续
        看日志 —— 所以"自动键入失败"不再等于"任务失败".
        """
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(
            tmp, man, steam_root, exp, file_mode="absent",
            auto_send_fails=True, human_after=6.0, manual_wait=40.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("需要你手动敲两行", proc.stdout)
        # ★ 人工接管必须**自带点亮步骤**: 判据假阳性时 P5 会被整个跳过,
        # 只给下行会让 `cloud_sync_down` 被 Steam 以 `AutoCloud is disabled` 拒掉
        # (2026-10-13 两轮真机就是这么白跑的).
        self.assertIn(f"testappcloudpaths {DS3}", proc.stdout)
        self.assertIn(f"cloud_sync_down {DS3}", proc.stdout)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["down"]["mode"], "manual")
        self.assertTrue(report["verdict"]["passed"])
        self.assertEqual(report["target"]["before"]["exists"], False)
        self.assertEqual(report["target"]["after"]["sha1"], exp.sha1)

    def test_manual_mode_never_touches_the_gui(self) -> None:
        """`--manual`: 一步都不碰 GUI (不点、不键入), 只打印 + 监测."""
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(
            tmp, man, steam_root, exp, file_mode="absent",
            manual=True, human_after=6.0, manual_wait=40.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("手动模式", proc.stdout)
        self.assertIn("需要你手动敲两行", proc.stdout)
        # ★★ 回归锁 (本次修的洞): 即便通道**被判为已点亮**(假阳性 ⇒ P5 被跳过),
        # 人工提示里也**必须先给点亮那一行**, 否则朋友只敲下行 ⇒ 被拒 ⇒ 白跑.
        self.assertIn(f"testappcloudpaths {DS3}", proc.stdout)
        self.assertIn(f"cloud_sync_down {DS3}", proc.stdout)
        self.assertNotIn("发送控制台命令", proc.stdout)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["down"]["mode"], "manual")

    def test_manual_timeout_is_reported_as_such(self) -> None:
        """转人工后人不敲 ⇒ 超时, 且措辞点明"那一行没敲进控制台"."""
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp)
        proc, out = self._run(
            tmp, man, steam_root, exp, file_mode="absent",
            auto_send_fails=True, manual_wait=6.0,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"]["code"], "timeout")
        self.assertEqual(report["down"]["mode"], "manual")
        self.assertIn("人工接管等待期内", report["verdict"]["message"])

    def test_disabled_channel_is_lit_up_then_downloads_and_passes(self) -> None:
        """★ 2026-10-13 朋友机实测回归 (整条链).

        现场: 账号对、目标不存在、云端就绪 —— 但**游戏级开关是关着的**,
        `cloud_sync_down` 被 Steam 以 `AutoCloud is disabled` 拒掉. 修前判据还
        假阳性报了"已点亮", 于是 P5 连点亮都没做, 整轮白跑.

        这条锁住修好之后的整链: 判为未点亮 ⇒ 去点亮 (自动键入失败则**等人工敲**
        `testappcloudpaths`, 不要求重跑) ⇒ 再 `cloud_sync_down` ⇒ 下发 ⇒ pass.
        两个人工步骤在**同一次运行**里完成.
        """
        tmp = self._tmp()
        man, steam_root, exp = self._env(tmp, console_ack=False, scenario="disabled")
        proc, out = self._run(
            tmp, man, steam_root, exp, file_mode="absent", auto_send_fails=True,
            manual_script=[
                (f"testappcloudpaths {DS3}", 4.0),
                (f"cloud_sync_down {DS3}", 9.0),
            ],
            manual_wait=45.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # 点亮那一步走了"转人工", 而且**没有让对方重跑**
        self.assertIn("需要你手动敲", proc.stdout)
        self.assertIn(f"testappcloudpaths {DS3}", proc.stdout)
        report = json.loads((out / "cross_report.json").read_text(encoding="utf-8"))
        # 点亮之后, 本会话通道应判为已点亮 (控制台回执)
        self.assertTrue(report["channel"]["lit"], report["channel"])
        self.assertEqual(report["verdict"]["code"], "pass")
        self.assertTrue(report["verdict"]["passed"])
        self.assertEqual(report["down"]["mode"], "manual")
        self.assertFalse(report["target"]["before"]["exists"])
        self.assertEqual(report["target"]["after"]["sha1"], exp.sha1)
        # 诊断增强: 回传包里要带"本会话该 appid 的全部行"
        self.assertTrue(report["cloud_log_session_appid"])
        import zipfile as _zip

        with _zip.ZipFile(out / "cross_bundle.zip") as zf:
            self.assertIn("cloud_log_session_appid.txt", zf.namelist())


class TestXferPushFromCloud(unittest.TestCase):
    """零写入出契约 (`xfer-push --from-cloud`) 的硬性质: **一个字节都不写**.

    动机 (真机流程里暴露的缺口): 只想验"跨机下行"时, 完整往返要求先把本机真实档
    换成测试档、测完再复位 —— 中间那段窗口里云上躺着的是旧档, 是**多余的真实进度
    风险**. 这条路径只声明云端**已有的**那份副本, 所以云端从头到尾没被动过.
    """

    def _tmp(self) -> Path:
        holder = tempfile.TemporaryDirectory(prefix="scb-xfer-declare-")
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def _fake(self, tmp: Path):
        man = mfs.build(tmp / "env", "synced", mfs.DEFAULT_ACCOUNT_ID, 8, force=False)
        return man, Path(man["steam_root"])

    def _run(self, tmp, man, steam_root, *, extra=()):
        env = dict(os.environ)
        env.update(man["env"])
        env["SCB_STEAM_RUNNING"] = "1"  # 零写入模式**不该**被"Steam 在跑"挡住
        env["SCB_STEAM_START_EPOCH"] = "0"
        env["PYTHONIOENCODING"] = "utf-8"
        out = tmp / "out"
        cmd = [
            sys.executable, str(BRIDGE), "--steam-root", str(steam_root),
            "xfer-push", "--appid", DS3, "--from-cloud", "--out", str(out),
        ]
        cmd.extend(extra)
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=120,
        ), out

    def test_declares_contract_without_writing_anything(self) -> None:
        tmp = self._tmp()
        man, steam_root = self._fake(tmp)
        target = (
            Path(man["env"]["APPDATA"]) / "DarkSoulsIII" / man["hex_dir"] / "DS30000.sl2"
        )
        self.assertTrue(target.is_file(), "假环境没把存档落在目标路径上, 测试前提不成立")
        before = target.read_bytes()
        clog = steam_root / "logs" / "cloud_log.txt"
        log_before = clog.read_text(encoding="utf-8", errors="replace")

        proc, out = self._run(tmp, man, steam_root)  # 注意: **不加 --yes**

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        exp = cm.Expected.load(out / "expected.json")
        self.assertEqual(exp.appid, DS3)
        self.assertEqual(exp.rel_norm, f"DarkSoulsIII/{man['hex_dir']}/DS30000.sl2")
        self.assertEqual(exp.sha1, man["save_sha1"])
        self.assertEqual(exp.size, int(man["save_bytes"]))
        self.assertEqual(exp.accountid, int(man["account_id"]))
        self.assertGreater(exp.remote_remotetime, 0)
        # 零写入的两条硬性质: 存档字节、云日志都**逐字节**没动
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(clog.read_text(encoding="utf-8", errors="replace"), log_before)

    def test_refuses_without_cloud_copy(self) -> None:
        """云端没有副本 (`remotetime == 0`) ⇒ 拒绝, 而不是产出一份测不出东西的契约."""
        tmp = self._tmp()
        man, steam_root = self._fake(tmp)
        rc_file = (
            steam_root / "userdata" / str(man["account_id"]) / DS3 / "remotecache.vdf"
        )
        text = rc_file.read_text(encoding="utf-8", errors="replace")
        rc_file.write_text(
            re.sub(r'("remotetime"\s*)"\d+"', r'\1"0"', text),
            encoding="utf-8",
            newline="\n",
        )
        proc, out = self._run(tmp, man, steam_root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("云端没有这个文件的副本", proc.stdout)
        self.assertFalse((out / "expected.json").exists())


if __name__ == "__main__":
    unittest.main()
