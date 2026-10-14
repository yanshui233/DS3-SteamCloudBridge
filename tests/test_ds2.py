"""魂二存档搬运 (`src/transport/ds2.py`) 的回归测试.

这些用例锁住的都是"会丢存档"或"会走错门"的路径, 不是覆盖率装饰:

* 落点必须**先在磁盘上找**, 找不到才允许按规则生成, 且生成的结果要标出来;
* 导入前必须逐字节核对, 核不过**一个字都不许写**;
* 备份必须发生在写入之前, 备份失败必须中止;
* 跨账号 / 游戏运行中 必须拦下;
* 清单是**外部输入**, 里面的文件名不能带路径分隔符 (目录穿越).

测试里一律用**虚构账号** (1234567890 / 目录名 01100001499602d2), 不出现真机的值 ——
`tests/test_redaction.py` 的 blocklist 会扫这个仓库, 真值只该存在于证据目录里.
"""

from __future__ import annotations

import json
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

from src.transport import ds2  # noqa: E402

# 自洽的虚构三元组 (accountid <-> 十六进制目录名), 与 test_redaction 的假值一致.
FAKE_ACCT = 1234567890
FAKE_HEX = "01100001499602d2"
OTHER_ACCT = 1111111111
OTHER_HEX = "01100001423a35c7"


def _blob(seed: bytes, size: int = 4096) -> bytes:
    return (seed * (size // len(seed) + 1))[:size]


class _TreeBase(unittest.TestCase):
    """给每个用例一个隔离的 `%APPDATA%` 与临时工作区."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="scb-ds2-")
        self.root = Path(self._tmp.name)
        self.appdata = self.root / "appdata"
        self.work = self.root / "work"
        self.appdata.mkdir()
        self.work.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def make_account_dir(self, hexname: str = FAKE_HEX, **files: bytes) -> Path:
        d = self.appdata / "DarkSoulsII" / hexname
        d.mkdir(parents=True, exist_ok=True)
        payload = files or {
            "DS2SOFS0000.sl2": _blob(b"sl2"),
            "DS2SOFS0000.co2": _blob(b"co2"),
        }
        for name, blob in payload.items():
            (d / name).write_bytes(blob)
        return d

    def locate(self, **kw):
        return ds2.locate(appdata_dir=self.appdata, **kw)


# ---------------------------------------------------------------- 落点


class TestLocate(_TreeBase):
    def test_single_account_dir_is_found_from_disk(self) -> None:
        d = self.make_account_dir()
        loc = self.locate()
        self.assertEqual(loc.dir, d)
        self.assertEqual(loc.accountid, FAKE_ACCT)
        self.assertFalse(loc.created)
        self.assertIn("DS2SOFS0000.sl2", [f.rel for f in loc.files])

    def test_non_hex_sibling_dirs_are_ignored(self) -> None:
        """`DarkSoulsII/` 下可能有别的目录 (插件、日志); 只有 16 位 hex 才算账号."""
        self.make_account_dir()
        (self.appdata / "DarkSoulsII" / "logs").mkdir()
        (self.appdata / "DarkSoulsII" / "GraphicsConfig_SOFS.xml").write_text("x")
        loc = self.locate()
        self.assertEqual(loc.accountid, FAKE_ACCT)

    def test_multiple_accounts_use_explicit_accountid(self) -> None:
        a = self.make_account_dir(FAKE_HEX)
        self.make_account_dir(OTHER_HEX)
        loc = self.locate(accountid=FAKE_ACCT)
        self.assertEqual(loc.dir, a)

        loc2 = self.locate(accountid=OTHER_ACCT)
        self.assertEqual(loc2.accountid, OTHER_ACCT)

    def test_single_candidate_does_not_consult_the_registry(self) -> None:
        """恰好一个账号目录时**它本身就是答案**, 不该再去读注册表.

        两个理由: 真机的注册表值会把证据行弄脏 (看着像"磁盘证据不足"),
        而且测试要能脱离真机跑.
        """
        d = self.make_account_dir()
        with mock.patch.object(
            ds2.accounts, "get_active_accountid", side_effect=AssertionError("不该被调用")
        ):
            loc = self.locate()
        self.assertEqual(loc.dir, d)
        self.assertFalse(any("本机当前账号" in e for e in loc.evidence))

    def test_explicit_accountid_conflicting_with_disk_is_reported(self) -> None:
        """用户明确说了账号, 而磁盘上只有另一个账号 —— 按磁盘证据走, 但必须说明.

        不说的话, 用户会以为自己指定的账号生效了.
        """
        d = self.make_account_dir()  # FAKE_HEX
        loc = self.locate(accountid=OTHER_ACCT)
        self.assertEqual(loc.dir, d)
        self.assertEqual(loc.accountid, FAKE_ACCT)
        self.assertTrue(any("只有一个账号目录" in n for n in loc.notes))

    def test_multiple_accounts_without_any_hint_refuses_to_guess(self) -> None:
        """两个账号目录、又判不出本机账号时, 宁可拒绝也不猜."""
        self.make_account_dir(FAKE_HEX)
        self.make_account_dir(OTHER_HEX)
        with mock.patch.object(ds2.accounts, "get_active_accountid", return_value=None):
            loc = self.locate()
        self.assertFalse(loc.resolved)
        # 未定位还不够 —— 必须说清"为什么定不下来", 否则用户只看到"未定位"干瞪眼
        self.assertTrue(any("无法确定哪个是本机账号" in n for n in loc.notes))

    def test_empty_root_without_create_is_unresolved(self) -> None:
        (self.appdata / "DarkSoulsII").mkdir()
        loc = self.locate()
        self.assertFalse(loc.resolved)
        self.assertFalse(loc.created)

    def test_empty_root_with_create_builds_dir_from_rule_and_flags_it(self) -> None:
        """换台新电脑导入是主路径 —— 这台机器从没玩过魂二, 存档根是空的."""
        loc = self.locate(accountid=FAKE_ACCT, create=True)
        self.assertTrue(loc.resolved)
        self.assertTrue(loc.created)
        self.assertEqual(loc.dir.name, FAKE_HEX)
        # 必须能看出"这个目录是算出来的, 不是磁盘上找到的"
        self.assertTrue(any("按已验证规则" in e for e in loc.evidence))
        self.assertTrue(any("不是磁盘上实测到" in n for n in loc.notes))

    def test_create_without_known_account_falls_back_to_local_account(self) -> None:
        (self.appdata / "DarkSoulsII").mkdir()
        with mock.patch.object(ds2.accounts, "get_active_accountid", return_value=OTHER_ACCT):
            loc = self.locate(create=True)
        self.assertTrue(loc.resolved)
        self.assertEqual(loc.accountid, OTHER_ACCT)
        self.assertEqual(loc.dir.name, OTHER_HEX)

    def test_no_hex_dir_rule_matches_steamid64_convention(self) -> None:
        """目录名 = SteamID64 的 16 位十六进制 —— 与魂三同构, 别再自创一套."""
        loc = self.locate(accountid=FAKE_ACCT, create=True)
        self.assertEqual(
            loc.dir.name, ds2.accounts.accountid_to_hex_dir(FAKE_ACCT)
        )
        self.assertEqual(loc.steamid64, ds2.accounts.accountid_to_steamid64(FAKE_ACCT))

    def test_profile_says_no_steam_cloud(self) -> None:
        """这是本模块存在的理由 —— 别被后人"顺手"改成 True."""
        self.assertFalse(ds2.PROFILE.steam_cloud)


# ---------------------------------------------------------------- 导出


class TestExport(_TreeBase):
    def test_manifest_carries_everything_a_reader_needs(self) -> None:
        self.make_account_dir()
        loc = self.locate()
        res = ds2.export_pack(loc, self.work / "pack", label="unit")
        self.assertTrue(res.ok, res.error)
        m = json.loads((self.work / "pack" / ds2.MANIFEST_NAME).read_text("utf-8"))
        self.assertEqual(m["schema"], ds2.PACK_SCHEMA)
        self.assertEqual(m["appid"], "335300")
        self.assertEqual(m["accountid"], FAKE_ACCT)
        self.assertFalse(m["steam_cloud"])
        self.assertEqual(m["label"], "unit")
        self.assertEqual(m["file_count"], 2)
        self.assertEqual(len(m["files"]), 2)
        for f in m["files"]:
            self.assertTrue(f["sha256"] and f["size"])

    def test_bak_files_are_excluded_by_default(self) -> None:
        """`.co2.bak` 是联机 mod 自己的轮转备份, 十个起步、每个都有一份存档大小."""
        self.make_account_dir(**{
            "DS2SOFS0000.sl2": _blob(b"a"),
            "DS2SOFS0000.co2.bak": _blob(b"b"),
        })
        loc = self.locate()
        self.assertEqual([f.rel for f in loc.files], ["DS2SOFS0000.sl2"])
        loc_all = self.locate(include_all=True)
        self.assertEqual(len(loc_all.files), 2)

    def test_empty_save_dir_is_an_error_not_an_empty_pack(self) -> None:
        self.make_account_dir()
        (self.appdata / "DarkSoulsII" / FAKE_HEX / "DS2SOFS0000.sl2").unlink()
        (self.appdata / "DarkSoulsII" / FAKE_HEX / "DS2SOFS0000.co2").unlink()
        res = ds2.export_pack(self.locate(), self.work / "pack")
        self.assertFalse(res.ok)
        self.assertIn("没有可导出", res.error)

    def test_partial_export_leaves_no_manifest(self) -> None:
        """清单最后写 —— 它不存在就等于"这包不完整", 读端不会把半包当整包."""
        self.make_account_dir()
        pack = self.work / "pack"
        # 让第二个文件的复制失败, 导出应当中止且不留下清单
        real_copy = ds2.shutil.copyfile
        calls = {"n": 0}

        def flaky(src, dst, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("模拟磁盘满")
            return real_copy(src, dst, *a, **kw)

        with mock.patch.object(ds2.shutil, "copyfile", side_effect=flaky):
            res = ds2.export_pack(self.locate(), pack)
        self.assertFalse(res.ok)
        self.assertFalse((pack / ds2.MANIFEST_NAME).exists())

    def test_export_preserves_mtime(self) -> None:
        d = self.make_account_dir()
        stamp = time.time() - 86400
        os.utime(d / "DS2SOFS0000.sl2", (stamp, stamp))
        loc = self.locate()
        ds2.export_pack(loc, self.work / "pack")
        packed = self.work / "pack" / "files" / "DS2SOFS0000.sl2"
        self.assertAlmostEqual(packed.stat().st_mtime, stamp, delta=2)


# ---------------------------------------------------------------- 包校验


class TestVerify(_TreeBase):
    def _export(self) -> Path:
        self.make_account_dir()
        pack = self.work / "pack"
        self.assertTrue(ds2.export_pack(self.locate(), pack).ok)
        return pack

    def test_intact_pack_verifies(self) -> None:
        ok, problems = ds2.verify_pack(self._export())
        self.assertTrue(ok, problems)

    def test_single_flipped_byte_is_caught(self) -> None:
        pack = self._export()
        victim = pack / "files" / "DS2SOFS0000.sl2"
        raw = bytearray(victim.read_bytes())
        raw[10] ^= 0x01
        victim.write_bytes(bytes(raw))
        ok, problems = ds2.verify_pack(pack)
        self.assertFalse(ok)
        self.assertTrue(any("sha256" in p for p in problems))

    def test_missing_payload_is_caught(self) -> None:
        pack = self._export()
        (pack / "files" / "DS2SOFS0000.co2").unlink()
        ok, problems = ds2.verify_pack(pack)
        self.assertFalse(ok)
        self.assertTrue(any("缺少文件" in p for p in problems))

    def test_missing_manifest_is_a_clear_error(self) -> None:
        pack = self.work / "pack"
        pack.mkdir()
        with self.assertRaises(ValueError) as ctx:
            ds2.read_pack(pack)
        self.assertIn(ds2.MANIFEST_NAME, str(ctx.exception))

    def test_wrong_appid_is_rejected(self) -> None:
        pack = self._export()
        m = json.loads((pack / ds2.MANIFEST_NAME).read_text("utf-8"))
        m["appid"] = "374320"
        (pack / ds2.MANIFEST_NAME).write_text(json.dumps(m), "utf-8")
        with self.assertRaises(ValueError):
            ds2.read_pack(pack)

    def test_unknown_schema_is_rejected(self) -> None:
        pack = self._export()
        m = json.loads((pack / ds2.MANIFEST_NAME).read_text("utf-8"))
        m["schema"] = "something-else/9"
        (pack / ds2.MANIFEST_NAME).write_text(json.dumps(m), "utf-8")
        with self.assertRaises(ValueError):
            ds2.read_pack(pack)


# ---------------------------------------------------------------- 导入


class TestImport(_TreeBase):
    def _pack(self) -> Path:
        self.make_account_dir()
        pack = self.work / "pack"
        self.assertTrue(ds2.export_pack(self.locate(), pack).ok)
        return pack

    def test_import_into_fresh_machine_creates_dir_and_matches_bytes(self) -> None:
        """换台新电脑: 那台机器从没玩过魂二, 存档根是空的 —— 这是主路径."""
        pack = self._pack()
        fresh = self.root / "another-machine"
        fresh.mkdir()
        target = ds2.locate(
            appdata_dir=fresh, accountid=FAKE_ACCT, create=True, with_files=False
        )
        res = ds2.import_pack(pack, target)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(res.written), 2)
        self.assertEqual(res.backup_dir, "")  # 本来就空, 无需备份
        self.assertTrue(target.dir.is_dir())
        for rel in res.written:
            self.assertEqual(
                ds2.sha256_of(target.dir / rel),
                ds2.sha256_of(pack / "files" / rel),
            )

    def test_import_backs_up_before_overwriting(self) -> None:
        pack = self._pack()
        target = self.make_account_dir()  # 目标已有文件
        (target / "DS2SOFS0000.sl2").write_bytes(_blob(b"OLD"))
        res = ds2.import_pack(pack, self.locate(with_files=False))
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.backup_dir)
        backup = Path(res.backup_dir)
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "DS2SOFS0000.sl2").read_bytes(), _blob(b"OLD"))

    def test_backup_failure_aborts_before_any_write(self) -> None:
        """备份失败必须中止 —— 没有退路的覆盖就是在赌用户的存档."""
        pack = self._pack()
        target = self.make_account_dir()
        victim = target / "DS2SOFS0000.sl2"
        original = victim.read_bytes()
        before = dict((p.name, p.read_bytes()) for p in target.iterdir())
        with mock.patch.object(ds2, "_backup_dir_now", side_effect=OSError("没权限")):
            res = ds2.import_pack(pack, self.locate(with_files=False))
        self.assertFalse(res.ok)
        self.assertIn("BACKUP_FAILED", res.blockers)
        self.assertEqual(victim.read_bytes(), original)
        after = dict((p.name, p.read_bytes()) for p in target.iterdir())
        self.assertEqual(before, after)

    def test_cross_account_is_blocked(self) -> None:
        """跨账号导入既违规又可能触发封号 —— 必须硬拦."""
        pack = self._pack()
        self.make_account_dir(OTHER_HEX)
        target = self.locate(accountid=OTHER_ACCT, with_files=False)
        res = ds2.import_pack(pack, target)
        self.assertFalse(res.ok)
        self.assertIn("CROSS_ACCOUNT", res.blockers)
        m = json.loads((pack / ds2.MANIFEST_NAME).read_text("utf-8"))
        self.assertNotEqual(m["accountid"], OTHER_ACCT)

    def test_game_running_is_blocked(self) -> None:
        pack = self._pack()
        target = self.locate(with_files=False)
        with mock.patch.dict(os.environ, {"SCB_GAME_RUNNING": "1"}):
            res = ds2.import_pack(pack, target)
        self.assertFalse(res.ok)
        self.assertIn("GAME_RUNNING", res.blockers)

    def test_unknown_game_state_is_noted_but_not_blocked(self) -> None:
        pack = self._pack()
        with mock.patch.dict(os.environ, {"SCB_GAME_RUNNING": "0"}):
            res = ds2.import_pack(pack, self.locate(with_files=False), dry_run=True)
        self.assertTrue(res.ok)

    def test_dry_run_writes_nothing(self) -> None:
        pack = self._pack()
        target = self.make_account_dir()
        (target / "DS2SOFS0000.sl2").write_bytes(_blob(b"OLD"))
        res = ds2.import_pack(pack, self.locate(with_files=False), dry_run=True)
        self.assertTrue(res.ok)
        self.assertTrue(res.dry_run)
        self.assertEqual((target / "DS2SOFS0000.sl2").read_bytes(), _blob(b"OLD"))
        self.assertFalse(any(target.glob("*.scbtmp")))

    def test_import_does_not_delete_unlisted_files(self) -> None:
        """导入只覆盖清单里的文件. 目标目录多出来的东西一律不动 ——
        删文件从来不是回滚手段 (主项目的既有红线)."""
        pack = self._pack()
        target = self.make_account_dir()
        stray = target / "DS2SOFS0000.co2.bak"
        stray.write_bytes(_blob(b"keep me"))
        res = ds2.import_pack(pack, self.locate(with_files=False))
        self.assertTrue(res.ok, res.error)
        self.assertTrue(stray.is_file())
        self.assertEqual(stray.read_bytes(), _blob(b"keep me"))

    def test_no_temp_files_left_behind(self) -> None:
        pack = self._pack()
        target = self.locate(accountid=FAKE_ACCT, create=True, with_files=False)
        ds2.import_pack(pack, target)
        self.assertEqual(list(target.dir.glob("*.scbtmp")), [])

    def test_import_preserves_mtime(self) -> None:
        d = self.make_account_dir()
        stamp = time.time() - 3 * 86400
        os.utime(d / "DS2SOFS0000.sl2", (stamp, stamp))
        pack = self.work / "pack"
        ds2.export_pack(self.locate(), pack)
        target = self.locate(accountid=FAKE_ACCT, create=True, with_files=False)
        self.assertTrue(ds2.import_pack(pack, target).ok)
        self.assertAlmostEqual(
            (target.dir / "DS2SOFS0000.sl2").stat().st_mtime, stamp, delta=2
        )

    def test_manifest_with_path_separator_is_refused(self) -> None:
        """清单是外部输入 —— 里面的文件名带分隔符就是目录穿越尝试."""
        pack = self._pack()
        m = json.loads((pack / ds2.MANIFEST_NAME).read_text("utf-8"))
        m["files"][0]["rel"] = "../escaped.sl2"
        (pack / ds2.MANIFEST_NAME).write_text(json.dumps(m), "utf-8")
        target = self.locate(accountid=FAKE_ACCT, create=True, with_files=False)
        res = ds2.import_pack(pack, target)
        self.assertFalse(res.ok)
        self.assertFalse((self.appdata / "DarkSoulsII" / "escaped.sl2").exists())
        self.assertFalse((self.appdata / "DarkSoulsII" / FAKE_HEX / ".." / "escaped.sl2").exists())

    def test_tampered_pack_refuses_before_writing(self) -> None:
        pack = self._pack()
        victim = pack / "files" / "DS2SOFS0000.sl2"
        raw = bytearray(victim.read_bytes())
        raw[3] ^= 0xFF
        victim.write_bytes(bytes(raw))
        target = self.make_account_dir()
        original = (target / "DS2SOFS0000.sl2").read_bytes()
        res = ds2.import_pack(pack, self.locate(with_files=False))
        self.assertFalse(res.ok)
        self.assertEqual((target / "DS2SOFS0000.sl2").read_bytes(), original)


# ---------------------------------------------------------------- 守卫


class TestGameRunning(unittest.TestCase):
    def test_env_hook_pins_the_result(self) -> None:
        with mock.patch.dict(os.environ, {"SCB_GAME_RUNNING": "1"}):
            self.assertIs(ds2.game_running(), True)
        with mock.patch.dict(os.environ, {"SCB_GAME_RUNNING": "0"}):
            self.assertIs(ds2.game_running(), False)

    def test_missing_tasklist_yields_unknown_not_false(self) -> None:
        """判不出来时必须返回 None —— 返回 False 会被读成"确认没在跑"."""
        env = {k: v for k, v in os.environ.items() if k != "SCB_GAME_RUNNING"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(ds2.shutil, "which", return_value=None):
                self.assertIsNone(ds2.game_running())


if __name__ == "__main__":
    unittest.main()
