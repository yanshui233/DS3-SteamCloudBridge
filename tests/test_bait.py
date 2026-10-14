#!/usr/bin/env python3
"""cloud_bait_check 的回归测试.

锁住三件事:
  1. `plan` 必须**零副作用** —— 诱饵验证器的可信度全押在"它不碰真存档"上.
  2. `drop` / `cleanup` 必须可逆 —— 跑完一轮后存档目录回到原样.
  3. `inspect` 的判定必须来自**实读证据**, 而不是"跑了没报错就算成功".
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import cloud_bait_check as cbc  # noqa: E402


class TestBaitCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.save_dir = self.root / "EldenRing" / "76561199194833618"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "ER0000.sl2").write_bytes(b"\x00" * 128)
        (self.save_dir / "ER0000.co2").write_bytes(b"\x00" * 32)
        self.baseline = sorted(p.name for p in self.save_dir.iterdir())

        self.steam = self.root / "Steam"
        self.ud_app = self.steam / "userdata" / "1234567890" / "1245620"
        self.ud_app.mkdir(parents=True)
        self.rc = self.ud_app / "remotecache.vdf"
        self.rc.write_text('"1245620"\n{\n\t"root"\t\t"3"\n}\n', encoding="utf-8")
        logs = self.steam / "logs"
        logs.mkdir(parents=True)
        self.log = logs / "cloud_log.txt"
        self.log.write_text(
            "[2026-10-12] AppID 1245620: Watching file ER0000.sl2\n", encoding="utf-8"
        )

        self.state = self.root / "reports" / "bait-1245620.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, mode: str, *extra: str) -> tuple[int, str]:
        argv = [
            "--mode", mode,
            "--save-dir", str(self.save_dir),
            "--appid", "1245620",
            "--steam-root", str(self.steam),
            "--state-file", str(self.state),
            *extra,
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cbc.main(argv)
        return code, buf.getvalue()

    def _names(self) -> list[str]:
        return sorted(p.name for p in self.save_dir.iterdir())

    # -- 1. plan 只读 ------------------------------------------------------

    def test_plan_is_read_only(self) -> None:
        rc_before = self.rc.read_text(encoding="utf-8")
        code, out = self._run("plan")
        self.assertEqual(code, 0)
        self.assertEqual(self._names(), self.baseline, "plan 改动了存档目录")
        self.assertEqual(self.rc.read_text(encoding="utf-8"), rc_before)
        self.assertFalse(self.state.exists(), "plan 不应写快照")
        self.assertIn("drop 将执行", out)

    # -- 2. drop/inspect/cleanup 往返 --------------------------------------

    def test_roundtrip_returns_to_baseline(self) -> None:
        code, _ = self._run("drop")
        self.assertEqual(code, 0)
        self.assertTrue(self.state.is_file())
        after_drop = self._names()
        self.assertEqual(len(after_drop), len(self.baseline) + 1)
        bait = [n for n in after_drop if n.startswith(cbc.BAIT_PREFIX)]
        self.assertEqual(len(bait), 1)
        self.assertEqual(bait[0][-4:], ".sl2")

        code, _ = self._run("cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(self._names(), self.baseline, "cleanup 没把目录还原")

    # -- 3. 判定来自实读证据 -----------------------------------------------

    def test_inspect_without_cloud_registration_is_not_seen(self) -> None:
        self._run("drop")
        code, out = self._run("inspect")
        self.assertEqual(code, 0)
        self.assertIn("NOT_SEEN", out)

        report = json.loads(
            self.state.with_name(self.state.stem + "-report.json").read_text("utf-8")
        )
        self.assertEqual(report["verdict"], "NOT_SEEN")
        self.assertFalse(report["remotecache_mentions_bait"])
        self.assertFalse(report["cloud_log_mentions_bait"])

    def test_inspect_accepted_when_cloud_registered(self) -> None:
        self._run("drop")
        bait = [n for n in self._names() if n.startswith(cbc.BAIT_PREFIX)][0]

        # 模拟 Steam 真的接受了诱饵: 写进 remotecache + 云日志
        self.rc.write_text(
            self.rc.read_text(encoding="utf-8") + f'\t"{bait}"\t\t"1"\n',
            encoding="utf-8",
        )
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(f"[2026-10-12] AppID 1245620: Upload OK for file {bait}\n")

        code, out = self._run("inspect")
        self.assertEqual(code, 0)
        self.assertIn("ACCEPTED", out)
        report = json.loads(
            self.state.with_name(self.state.stem + "-report.json").read_text("utf-8")
        )
        self.assertEqual(report["verdict"], "ACCEPTED")
        self.assertTrue(report["cloud_log_lines"])

    # -- 4. 防呆 -----------------------------------------------------------

    def test_drop_refuses_when_bait_already_present(self) -> None:
        (self.save_dir / f"{cbc.BAIT_PREFIX}stale.sl2").write_bytes(b"x")
        code, _ = self._run("drop")
        self.assertEqual(code, 2)

    def test_drop_refuses_missing_save_dir(self) -> None:
        argv = [
            "--mode", "drop",
            "--save-dir", str(self.root / "nope"),
            "--appid", "1245620",
            "--state-file", str(self.state),
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            code = cbc.main(argv)
        self.assertEqual(code, 2)

    def test_inspect_requires_snapshot(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = self._run("inspect")
        self.assertEqual(code, 2)

    # -- 5. 自动定位 -------------------------------------------------------

    def test_find_userdata_app_scans_accounts(self) -> None:
        found = cbc.find_userdata_app(self.steam, "1245620")
        self.assertEqual(found, self.ud_app)
        self.assertIsNone(cbc.find_userdata_app(self.steam, "374320"))


if __name__ == "__main__":
    unittest.main()
