"""图形向导**后端层** (`src/gui/service.py`) 的单元测试.

分层的红利就在这里: 这一层是纯逻辑, 一行 tkinter 都没有 —— 所以不开窗口就能把
"体检 / 备份 / 点亮 / 启动"全部覆盖掉. 界面层只负责显示, 它出错最多是难看,
碰不到用户的存档; 真正有副作用的动作全在这一层, 也因此必须被盯住.

真机 vs 假环境: 凡是需要 Steam 的用例, 一律走 `make_fake_steam` 造的假环境
(`inspect(steam_root=...)`), 不读真机注册表、不碰真存档、不启动真游戏.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import make_fake_steam as mfs  # noqa: E402
from src.gui import service  # noqa: E402
from src.steam import discovery  # noqa: E402


def _entry(rules: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(rules=rules)


class TestLayering(unittest.TestCase):
    """分层纪律: 后端层不许碰界面."""

    def test_service_never_imports_tkinter(self) -> None:
        text = (PROJECT_ROOT / "src" / "gui" / "service.py").read_text(encoding="utf-8")
        for bad in ("import tkinter", "from tkinter", "tkinter."):
            with self.subTest(token=bad):
                self.assertNotIn(bad, text)

    def test_importing_the_package_does_not_need_tkinter(self) -> None:
        """`import src.gui` 必须安全 —— 单文件构建期会扫它, 无图形环境的机器也要能跑.

        用 `sys.executable` (可能是没装 tkinter 的解释器) 在子进程里验, 因为
        当前测试进程可能已经被别的用例 import 过 tkinter 了.
        """
        code = "import src.gui, sys; print('tkinter' in sys.modules)"
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False", "导入 src.gui 时把 tkinter 也拉进来了")


class TestFormatting(unittest.TestCase):
    def test_fmt_size(self) -> None:
        cases = [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (9307472, "8.9 MB")]
        for value, want in cases:
            with self.subTest(value=value):
                self.assertEqual(service.fmt_size(value), want)


class TestCloudRoot(unittest.TestCase):
    """存档根目录的推导 —— 必须靠证据, 不许按游戏名猜目录."""

    def test_derives_root_from_the_cloud_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            roaming = Path(td)
            entry = _entry([{"root": "WinAppDataRoaming", "path": "DarkSoulsIII"}])
            with mock.patch.dict(os.environ, {"APPDATA": str(roaming)}):
                got = service.cloud_root_of(entry)
            self.assertEqual(got, roaming / "DarkSoulsIII")

    def test_no_rules_means_no_answer(self) -> None:
        self.assertIsNone(service.cloud_root_of(_entry([])))
        self.assertIsNone(service.cloud_root_of(None))

    def test_falls_back_to_the_located_save_path(self) -> None:
        """日志轮转后规则行会消失, 那时只能从已确认的落点反推."""
        with tempfile.TemporaryDirectory() as td:
            roaming = Path(td) / "Roaming"
            root = roaming / "DarkSoulsIII"
            (root / "0110abc").mkdir(parents=True)
            save = root / "0110abc" / "DS30000.sl2"
            save.write_bytes(b"x")

            with mock.patch.dict(os.environ, {"APPDATA": str(roaming)}):
                got, why = service.ac_root_from_save_path(save)
            self.assertEqual(got, root)
            self.assertIn("反推", why)

    def test_save_outside_appdata_falls_back_to_its_own_folder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            elsewhere = Path(td) / "somewhere" / "save"
            elsewhere.mkdir(parents=True)
            save = elsewhere / "DS30000.sl2"
            save.write_bytes(b"x")
            with mock.patch.dict(os.environ, {"APPDATA": str(Path(td) / "Roaming"),
                                              "USERPROFILE": str(Path(td) / "Home")}):
                got, why = service.ac_root_from_save_path(save)
            self.assertEqual(got, elsewhere)
            self.assertIn("未能确定", why)

    def test_missing_path_gives_nothing(self) -> None:
        self.assertEqual(service.ac_root_from_save_path(None), (None, ""))


class TestBackup(unittest.TestCase):
    """备份: 只复制不移动, 复制完必须逐个校验."""

    def _fake_env(self, root: Path) -> service.Environment:
        env = service.Environment(appid="374320")
        env.cloud_root = root
        return env

    def _make_saves(self, td: str) -> tuple[Path, bytes]:
        root = Path(td) / "DarkSoulsIII"
        (root / "0110abc").mkdir(parents=True)
        payload = b"SCB-TEST-SAVE\n" + bytes(range(256)) * 40
        (root / "0110abc" / "DS30000.sl2").write_bytes(payload)
        (root / "steam_autocloud.vdf").write_bytes(b'"accountid" "1"\n')
        return root, payload

    def test_backup_copies_verifies_and_leaves_the_source_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, payload = self._make_saves(td)
            before = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}

            res = service.backup_saves(self._fake_env(root), Path(td) / "out")

            self.assertTrue(res.ok, res.error)
            self.assertTrue(res.verified)
            self.assertEqual(len(res.files), 2)
            self.assertEqual(res.total_bytes, sum(f.size for f in res.files))
            self.assertTrue((res.dest / "0110abc" / "DS30000.sl2").is_file())
            # 源目录一个字节都不许动
            after = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual((root / "0110abc" / "DS30000.sl2").read_bytes(), payload)

    def test_backup_writes_a_readable_info_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, _ = self._make_saves(td)
            res = service.backup_saves(self._fake_env(root), Path(td) / "out")
            info = res.dest.parent / "BACKUP-INFO.txt"
            self.assertTrue(info.is_file())
            text = info.read_text(encoding="utf-8")
            self.assertIn("DS30000.sl2", text)
            self.assertIn("sha256", text)
            self.assertIn("怎么恢复", text)

    def test_backup_refuses_when_no_root_is_known(self) -> None:
        res = service.backup_saves(service.Environment())
        self.assertFalse(res.ok)
        self.assertFalse(res.verified)
        self.assertIn("存档目录", res.error)

    def test_default_root_honours_the_test_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"SCB_EXE_DIR": td}):
                self.assertEqual(service.default_backup_root(), Path(td) / service.BACKUP_DIR_NAME)

    def test_list_backups_is_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for stamp in ("dir-20260101-000000", "dir-20260102-000000"):
                d = out / stamp / "DarkSoulsIII"
                d.mkdir(parents=True)
                (d / "DS30000.sl2").write_bytes(b"x")
            items = service.list_backups(out)
            self.assertEqual([b["dir"].name for b in items],
                             ["dir-20260102-000000", "dir-20260101-000000"])

    def test_list_backups_ignores_empty_folders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "dir-20260101-000000").mkdir(parents=True)
            self.assertEqual(service.list_backups(Path(td)), [])


class TestActions(unittest.TestCase):
    def test_enable_command_is_exactly_the_documented_line(self) -> None:
        self.assertEqual(service.enable_command("374320"), "testappcloudpaths 374320")
        self.assertEqual(service.enable_command(374320), "testappcloudpaths 374320")

    def test_launching_the_game_goes_through_the_fake_hook(self) -> None:
        """测试里绝不能真去启动用户的游戏 —— 只允许记录 URI."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "launch.txt"
            with mock.patch.dict(os.environ, {"SCB_LAUNCH_FAKE": str(log)}):
                ok, detail = service.launch_game("374320")
            self.assertTrue(ok, detail)
            self.assertEqual(
                log.read_text(encoding="utf-8").strip(), "steam://rungameid/374320"
            )

    def test_open_path_refuses_what_does_not_exist(self) -> None:
        self.assertFalse(service.open_path(None))
        self.assertFalse(service.open_path(Path("C:/definitely/not/here-12345")))


class TestWatchEnable(unittest.TestCase):
    """盯控制台日志等回执 —— 这是"手敲"路径的判定核心."""

    ACK = '[2026-10-14 19:00:00] "testappcloudpaths" = "374320"\n'

    def _install(self, td: str) -> tuple[discovery.SteamInstall, Path]:
        root = Path(td) / "steam"
        logs = root / "logs"
        logs.mkdir(parents=True)
        cl = logs / "console_log.txt"
        cl.write_text("[t] Console Log Start\n", encoding="utf-8")
        return discovery.SteamInstall(root=root, source="test"), cl

    def test_sees_the_ack_the_user_typed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            install, cl = self._install(td)

            def late() -> None:
                time.sleep(1.0)
                with cl.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(self.ACK)

            threading.Thread(target=late, daemon=True).start()
            out = service.watch_enable(install, "374320", wait_s=8, poll=0.3)
            self.assertTrue(out.ok, out.reason)
            self.assertIn("testappcloudpaths", out.ack_line)

    def test_gives_up_when_nothing_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            install, _ = self._install(td)
            out = service.watch_enable(install, "374320", wait_s=1, poll=0.3)
            self.assertFalse(out.ok)
            self.assertTrue(out.ack_line == "")

    def test_ack_for_another_appid_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            install, cl = self._install(td)
            cl.write_text(
                '[t] Console Log Start\n[t] "testappcloudpaths" = "1245620"\n',
                encoding="utf-8",
            )
            out = service.watch_enable(install, "374320", wait_s=1, poll=0.3)
            self.assertFalse(out.ok, out.ack_line)

    def test_cancel_flag_stops_the_wait(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            install, _ = self._install(td)
            stop = threading.Event()
            threading.Timer(0.6, stop.set).start()
            started = time.time()
            out = service.watch_enable(
                install, "374320", wait_s=30, poll=0.3, should_stop=stop.is_set
            )
            self.assertFalse(out.ok)
            self.assertEqual(out.reason, "已取消")
            self.assertLess(time.time() - started, 10.0)


class TestInspectOnASandbox(unittest.TestCase):
    """体检跑在假环境上 —— 不读真机注册表, 也不碰真存档."""

    def _sandbox(self, td: str, scenario: str = "synced") -> dict:
        return mfs.build(Path(td) / "env", scenario, mfs.DEFAULT_ACCOUNT_ID, 8, force=False)

    def test_reads_the_sandbox_not_the_real_machine(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = self._sandbox(td)
            with mock.patch.dict(os.environ, manifest["env"]):
                env = service.inspect("374320", steam_root=manifest["steam_root"])

            self.assertEqual(env.error, "")
            self.assertEqual(env.accountid, mfs.DEFAULT_ACCOUNT_ID)
            self.assertTrue(env.game_installed)

    def test_locates_the_save_and_the_cloud_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = self._sandbox(td)
            with mock.patch.dict(os.environ, manifest["env"]):
                env = service.inspect("374320", steam_root=manifest["steam_root"])

            self.assertIsNotNone(env.save_path)
            self.assertTrue(str(env.save_path).endswith("DS30000.sl2"))
            self.assertTrue(env.save_exists)
            self.assertGreater(env.save_size, 0)
            self.assertIsNotNone(env.cloud_root)
            self.assertEqual(env.cloud_root.name, "DarkSoulsIII")
            self.assertTrue(env.can_backup)

    def test_backup_works_end_to_end_on_the_sandbox_save(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = self._sandbox(td)
            with mock.patch.dict(os.environ, manifest["env"]):
                env = service.inspect("374320", steam_root=manifest["steam_root"])
                out = Path(td) / "backups"
                res = service.backup_saves(env, out)

            self.assertTrue(res.ok, res.error)
            self.assertTrue(res.verified)
            self.assertGreaterEqual(len(res.files), 1)
            for f in res.files:
                with self.subTest(rel=f.rel):
                    self.assertEqual(len(f.sha256), 64)

    def test_reports_a_missing_steam_root_instead_of_crashing(self) -> None:
        env = service.inspect("374320", steam_root=Path("C:/no/such/steam"))
        self.assertIn("不存在", env.error)

    def test_every_check_row_has_a_known_state(self) -> None:
        """界面把 state 直接映射成颜色 —— 出现新档位会静默变成灰色."""
        with tempfile.TemporaryDirectory() as td:
            manifest = self._sandbox(td)
            with mock.patch.dict(os.environ, manifest["env"]):
                env = service.inspect("374320", steam_root=manifest["steam_root"])
            self.assertTrue(env.checks)
            for c in env.checks:
                with self.subTest(label=c.label):
                    self.assertIn(c.state, (service.OK, service.WARN, service.BAD, service.INFO))


class TestDs2Service(unittest.TestCase):
    """魂二的界面后端包装层.

    这几个函数**故意写得极薄** —— 它们存在的唯一理由是让界面别自己发明判据.
    所以这里测的不是"功能对不对"(那在 `tests/test_ds2.py` 里), 而是两条纪律:
    1. 包装层确实把调用透传给了 `src.transport.ds2`, 没有夹带私货;
    2. `ds2_locate` 保持只读, 只有 `ds2_locate_for_import` 才允许按规则建目录.
    """

    FAKE_ACCT = 1234567890
    FAKE_HEX = "01100001499602d2"  # accountid_to_hex_dir(1234567890)

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.appdata = self.root / "appdata"
        self.save_dir = self.appdata / "DarkSoulsII" / self.FAKE_HEX
        self.save_dir.mkdir(parents=True)
        self.save = self.save_dir / "DS2SOFS0000.sl2"
        self.save.write_bytes(b"sl2-bytes-v1")
        env = mock.patch.dict(
            os.environ,
            {
                "APPDATA": str(self.appdata),
                "SCB_ACTIVE_ACCOUNT": str(self.FAKE_ACCT),
                "SCB_GAME_RUNNING": "0",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_wrappers_are_exported(self) -> None:
        for name in (
            "ds2_locate",
            "ds2_locate_for_import",
            "ds2_export",
            "ds2_verify_pack",
            "ds2_read_pack",
            "ds2_import",
        ):
            with self.subTest(name=name):
                self.assertIn(name, service.__all__)
                self.assertTrue(callable(getattr(service, name)))

    def test_locate_finds_the_sandbox_save(self) -> None:
        loc = service.ds2_locate()
        self.assertEqual(loc.dir, self.save_dir)
        self.assertEqual(loc.accountid, self.FAKE_ACCT)
        self.assertEqual([f.rel for f in loc.files], ["DS2SOFS0000.sl2"])

    def test_locate_is_read_only(self) -> None:
        """只有 `..._for_import` 允许建目录 —— 只读定位建了目录就是越权."""
        shutil.rmtree(self.save_dir)
        loc = service.ds2_locate()
        self.assertIsNone(loc.dir)
        self.assertFalse(self.save_dir.exists(), "只读定位不该在磁盘上留下任何东西")

    def test_locate_for_import_resolves_the_dir_without_touching_disk(self) -> None:
        """换台从没玩过魂二的新机器导入 —— 这是主路径, 必须能自己把目标目录算出来."""
        pack = self.root / "pack"
        service.ds2_export(service.ds2_locate(), pack, label="gui-test")
        shutil.rmtree(self.save_dir)  # 假装这是一台新机器, 存档根是空的

        loc = service.ds2_locate_for_import()
        self.assertEqual(loc.dir, self.save_dir)
        self.assertEqual(loc.accountid, self.FAKE_ACCT)
        self.assertTrue(loc.created)
        self.assertFalse(loc.dir.exists(), "只推导路径, 不落盘 —— 建目录留给导入那一步")

        res = service.ds2_import(pack, loc)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.save.read_bytes(), b"sl2-bytes-v1")
        self.assertEqual(res.backup_dir, "", "空目录没什么可备份, 不该凭空造一份")

    def test_export_verify_read_import_roundtrip(self) -> None:
        pack = self.root / "pack"
        loc = service.ds2_locate()
        out = service.ds2_export(loc, pack, label="gui-test")
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.file_count, 1)

        ok, problems = service.ds2_verify_pack(pack)
        self.assertTrue(ok, problems)

        manifest = service.ds2_read_pack(pack)
        self.assertEqual(manifest["appid"], service.ds2.PROFILE.appid)
        self.assertEqual(manifest["accountid"], self.FAKE_ACCT)

        # 改成本地内容, 再导回来 —— 必须变成包里的字节, 且自动备份
        self.save.write_bytes(b"local-edit")
        res = service.ds2_import(pack, service.ds2_locate_for_import())
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.save.read_bytes(), b"sl2-bytes-v1")
        self.assertTrue(res.backup_dir)
        self.assertTrue(Path(res.backup_dir).is_dir())

    def test_import_refuses_when_the_game_is_running(self) -> None:
        pack = self.root / "pack"
        service.ds2_export(service.ds2_locate(), pack)
        with mock.patch.dict(os.environ, {"SCB_GAME_RUNNING": "1"}):
            res = service.ds2_import(pack, service.ds2_locate_for_import())
        self.assertFalse(res.ok)
        self.assertIn("GAME_RUNNING", res.blockers)
        self.assertEqual(self.save.read_bytes(), b"sl2-bytes-v1", "拒绝时一个字都不该写")


if __name__ == "__main__":
    unittest.main()
