#!/usr/bin/env python3
"""沙箱端到端测试: 造假 Steam 环境 -> 跑探测器 -> 按预期断言.

覆盖 `tools/make_fake_steam.py` + `tools/run_sandbox_probe.py` 的整条链路,
并锁住两个**只在端到端才暴露**的缺陷:

* **D5**: `--account` 必须同时决定"扫哪个 userdata 目录"和"用哪个账号标识
  反查存档目录"。旧实现只改前者, 后者仍取注册表里的真机账号 —— 于是离线
  复现 / 沙箱测试时, 指定账号的存档永远匹配不上 (实测 550 失联、报告里混进
  真机 accountid)。
* **D2**: `cloud_log` 里没有 `Watching file` 时, 定位必须能靠 `ac\\` 镜像 +
  账号标识走通 (clean / disabled 场景)。

这些测试**不接触任何真机 Steam 数据**: 全程用临时目录 + 虚构账号, 靠
`--steam-root` 与环境变量重定向把探测器的视野限制在假环境内。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import make_fake_steam as mfs  # noqa: E402
from src.steam import accounts  # noqa: E402

RUNNER = PROJECT_ROOT / "tools" / "run_sandbox_probe.py"
SCENARIOS = ("clean", "synced", "disabled")


class TestSandboxE2E(unittest.TestCase):
    """三个场景各跑一遍完整链路, 并逐条校验断言."""

    def _run(self, scenario: str) -> tuple[dict, str]:
        tmp = tempfile.TemporaryDirectory(prefix=f"scb-{scenario}-")
        self.addCleanup(tmp.cleanup)
        env_dir = Path(tmp.name) / "env"
        manifest = mfs.build(env_dir, scenario, mfs.DEFAULT_ACCOUNT_ID, 16, force=False)
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--env", str(env_dir), "--zip", "--verify"],
            capture_output=True,
            timeout=300,
        )
        out = proc.stdout.decode("utf-8", "replace")
        if proc.returncode != 0:
            self.fail(
                f"场景 {scenario} 断言未通过 (rc={proc.returncode}):\n{out}\n"
                f"{proc.stderr.decode('utf-8', 'replace')}"
            )
        self.assertNotIn("FAIL", out, out)
        return manifest, out

    def test_clean_scenario(self) -> None:
        """无云痕迹: 仍能定位存档, 但没有任何上传证据."""
        manifest, out = self._run("clean")
        self.assertIn("沙箱断言", out)
        self.assertEqual(manifest["expected"]["upload_verified"], False)
        self.assertEqual(manifest["expected"]["change_number"], 0)

    def test_synced_scenario(self) -> None:
        """已上传成功: change_number / upload_confirmed / bridgeable 全亮."""
        manifest, out = self._run("synced")
        self.assertEqual(manifest["expected"]["upload_verified"], True)
        self.assertEqual(manifest["expected"]["change_number"], 1)

    def test_disabled_scenario(self) -> None:
        """云开关关着: 给出 testappcloudpaths 提示, 且定位不依赖 cloud_log."""
        manifest, out = self._run("disabled")
        self.assertEqual(manifest["expected"]["cloud_disabled"], True)
        self.assertEqual(manifest["expected"]["expect_ac_based_locating"], True)

    def test_all_scenarios_isolated_from_real_machine(self) -> None:
        """每个场景的报告都必须只认沙箱账号 (D5 回归锁)."""
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                manifest, _ = self._run(scenario)
                reports = Path(manifest["steam_root"]).parent / "reports"
                # 完整档在 private/ 下 (根目录只放可外发的东西)
                report = reports / "private" / "machine_profile.json"
                self.assertTrue(report.is_file(), f"缺完整档: {report}")
                self.assertFalse(
                    (reports / "machine_profile.json").is_file(),
                    "未脱敏完整档不得落在 reports 根目录",
                )
                data = json.loads(report.read_text(encoding="utf-8"))
                scanned = sorted(
                    (data.get("steam") or {}).get("userdata_apps_by_account", {}).keys()
                )
                self.assertEqual(scanned, [str(mfs.DEFAULT_ACCOUNT_ID)])


class TestSandboxGenerator(unittest.TestCase):
    """生成器自身的约束: 账号自洽、不引用真机身份."""

    def test_account_triple_is_self_consistent(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scb-gen-")
        self.addCleanup(tmp.cleanup)
        manifest = mfs.build(
            Path(tmp.name) / "env", "synced", mfs.DEFAULT_ACCOUNT_ID, 8, force=False
        )
        aid = mfs.DEFAULT_ACCOUNT_ID
        self.assertEqual(manifest["account_id"], aid)
        self.assertEqual(manifest["steamid64"], str(accounts.accountid_to_steamid64(aid)))
        self.assertEqual(manifest["hex_dir"], accounts.accountid_to_hex_dir(aid))
        # 存档相对路径必须与 hex 目录一致, 否则 remotecache 标定会落空
        self.assertIn(manifest["hex_dir"], manifest["save_file"])

    def test_default_account_is_fictional_placeholder(self) -> None:
        """默认账号必须是与 tests/ 共用的虚构值, 不能是任何真机账号."""
        self.assertEqual(mfs.DEFAULT_ACCOUNT_ID, 1234567890)


if __name__ == "__main__":
    unittest.main()
