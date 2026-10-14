#!/usr/bin/env python3
"""machine_probe / gen_adapter / build_single_file 的回归测试.

重点锁住两类曾经出过的错:
  1. **脱敏不彻底**: 字符串值之外的整数型账号字段、字典键都曾漏网.
  2. **存档目录错误归属**: `is_current_account` 曾当置信度用, 导致 %APPDATA%
     下十几个游戏目录 (都叫同一个账号名) 互相张冠李戴 —— 例如 Stardew 被
     判成存档在 DarkSoulsIII\\<hex>. 现在只认"日志实锤 / 本游戏 remotecache /
     目录名精确匹配".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import gen_adapter as ga  # noqa: E402
import machine_probe as mp  # noqa: E402
from src.steam import accounts, cloud_log, discovery, remotecache  # noqa: E402

AID = 1234567890
HEX_DIR = accounts.accountid_to_hex_dir(AID)
SID64 = str(accounts.accountid_to_steamid64(AID))
# 用**真实**家目录做样本: 模板化替换只对 %USERPROFILE% 这类已知根生效,
# 拿一个假路径当样本会掩盖规则是否真的生效.
HOME = str(Path(os.path.expanduser("~")))
HOME_NAME = Path(HOME).name


def _ud_stub(tmp: Path, ac_top_dirs: list[str], has_remote: bool = False) -> dict:
    return {
        "appid": "413150",
        "path": str(tmp / "userdata" / str(AID) / "413150"),
        "has_remote_dir": has_remote,
        "has_ac_dir": bool(ac_top_dirs),
        "has_remotecache": False,
        "remote_files": 0,
        "remote_bytes": 0,
        "ac_top_dirs": ac_top_dirs,
        "cache": None,
    }


def _forms() -> dict[str, str]:
    return {
        HEX_DIR.lower(): "steamid64-hex",
        SID64: "steamid64-decimal",
        str(AID): "accountid-decimal",
    }


class TestRedaction(unittest.TestCase):
    """脱敏必须覆盖: 字符串值 / 整数型字段 / 字典键."""

    def _profile(self) -> dict:
        return {
            "tool": {"name": "machine_probe", "version": "0.1.1", "redacted": False},
            "os": {"system": "Windows"},
            "games": [
                {
                    "appid": "374320",
                    "install_dir": r"D:\Steam\steamapps\common\DARK SOULS III",
                    "save_dir_resolved": rf"{HOME}\AppData\Roaming\DarkSoulsIII\{HEX_DIR}",
                }
            ],
            "steam": {
                "primary_root": r"C:\Program Files (x86)\Steam",
                "installs": [{"root": r"C:\Program Files (x86)\Steam"}],
                "libraries": [r"C:\Program Files (x86)\Steam\steamapps"],
                "active_accountid": AID,
                "scanned_accountids": [str(AID)],
                "account_ident_strings": discovery.account_ident_strings(AID),
                "accounts_known": [
                    {
                        "steamid64": SID64,
                        "account_id": AID,
                        "account_id_hex_dir": HEX_DIR,
                        "account_name": "myname",
                        "persona_name": "My Persona",
                        "timestamp": 1700000000,
                    }
                ],
                "userdata_apps_by_account": {str(AID): ["374320"]},
            },
        }

    def test_no_identity_leaks(self) -> None:
        red = mp.redact(self._profile())
        blob = json.dumps(red, ensure_ascii=False)

        for secret in (str(AID), SID64, HEX_DIR, "myname", "My Persona", "7656"):
            self.assertNotIn(secret, blob, f"脱敏遗漏: {secret}")
        self.assertNotIn(HEX_DIR, json.dumps(red["games"]))

    def test_absolute_paths_templated(self) -> None:
        red = mp.redact(self._profile())
        self.assertEqual(red["steam"]["primary_root"], "%STEAM_ROOT%")
        self.assertEqual(red["games"][0]["install_dir"], "%GAME_DIR_374320%")
        self.assertIn("%USERPROFILE%", red["games"][0]["save_dir_resolved"])
        self.assertEqual(red["tool"]["redacted"], True)

    def test_shared_install_dir_keeps_own_label(self) -> None:
        """D6 回归: 多个 appid 共用同一物理安装目录时, 各自标签不许串.

        实例: `70` Half-Life 与 `130` Half-Life: Blue Shift 都装在
        `steamapps\\common\\Half-Life` 下. 旧实现里脱敏表用 set() 去重,
        同长度的两条替换项先后随机, 结果 70 的 install_dir 会被写成
        `%GAME_DIR_130%`.
        """
        prof = self._profile()
        shared = r"D:\Steam\steamapps\common\Half-Life"
        prof["games"] = [
            {
                "appid": "70",
                "install_dir": shared,
                "save_dir_resolved": rf"{shared}\valve\SAVE",
            },
            {
                "appid": "130",
                "install_dir": shared,
                "save_dir_resolved": rf"{shared}\bshift\SAVE",
            },
        ]
        red = mp.redact(prof)
        by_id = {g["appid"]: g for g in red["games"]}
        self.assertEqual(by_id["70"]["install_dir"], "%GAME_DIR_70%")
        self.assertEqual(by_id["130"]["install_dir"], "%GAME_DIR_130%")
        self.assertTrue(by_id["70"]["save_dir_resolved"].startswith("%GAME_DIR_70%"))
        self.assertTrue(by_id["130"]["save_dir_resolved"].startswith("%GAME_DIR_130%"))

    def test_redaction_is_deterministic(self) -> None:
        """回归: 同一输入反复脱敏必须字节一致 (set() 曾让同长度替换项先后随机)."""
        prof = self._profile()
        shared = r"D:\Steam\steamapps\common\Half-Life"
        prof["games"] = [
            {"appid": "70", "install_dir": shared},
            {"appid": "130", "install_dir": shared},
        ]
        outs = {
            json.dumps(mp.redact(prof), ensure_ascii=False, sort_keys=True)
            for _ in range(8)
        }
        self.assertEqual(len(outs), 1, "脱敏结果不稳定")

    def test_dict_keys_and_int_fields_redacted(self) -> None:
        red = mp.redact(self._profile())
        self.assertNotIn(str(AID), red["steam"]["userdata_apps_by_account"])
        self.assertNotIn(str(AID), json.dumps(red["steam"]["active_accountid"]))
        self.assertIsInstance(red["steam"]["active_accountid"], str)

    def test_three_forms_share_one_hash_but_keep_form(self) -> None:
        """同一账号的三种目录写法必须归一到同一哈希, 同时保留"用了哪种写法"."""
        red = mp.redact(self._profile())
        strings = red["steam"]["account_ident_strings"]
        self.assertEqual(len(strings), 3)
        self.assertEqual(len({s.split(":")[0] for s in strings}), 1)
        self.assertEqual(
            len({s.split(":")[-1].rstrip(">") for s in strings}), 3, "应保留三种形态后缀"
        )

    def test_drop_identity_fields(self) -> None:
        red = mp.redact(self._profile())
        acc = red["steam"]["accounts_known"][0]
        self.assertIsNone(acc["account_name"])
        self.assertIsNone(acc["persona_name"])
        self.assertIsNone(acc["steamid64"])
        self.assertEqual(acc["timestamp"], 0)

    def test_accountid_hex_short_form_in_filenames(self) -> None:
        """回归: Steam 把 accountid 的 8 位 hex 编进文件名.

        实例: 手柄配置 `preferences_45e-2ff-499602d2.vdf`.
        只替换 16 位 sidhex 整串会漏掉其中的 `499602d2`, 而它反推一步就是 accountid.
        """
        prof = self._profile()
        short = f"{AID:08x}"
        prof["games"][0]["persisted_files"] = [
            rf"{HOME}\AppData\Roaming\DarkSoulsIII\{HEX_DIR}\DS30000.sl2",
            r"C:\Program Files (x86)\Steam\steamapps\common\Steam Controller Configs"
            rf"\{AID}\config\preferences_45e-2ff-{short}.vdf",
        ]
        blob = json.dumps(mp.redact(prof), ensure_ascii=False)
        self.assertNotIn(short, blob, "8 位 hex 短形式泄漏")
        self.assertNotIn(short.upper(), blob, "8 位 hex 大写形式泄漏")
        self.assertIn("accountid-hex", blob)

    def test_short_hex_uses_same_account_hash(self) -> None:
        """短形式必须归到与整串相同的账号哈希, 否则等于自曝"这两者同源"."""
        prof = self._profile()
        prof["games"][0]["persisted_files"] = [f"cfg/prefs-{AID:08x}.vdf"]
        red = mp.redact(prof)
        out = red["games"][0]["persisted_files"][0]
        full_hash = red["steam"]["account_ident_strings"][0].split(":")[0]
        self.assertIn(full_hash, out)


class TestSaveDirResolution(unittest.TestCase):
    """存档目录定位的置信规则 —— 这一块曾把 DS3 的目录算成 Stardew 的存档."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.appdata = self.tmp / "appdata"
        self.ds3_dir = self.appdata / "DarkSoulsIII" / HEX_DIR
        self.ds3_dir.mkdir(parents=True)
        self.stardew_dir = self.appdata / "StardewValley"
        self.stardew_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _probe(self, name: str, entry: cloud_log.CloudLogEntry, ud: dict):
        manifest = discovery.AppManifest(
            appid="413150", name=name, installdir=name, library=None
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)}):
            return mp.probe_game(
                "413150", manifest, ud, entry, None,
                discovery.account_ident_strings(AID), _forms(), {},
            )

    def test_shared_account_dir_only_confirms_on_name_match(self) -> None:
        entry = cloud_log.CloudLogEntry(appid="413150")  # 无 Watching 记录
        ud = _ud_stub(self.tmp, ["WinAppDataRoaming"])

        # 名字对得上 -> 确认, 命中 DarkSoulsIII\<hex>
        ok = self._probe("DARK SOULS III", entry, ud)
        self.assertTrue(ok["save_dir_confident"])
        self.assertEqual(ok["save_dir_resolved"], str(self.ds3_dir))
        self.assertEqual(ok["account_dir_form"], "steamid64-hex")

        # 名字对不上 (Stardew) -> **必须不能**把 DS3 的目录当成它的存档
        bad = self._probe("Stardew Valley", entry, ud)
        self.assertFalse(bad["save_dir_confident"])
        self.assertIsNone(bad["save_dir_resolved"])

    def test_cloud_log_watching_beats_ac_root(self) -> None:
        watched_dir = self.tmp / "watched" / "SomeGame"
        watched_dir.mkdir(parents=True)
        watched_file = watched_dir / "save.sav"
        watched_file.write_bytes(b"x")

        entry = cloud_log.CloudLogEntry(appid="413150", watching_files=[str(watched_file)])
        ud = _ud_stub(self.tmp, ["WinAppDataRoaming"])

        g = self._probe("Totally Unrelated Title", entry, ud)
        self.assertEqual(g["save_dir_resolved"], str(watched_dir))
        self.assertEqual(g["save_dir_source"], "cloud_log")
        self.assertTrue(g["save_dir_confident"])
        self.assertEqual(g["watched_file_names"], ["save.sav"])
        self.assertEqual(g["watched_file_state"][0]["exists"], True)

    def test_remote_dir_is_confirmed(self) -> None:
        remote = self.tmp / "userdata" / str(AID) / "413150" / "remote"
        remote.mkdir(parents=True)
        (remote / "a.dat").write_bytes(b"y")
        ud = _ud_stub(self.tmp, [], has_remote=True)
        entry = cloud_log.CloudLogEntry(appid="413150")

        g = self._probe("Whatever", entry, ud)
        self.assertEqual(g["save_dir_resolved"], str(remote))
        self.assertEqual(g["save_dir_source"], "remote-dir")

    def test_remotecache_entry_confirms(self) -> None:
        """remotecache 属于该 appid 自己, 据此推出且存在的路径应直接确认."""
        save_dir = self.appdata / "SomeGame" / "Saves"
        save_dir.mkdir(parents=True)
        (save_dir / "slot1.sav").write_bytes(b"z")

        ud = _ud_stub(self.tmp, [])
        ud["has_remotecache"] = True
        ud["cache"] = {
            "entries": [{"root": "4", "name": r"SomeGame\Saves\slot1.sav"}]
        }
        entry = cloud_log.CloudLogEntry(appid="413150")
        manifest = discovery.AppManifest(
            appid="413150", name="Some Game", installdir="SomeGame", library=None
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)}):
            g = mp.probe_game(
                "413150", manifest, ud, entry, None,
                discovery.account_ident_strings(AID), _forms(),
                {"4": self.appdata},
            )
        self.assertEqual(g["save_dir_resolved"], str(save_dir))
        self.assertEqual(g["save_dir_source"], "remotecache")


class TestSaveDirHardening(unittest.TestCase):
    """朋友机回传报告暴露的 4 个缺陷的回归锁.

    D1 `root_base_paths` 从未传进 `probe_game` (非 0/1 号 root 静默失效)
    D2 `steam_autocloud.vdf` 只用于选目标, 不参与机制判定与存档定位
    D3 存档目录被解析成游戏安装目录 / 同级别里父目录压过 `...\\SAVE`
    D4 候选池漏掉"只有云端变更号已推进"这一条证据的游戏
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.lib = self.tmp / "SteamLibrary"
        self.inst = self.lib / "steamapps" / "common" / "Half-Life"
        self.inst.mkdir(parents=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _manifest(self, appid: str, installdir: str, name: str = "Some Game"):
        return discovery.AppManifest(
            appid=appid, name=name, installdir=installdir, library=self.lib
        )

    def _probe(self, appid, manifest, ud, entry, root_base_paths=None, markers=None):
        return mp.probe_game(
            appid, manifest, ud, entry, None,
            discovery.account_ident_strings(AID), _forms(),
            root_base_paths=root_base_paths, markers=markers or [],
        )

    # ---- D2 ----

    def test_autocloud_marker_resolves_when_nothing_else_does(self) -> None:
        """D2: 只有 marker 一条线索的游戏也必须能定位, 且机制判为 auto-cloud.

        实测样本: 692850 Bloodstained 有
        `<GameDir>\\BloodstainedRotN\\Saved\\SaveGames\\steam_autocloud.vdf`,
        却因为 marker 没进候选而报"未能定位存档目录 / 机制未知".
        """
        save = self.inst / "valve" / "SAVE"
        save.mkdir(parents=True)
        marker = save / "steam_autocloud.vdf"
        marker.write_text("x", encoding="utf-8")

        g = self._probe(
            "10", self._manifest("10", "Half-Life", "Counter-Strike"),
            None, None, markers=[str(marker)],
        )
        self.assertEqual(g["save_dir_resolved"], str(save))
        self.assertEqual(g["save_dir_source"], "autocloud-marker")
        self.assertTrue(g["save_dir_confident"])
        self.assertEqual(g["mechanism"], "auto-cloud")

    def test_marker_outside_own_install_dir_is_ignored(self) -> None:
        """D2 护栏: 归属函数曾把 `Half-Life 2\\hl2\\save` 算成 install-dir 为
        `Half-Life` 的那个 appid (纯字符串前缀命中). marker 必须落回自己的安装目录."""
        other = self.lib / "steamapps" / "common" / "Half-Life 2" / "hl2" / "save"
        other.mkdir(parents=True)
        marker = other / "steam_autocloud.vdf"
        marker.write_text("x", encoding="utf-8")

        g = self._probe(
            "10", self._manifest("10", "Half-Life", "Counter-Strike"),
            None, None, markers=[str(marker)],
        )
        self.assertEqual(g["autocloud_marker_dirs"], [])
        self.assertIsNone(g["save_dir_resolved"])

    def test_marker_does_not_override_remote_storage_classification(self) -> None:
        """D2 刻意的"不夺权": 同时有 remote 目录与 marker 的 API 型游戏不能改判."""
        ud = _ud_stub(self.tmp, [], has_remote=True)
        remote = Path(ud["path"]) / "remote"
        remote.mkdir(parents=True)
        marker_dir = self.inst / "valve"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / "steam_autocloud.vdf"
        marker.write_text("x", encoding="utf-8")

        g = self._probe(
            "10", self._manifest("10", "Half-Life", "Counter-Strike"),
            ud, None, markers=[str(marker)],
        )
        self.assertEqual(g["mechanism"], "remote-storage-api")
        self.assertEqual(g["save_dir_resolved"], str(remote))
        self.assertEqual(g["save_dir_source"], "remote-dir")

    # ---- D3 ----

    def test_install_root_is_rejected(self) -> None:
        """D3: 全部候选都退回到安装根时必须拒绝.

        实测样本: 113200 (The Binding Of Isaac) / 4659620 都被解析成安装目录本身,
        交给 M2 会去复制整个游戏目录.
        """
        (self.inst / "slot1.sav").write_bytes(b"z")
        ud = _ud_stub(self.tmp, [])
        ud["has_remotecache"] = True
        ud["cache"] = {"entries": [{"root": "1", "name": "slot1.sav"}]}

        g = self._probe(
            "113200", self._manifest("113200", "Half-Life", "ISAAC"), ud, None
        )
        self.assertIsNone(g["save_dir_resolved"])
        self.assertFalse(g["save_dir_confident"])
        self.assertTrue(g["save_dir_rejected"])

    def test_save_like_sibling_beats_its_parent(self) -> None:
        """D3: 同级别候选里 `...\\SAVE` 必须压过父目录 `...\\bshift`.

        实测样本: 130 Half-Life: Blue Shift 被判成 `...\\bshift`, 快照枚举出
        3430 个 .wav/.mdl/.dll (游戏资源目录), 而不是 `...\\bshift\\SAVE`.
        """
        base = self.inst / "bshift"
        (base / "SAVE").mkdir(parents=True)
        (base / "SAVE" / "slot.sav").write_bytes(b"z")
        (base / "slot2.sav").write_bytes(b"z")

        ud = _ud_stub(self.tmp, [])
        ud["has_remotecache"] = True
        ud["cache"] = {
            "entries": [
                {"root": "1", "name": r"bshift\SAVE\slot.sav"},
                {"root": "1", "name": r"bshift\slot2.sav"},
            ]
        }
        g = self._probe(
            "130", self._manifest("130", "Half-Life", "Half-Life: Blue Shift"), ud, None
        )
        self.assertEqual(g["save_dir_resolved"], str(base / "SAVE"))

    # ---- D1 ----

    def test_calibrated_root_bases_excludes_per_appid_and_low_confidence(self) -> None:
        """D1: 只有**全局**且**非低置信**的标定才能回灌给逐游戏探测."""
        base = self.tmp / "AppDataLocal"
        base.mkdir(parents=True)
        cal = {
            "0": remotecache.RootCalibration(
                "0", "<SteamRemote>", "(per-appid, 例: <remote>)", 132, 132
            ),
            "1": remotecache.RootCalibration(
                "1", "<GameInstallDir>", "(per-appid, 例: <install>)", 53, 53
            ),
            "3": remotecache.RootCalibration("3", "%LOCALAPPDATA%", str(base), 6, 6),
            "14": remotecache.RootCalibration("14", "%APPDATA%", str(base), 0, 2),
        }
        got = mp.calibrated_root_bases(cal)
        self.assertEqual(set(got), {"3"})
        self.assertEqual(got["3"], base)

    # ---- D4 ----

    def test_select_targets_includes_change_number_only_game(self) -> None:
        """D4: 只有 `synced_change_number` 证据的游戏必须进候选池 (ELDEN RING)."""
        man = {"1245620": self._manifest("1245620", "ELDEN RING", "ELDEN RING")}
        entries = {
            "1245620": cloud_log.CloudLogEntry(appid="1245620", synced_change_number=23)
        }
        self.assertIn("1245620", mp.select_targets(man, {}, {}, entries))

    def test_select_targets_keeps_uninstalled_game(self) -> None:
        """D4: 已卸载游戏 (无 appmanifest) 仍应被探测 —— 与解析器注释一致."""
        entries = {
            "1245620": cloud_log.CloudLogEntry(appid="1245620", synced_change_number=3)
        }
        self.assertIn("1245620", mp.select_targets({}, {}, {}, entries))

    def test_select_targets_skips_zero_change_number_and_steam_internals(self) -> None:
        """D4: `ChangeNumber=0` 不算证据; Steam 自身 (7 / 241100) 不进候选池."""
        man = {"2371090": self._manifest("2371090", "X", "X")}
        entries = {
            "2371090": cloud_log.CloudLogEntry(appid="2371090", synced_change_number=0),
            "7": cloud_log.CloudLogEntry(appid="7", synced_change_number=5),
            "241100": cloud_log.CloudLogEntry(appid="241100", synced_change_number=75),
        }
        self.assertEqual(mp.select_targets(man, {}, {}, entries), [])

    def test_marker_attribution_respects_path_boundary(self) -> None:
        """marker 归属必须是**路径级**前缀匹配.

        早先用字符串前缀, 于是 `common/Half-Life 2/hl2/save/...` 被算到 install-dir
        为 `Half-Life` 的那个 appid 头上. 本机实测被算到 appid 10 (Counter-Strike).
        """
        p = (
            self.lib / "steamapps" / "common" / "Half-Life 2" / "hl2" / "save"
            / "steam_autocloud.vdf"
        )
        both = {
            "10": self._manifest("10", "Half-Life", "Counter-Strike"),
            "220": self._manifest("220", "Half-Life 2", "Half-Life 2"),
        }
        self.assertEqual(mp.appid_from_path(p, both), "220")
        only_hl1 = {"10": self._manifest("10", "Half-Life", "Counter-Strike")}
        self.assertEqual(mp.appid_from_path(p, only_hl1), "unknown")


class TestRemoteUploadVerdict(unittest.TestCase):
    """上传往返判据: `remotecache` 的 `remotetime` 必须覆盖 cloud_log 的弱判定.

    背景 (2026-10-05 实测): Auto-Cloud 的**上传**方向不打印 `Successfully synced`,
    连确认已上云的对照游戏 241100 在 cloud_log 层都会被判成 queued_not_uploaded.
    所以"到底有没有上云"必须以 `remotecache` 的 `remotetime` 为准 (remotetime>0).
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.appdata = self.tmp / "AppData" / "Roaming"
        # DS3 真实落点: %APPDATA%\DarkSoulsIII\<steamid64-hex>\DS30000.sl2
        self.save_dir = self.appdata / "DarkSoulsIII" / HEX_DIR
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "DS30000.sl2").write_bytes(b"save")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _probe(self, cache: Any, entry: cloud_log.CloudLogEntry) -> dict:
        ud = _ud_stub(self.tmp, [])
        ud["has_remotecache"] = True
        ud["cache"] = cache
        manifest = discovery.AppManifest(
            appid="374320", name="DARK SOULS III",
            installdir="DARK SOULS III", library=None,
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)}):
            return mp.probe_game(
                "374320", manifest, ud, entry, None,
                discovery.account_ident_strings(AID), _forms(), {"4": self.appdata},
            )

    def test_remotetime_zero_overrides_optimistic_log(self) -> None:
        """日志层给了 synced_change_number (看似往返成功), 但 remotetime=0 → 判为未上传."""
        entry = cloud_log.CloudLogEntry(appid="374320")
        entry.synced_change_number = 9  # 日志层面"看起来"有往返
        entry.persisted_files = [r"C:\x\DS30000.sl2"]
        rel = f"DarkSoulsIII\\{HEX_DIR}\\DS30000.sl2"
        cache = {
            "entries": [{
                "root": "4", "name": rel,
                "remotetime": 0, "localtime": 1789916991, "sha": "abc",
            }],
            "upload_verified": False, "max_remotetime": 0,
            "pending_upload": [rel], "file_count": 1,
        }
        g = self._probe(cache, entry)
        self.assertFalse(g["cloud_roundtrip_ok"])
        self.assertTrue(g["queued_not_uploaded"])
        self.assertEqual(g["remote_upload"]["source"], "remotecache.vdf")

    def test_remotetime_positive_means_uploaded(self) -> None:
        """remotetime > 0 = 云端确有副本, 即使日志层什么都没说."""
        entry = cloud_log.CloudLogEntry(appid="374320")  # 日志层无任何往返证据
        rel = f"DarkSoulsIII\\{HEX_DIR}\\DS30000.sl2"
        cache = {
            "entries": [{
                "root": "4", "name": rel,
                "remotetime": 1598232224, "localtime": 1598232224, "sha": "z",
            }],
            "upload_verified": True, "max_remotetime": 1598232224,
            "pending_upload": [], "file_count": 1,
        }
        g = self._probe(cache, entry)
        self.assertTrue(g["cloud_roundtrip_ok"])
        self.assertFalse(g["queued_not_uploaded"])

    def test_remotecache_change_number_overrides_log_progress(self) -> None:
        """云端变更号 > 0 时, "曾推进过"必须以 remotecache 为准.

        2026-10-11 21:33 DS3 **首次**上传成功. 但 cloud_log 里最后一条
        `Need to sync from local change number '0' to global change number '0'`
        打印在上传**之前**, 只看日志会把刚传成功的游戏报成 `曾推进过=False`,
        与同一份报告里"云端已存在副本"自相矛盾.
        """
        entry = cloud_log.CloudLogEntry(appid="374320")
        entry.change_numbers = [(0, 0)]        # 日志层: 全 0
        rel = f"DarkSoulsIII\\{HEX_DIR}\\DS30000.sl2"
        cache = {
            "entries": [{
                "root": "4", "name": rel,
                "remotetime": 1791725553, "localtime": 1791725554, "sha": "ec4d2bd1",
            }],
            "upload_verified": True, "max_remotetime": 1791725553,
            "pending_upload": [], "file_count": 1,
            "change_number": 1, "ever_advanced": True,
        }
        g = self._probe(cache, entry)
        self.assertTrue(g["ever_advanced"])
        self.assertFalse(g["no_change_advance"])
        self.assertFalse(g["never_synced_strong"])
        self.assertEqual(g["remote_upload"]["change_number"], 1)
        self.assertTrue(g["remote_upload"]["verified"])
        self.assertTrue(g["cloud_roundtrip_ok"])

    def test_no_remotecache_keeps_log_verdict(self) -> None:
        """没有 remotecache 时不覆盖, 保留日志层判定."""
        entry = cloud_log.CloudLogEntry(appid="374320")
        entry.persisted_files = [r"C:\x\DS30000.sl2"]
        ud = _ud_stub(self.tmp, [])
        manifest = discovery.AppManifest(
            appid="374320", name="DARK SOULS III",
            installdir="DARK SOULS III", library=None,
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)}):
            g = mp.probe_game(
                "374320", manifest, ud, entry, None,
                discovery.account_ident_strings(AID), _forms(), {},
            )
        self.assertIsNone(g["remote_upload"])
        self.assertTrue(g["queued_not_uploaded"])  # 日志层判定保留


class TestCloudBlockerLayers(unittest.TestCase):
    """两层云开关诊断: 游戏级(易失) 优先于 账号级(持久)."""

    def _game(self, **kw) -> dict:
        base = {
            "appid": "374320",
            "mechanism": "auto-cloud",
            "mechanisms": ["auto-cloud"],
            "cloud_state": "unknown",
            "cloud_toggle": {"needed": False, "command": None},
            "queued_not_uploaded": False,
            "upload_initiated": False,
            "remote_upload": None,
        }
        base.update(kw)
        return base

    def test_game_toggle_takes_priority(self) -> None:
        """即使整机上传是活的, 本游戏开关关着仍报游戏级阻塞."""
        games = [
            self._game(cloud_state="disabled",
                       cloud_toggle={"needed": True, "command": "testappcloudpaths 374320"}),
            self._game(appid="105600", upload_initiated=True, mechanism="remote-storage-api",
                       mechanisms=["remote-storage-api"]),
        ]
        alive = mp.compute_cloud_blockers(games)
        self.assertTrue(alive)
        self.assertEqual(games[0]["cloud_blocker"]["code"], "game-toggle-off")
        self.assertIsNone(games[1]["cloud_blocker"])

    def test_account_layer_when_nothing_uploads(self) -> None:
        """整机没有任何上传批次时, 未上云的游戏判为账号级阻塞."""
        games = [self._game(queued_not_uploaded=True)]
        alive = mp.compute_cloud_blockers(games)
        self.assertFalse(alive)
        self.assertEqual(games[0]["cloud_blocker"]["layer"], "account")

    def test_pending_full_sync_when_account_alive(self) -> None:
        """账号级已通、游戏级也没关, 但云端仍无副本 → 待首次全量上传."""
        games = [
            self._game(queued_not_uploaded=True),
            self._game(appid="105600", upload_initiated=True, mechanism="remote-storage-api",
                       mechanisms=["remote-storage-api"]),
        ]
        mp.compute_cloud_blockers(games)
        self.assertEqual(games[0]["cloud_blocker"]["code"], "pending-full-sync")

    def test_healthy_game_has_no_blocker(self) -> None:
        games = [self._game(remote_upload={"verified": True, "max_remotetime": 1791722829})]
        mp.compute_cloud_blockers(games)
        self.assertIsNone(games[0]["cloud_blocker"])

    # --- 账号级判据必须整机级 (2026-10-11 实测修正) ---

    class _FakeEntry:
        """最小化 CloudLogEntry 替身: 只暴露账号级判定用到的两个字段."""

        def __init__(self, upload_batches=None) -> None:
            self.upload_batches = list(upload_batches or [])

        @property
        def upload_initiated(self) -> bool:
            return bool(self.upload_batches)

    def test_account_alive_uses_machine_wide_entries(self) -> None:
        """只探测一个游戏时, 账号级判据必须看整机 cloud_log, 不能看探测子集.

        2026-10-11 实测: 账号级已通 (当天另有 11 个游戏发起过上传批次),
        但 `--appid 374320` 只探测 DS3 一个游戏. 旧逻辑只看探测子集 ⇒
        把 DS3 误报成 `account-cloud-off`, 误导用户去改一个本来就正常的开关.
        """
        games = [self._game(queued_not_uploaded=True)]   # DS3 自身从未发起上传
        entries = {
            "374320": self._FakeEntry(),
            "105600": self._FakeEntry([(1352431366534545472, 6)]),
        }
        alive = mp.compute_cloud_blockers(games, entries)
        self.assertTrue(alive)
        self.assertEqual(games[0]["cloud_blocker"]["code"], "pending-full-sync")

    def test_appid7_upload_does_not_count_as_account_alive(self) -> None:
        """Steam 自身 (AppID 7) 的上传不算账号级存活.

        AppID 7 的云 (控制器配置/截图) 与「游戏云同步」开关不是同一条链路:
        2026-10-05 实测账号级停摆期间 AppID 7 仍在上传. 不排除它就会漏判停摆.
        """
        games = [self._game(queued_not_uploaded=True)]
        entries = {"7": self._FakeEntry([(5655103410381653414, 1423)])}
        alive = mp.compute_cloud_blockers(games, entries)
        self.assertFalse(alive)
        self.assertEqual(games[0]["cloud_blocker"]["layer"], "account")
        self.assertEqual(mp.machine_upload_initiated_appids(entries), [])

    def test_machine_upload_initiated_appids_excludes_steam_itself(self) -> None:
        entries = {
            "7": self._FakeEntry([(1, 1)]),
            "374320": self._FakeEntry([(2, 1)]),
            "105600": self._FakeEntry(),
        }
        self.assertEqual(mp.machine_upload_initiated_appids(entries), ["374320"])


class TestSaveDirReasonPointsAtRootCause(unittest.TestCase):
    """云通道未点亮时, "未能定位存档目录" 不能把用户指向 `--all-accounts`.

    2026-10-12 实测 (`374320` DARK SOULS III, 朋友机): 未点亮的游戏在本机
    **连 `userdata\\<acct>\\<appid>` 目录都不存在**, 存档目录的三个来源
    (日志 `Watching file` / `remotecache` / `ac` 目录) 同时为空.
    此时换账号 (`--all-accounts`) 也扫不出一个根本不存在的目录 ——
    旧文案把用户往那条路上引, 而真正的解法是 `testappcloudpaths <appid>` + 启动一次游戏.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.lib = self.tmp / "SteamLibrary"
        self.inst = self.lib / "steamapps" / "common" / "DARK SOULS III"
        self.inst.mkdir(parents=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _probe(self, entry):
        manifest = discovery.AppManifest(
            appid="374320", name="DARK SOULS III",
            installdir="DARK SOULS III", library=self.lib,
        )
        return mp.probe_game(
            "374320", manifest, None, entry, None,
            discovery.account_ident_strings(AID), _forms(),
        )

    def test_disabled_channel_points_at_toggle_not_all_accounts(self) -> None:
        entry = cloud_log.CloudLogEntry(appid="374320", disabled_count=2)
        g = self._probe(entry)

        self.assertEqual(g["cloud_state"], "disabled")
        self.assertIsNone(g["save_dir_resolved"])
        self.assertFalse(g["verdict"]["bridgeable"])

        reasons = g["verdict"]["reasons"]
        # 根因必须在最前 —— 面客报告按此顺序渲染, 用户先读到的那条就是该做的动作.
        self.assertIn("testappcloudpaths 374320", reasons[0])
        joined = " ".join(reasons)
        self.assertNotIn("--all-accounts", joined)
        self.assertIn("云通道未点亮", joined)

    def test_unknown_channel_keeps_generic_hint(self) -> None:
        """没有禁用证据时 (例如只探测到一个空壳 userdata), 原提示仍然适用."""
        entry = cloud_log.CloudLogEntry(appid="374320")
        g = self._probe(entry)

        self.assertNotEqual(g["cloud_state"], "disabled")
        joined = " ".join(g["verdict"]["reasons"])
        self.assertIn("--all-accounts", joined)


class TestCloudToggleConsoleEvidence(unittest.TestCase):
    """`testappcloudpaths` 的**执行回执只写 console_log**, 不写 cloud_log.

    2026-10-13 实测卡点: 朋友机 DS3 的 `AutoCloud is disabled` 从 2 次涨到 4 次
    (Steam 又评估了两次), 但报告只有 cloud_log, 于是"点亮命令到底执行过没有"
    无从回答 —— 整轮分析只能靠猜. 本组锁住两种可操作情形各自的判定文案:
    命令没执行过 vs 执行过但之后仍无活动 (多为执行后又重启了 Steam).
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.lib = self.tmp / "SteamLibrary"
        (self.lib / "steamapps" / "common" / "DARK SOULS III").mkdir(parents=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _probe(self, entry, console_lines=None):
        manifest = discovery.AppManifest(
            appid="374320", name="DARK SOULS III",
            installdir="DARK SOULS III", library=self.lib,
        )
        return mp.probe_game(
            "374320", manifest, None, entry, None,
            discovery.account_ident_strings(AID), _forms(),
            console_lines=console_lines,
        )

    def test_ack_absent_reports_command_never_ran(self) -> None:
        """有 console_log、但没有回执 ⇒ 命令从未在本机执行过."""
        entry = cloud_log.CloudLogEntry(appid="374320", disabled_count=4)
        lines = [
            "[2026-10-13 00:05:00] Console Log Start",
            "[2026-10-13 00:06:00] find cloud",
        ]
        g = self._probe(entry, console_lines=lines)

        ack = g["cloud_toggle_console"]
        self.assertIsNotNone(ack)
        self.assertFalse(ack["found"])
        self.assertEqual(ack["count"], 0)

        first = g["verdict"]["reasons"][0]
        self.assertIn("testappcloudpaths 374320", first)
        self.assertIn("**没有**", first)
        self.assertIn("执行回执", first)
        self.assertNotIn("--all-accounts", " ".join(g["verdict"]["reasons"]))

    def test_ack_present_points_at_restart_trap(self) -> None:
        """有回执但仍未点亮 ⇒ 指出"执行后重启 Steam 会清掉"这个坑, 并给正确顺序."""
        entry = cloud_log.CloudLogEntry(appid="374320", disabled_count=4)
        lines = [
            "[2026-10-13 00:05:00] Console Log Start",
            '[2026-10-13 00:06:12] "testappcloudpaths" = "374320"',
        ]
        g = self._probe(entry, console_lines=lines)

        ack = g["cloud_toggle_console"]
        self.assertTrue(ack["found"])
        self.assertEqual(ack["count"], 1)
        self.assertTrue(ack["in_session"])

        first = g["verdict"]["reasons"][0]
        self.assertIn("**有**", first)
        self.assertIn("重启", first)
        self.assertIn("同一个", first)   # 正确顺序那句
        self.assertNotIn("--all-accounts", " ".join(g["verdict"]["reasons"]))

    def test_no_console_log_keeps_generic_wording(self) -> None:
        """拿不到 console_log 时不能假装知道 —— 字段留 `None`, 文案退回通用版."""
        entry = cloud_log.CloudLogEntry(appid="374320", disabled_count=4)
        g = self._probe(entry, console_lines=None)

        self.assertIsNone(g["cloud_toggle_console"])
        first = g["verdict"]["reasons"][0]
        self.assertIn("testappcloudpaths 374320", first)
        self.assertIn("无 console_log", first)
        self.assertNotIn("**有**", first)
        self.assertNotIn("**没有**", first)

    def test_var_value_decides_toggle_state(self) -> None:
        """控制台变量 `testappcloudpaths` 的**值** = 现在点亮了没有.

        2026-10-13 实测卡点: 朋友在控制台敲完 `testappcloudpaths 374320` 反馈
        "什么都没返回"。该命令成功时**确实不打印任何提示语**, 只回显一行变量赋值,
        所以"看着没反应"完全可能已经生效 —— 唯一能当场看见的答案就是这个变量的值。
        本机 `console_log.txt` 给了完整因果链: 执行命令后值变 `374320`,
        执行 `testappcloudpaths 0` 或重启 Steam 后回到 `0`(值只在 `find` 的输出里露面)。
        """
        entry = cloud_log.CloudLogEntry(appid="374320", disabled_count=4)
        on = [
            "[2026-10-13 00:05:00] Console Log Start",
            '[2026-10-13 00:07:00] testappcloudpaths = "374320" - ',
        ]
        ack = self._probe(entry, console_lines=on)["cloud_toggle_console"]
        self.assertEqual(ack["value"], "374320")
        self.assertTrue(ack["value_matches_appid"])
        # `find` 形态也算"点亮痕迹", 但它证明的是**状态**, 不是"命令被敲过"那条回显
        self.assertTrue(ack["found"])

        off = [
            "[2026-10-13 00:05:00] Console Log Start",
            '[2026-10-13 00:08:00] testappcloudpaths = "0" - ',
        ]
        ack2 = self._probe(entry, console_lines=off)["cloud_toggle_console"]
        self.assertEqual(ack2["value"], "0")
        self.assertFalse(ack2["value_matches_appid"])
        self.assertFalse(ack2["found"])

    def test_report_shows_toggle_value_and_selfcheck(self) -> None:
        """报告要么给出开关当前值, 要么给出"自己去读值"的命令 —— 不能只说"未点亮"."""
        entry = cloud_log.CloudLogEntry(appid="374320", disabled_count=4)
        lines = [
            "[2026-10-13 00:05:00] Console Log Start",
            '[2026-10-13 00:07:00] testappcloudpaths = "374320" - ',
        ]
        g = self._probe(entry, console_lines=lines)

        def prof(caps: dict) -> dict:
            return {
                "tool": {
                    "name": "machine_probe",
                    "version": "0.2.6",
                    "redacted": False,
                    "entry": "python-script",
                },
                "os": {"system": "Windows", "release": "11", "python": "3.13.14"},
                "steam": {"primary_root": HOME},
                "capabilities": caps,
                "games": [g],
            }

        on_md = mp.render_md(mp.redact(prof({
            "console_log_available": True,
            "game_toggle_var_values": {"374320": "374320"},
        })))
        self.assertIn("开关当前值", on_md)
        self.assertIn("已点亮", on_md)
        self.assertIn("374320=「374320」", on_md)

        # 读不到值时 (朋友只敲了命令、没敲 `find`), 必须把自证命令写出来
        g["cloud_toggle_console"]["value"] = None
        g["cloud_toggle_console"]["value_matches_appid"] = None
        off_md = mp.render_md(mp.redact(prof({"console_log_available": True})))
        self.assertIn("find testappcloudpaths", off_md)
        self.assertIn("未知", off_md)


class TestAdapterGeneration(unittest.TestCase):
    def _profile(self) -> dict:
        return {
            "tool": {"name": "machine_probe", "version": "0.1.1", "redacted": False},
            "os": {"system": "Windows"},
            "steam": {"primary_root": r"C:\Steam"},
            "games": [
                {
                    "appid": "374320",
                    "name": "DARK SOULS III",
                    "mechanism": "auto-cloud",
                    "mechanisms": ["auto-cloud"],
                    "cloud_state": "disabled",
                    "cloud_blocker": {
                        "layer": "game",
                        "code": "game-toggle-off",
                        "detail": "该游戏的 AutoCloud 隐藏开关未点亮; 每次重启 Steam 都会失效.",
                        "fix": "testappcloudpaths 374320",
                    },
                    "cloud_toggle": {
                        "needed": True,
                        "command": "testappcloudpaths 374320",
                        "reset_command": "testappcloudpaths 0",
                    },
                    "change_numbers": {"local": 0, "global": 0},
                    "watching_files": [],
                    "watched_file_names": [],
                    "watched_file_state": None,
                    "save_dir_resolved": rf"C:\Users\me\AppData\Roaming\DarkSoulsIII\{HEX_DIR}",
                    "save_dir_source": "ac-root",
                    "save_dir_confident": True,
                    "account_dir_form": "steamid64-hex",
                    "save_snapshot": {"file_count": 3, "bytes": 10, "extensions": {".sl2": 1}, "files": ["DS30000.sl2"]},
                    "local_frame": {
                        "loader_dirs": ["D:////g////Game////dllMods"],
                        "mod_dirs": [],
                        "config_files": [],
                        "launchers": [],
                        "steam_appid_txt": "374320",
                        "injection_available": True,
                    },
                    "verdict": {"bridgeable": True, "reasons": ["云开关被禁用"]},
                    "save_candidates": [],
                }
            ],
        }

    def test_adapter_fields_and_steps(self) -> None:
        prof = self._profile()
        adapter = ga.build_adapter(prof, prof["games"][0])

        self.assertEqual(adapter["appid"], "374320")
        self.assertEqual(adapter["save"]["confidence"], "confirmed-by-name-match")
        self.assertEqual(adapter["save"]["path_style"], "absolute")
        self.assertTrue(adapter["injection"]["available"])
        self.assertTrue(adapter["bridge"]["supported"])

        steps = "\n".join(adapter["bridge"]["steps"])
        self.assertIn("testappcloudpaths 374320", steps)
        self.assertIn("dllMods", steps)
        self.assertIn("禁止跨账号导入", steps)
        # 两层开关的执行顺序必须出现 (先账号级后游戏级), 否则会教用户白点开关
        self.assertIn("两层开关", steps)
        self.assertIn("每次重启 Steam 都会失效", steps)
        self.assertEqual(adapter["cloud"]["blocker"]["code"], "game-toggle-off")

    def test_toggle_step_survives_legacy_profile(self) -> None:
        """旧档没有 cloud_blocker 字段时, 仍要由 cloud_state 兜底输出点云命令."""
        prof = self._profile()
        prof["games"][0].pop("cloud_blocker")
        adapter = ga.build_adapter(prof, prof["games"][0])
        steps = "\n".join(adapter["bridge"]["steps"])
        self.assertIn("testappcloudpaths 374320", steps)

    def test_account_layer_blocker_points_at_settings(self) -> None:
        """账号级阻塞必须把用户导向「设置 → 云服务」而不是游戏级命令."""
        prof = self._profile()
        g = prof["games"][0]
        g["cloud_state"] = "unknown"
        g["cloud_toggle"] = {"needed": False, "command": None, "reset_command": "testappcloudpaths 0"}
        g["cloud_blocker"] = {
            "layer": "account",
            "code": "account-cloud-off",
            "detail": "整机没有任何游戏发起过上传批次 ⇒ 账号级 Steam 云同步未启用.",
            "fix": "设置 → 云服务 → 勾选「为此账号启用 Steam 云同步」, 然后重启 Steam.",
        }
        adapter = ga.build_adapter(prof, g)
        steps = "\n".join(adapter["bridge"]["steps"])
        self.assertIn("账号级云同步", steps)
        self.assertEqual(adapter["cloud"]["blocker"]["layer"], "account")

    def test_yaml_emitter(self) -> None:
        prof = self._profile()
        adapter = ga.build_adapter(prof, prof["games"][0])
        yml = "\n".join(ga.to_yaml(adapter))

        self.assertIn('appid: "374320"', yml)
        self.assertIn("toggle_required: true", yml)
        # 反斜杠必须转义, 否则 YAML 双引号串会解析出错
        self.assertIn("\\\\", yml)
        # 列表元素应渲染成 "- xxx"
        self.assertIn("- ", yml)
        # 不应出现 Python 字面量
        for bad in ("None", "True", "False", "{'", "<class"):
            self.assertNotIn(bad, yml, f"YAML 里混入了 Python 字面量: {bad}")

    def test_redacted_profile_marks_templated(self) -> None:
        prof = self._profile()
        prof["tool"]["redacted"] = True
        adapter = ga.build_adapter(prof, prof["games"][0])
        self.assertEqual(adapter["save"]["path_style"], "templated")
        self.assertIn("脱敏档", "\n".join(adapter["bridge"]["steps"]))


class TestYamlEdgeCases(unittest.TestCase):
    def test_nested_and_empty(self) -> None:
        doc = {
            "a": {"b": [{"c": 1, "d": "x"}, {"c": 2}]},
            "empty_dict": {},
            "empty_list": [],
            "none": None,
            "bool": False,
        }
        yml = "\n".join(ga.to_yaml(doc))
        self.assertIn("empty_dict: {}", yml)
        self.assertIn("empty_list: []", yml)
        self.assertIn("none: null", yml)
        self.assertIn("bool: false", yml)
        self.assertIn("- c: 1", yml)


class TestLocalFrameProbe(unittest.TestCase):
    """注入点分类: 只有 dllMods / 加载器配置算注入能力.

    曾经把 mods / plugins 目录也算成注入点, 结果 8 个游戏全被误报"可 drop-in DLL"
    (Unity 的 <Game>_Data/Plugins 几乎每个游戏都有).
    """

    def test_dllmods_is_injection_point_but_plugins_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Game" / "dllMods").mkdir(parents=True)
            (root / "Game" / "modengine.ini").write_text("cfg", encoding="utf-8")
            (root / "SomeGame_Data" / "Plugins").mkdir(parents=True)
            (root / "Mods").mkdir()

            frame = mp.probe_local_frame(root)

            self.assertEqual(len(frame["loader_dirs"]), 1)
            self.assertTrue(frame["loader_dirs"][0].endswith("dllMods"))
            self.assertTrue(frame["injection_available"])
            self.assertEqual(len(frame["mod_dirs"]), 2)
            self.assertNotIn("Plugins", json.dumps(frame["loader_dirs"]))
            self.assertIn("modengine.ini", json.dumps(frame["config_files"]))

    def test_config_only_still_counts_as_injection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "modengine.ini").write_text("cfg", encoding="utf-8")
            frame = mp.probe_local_frame(root)
            self.assertEqual(frame["loader_dirs"], [])
            self.assertTrue(frame["injection_available"])

    def test_missing_dir_is_safe(self) -> None:
        frame = mp.probe_local_frame(None)
        self.assertFalse(frame["injection_available"])
        self.assertEqual(frame["loader_dirs"], [])


class TestDefaultOutDir(unittest.TestCase):
    """默认输出目录必须跟着布局走.

    单文件发行版里 `__file__` 就是那一个 .py, 如果照抄 `PROJECT_ROOT`
    (= parent.parent), 朋友的报告会落到**解压文件夹的外面**, 极反直觉.
    """

    def _with_fake_file(self, fake: str):
        original = mp.__file__
        mp.__file__ = fake
        try:
            return mp._default_out_dir()
        finally:
            mp.__file__ = original

    def test_repo_layout_uses_project_reports(self) -> None:
        out = self._with_fake_file("C:/x/repo/tools/machine_probe.py")
        self.assertEqual(out, Path("C:/x/repo/reports"))

    def test_single_file_layout_uses_sibling_reports(self) -> None:
        out = self._with_fake_file("C:/x/desktop/scb/SteamCloudBridgeProbe.py")
        self.assertEqual(out, Path("C:/x/desktop/scb/reports"))

    def test_single_file_layout_never_escapes_its_folder(self) -> None:
        fake = "C:/x/desktop/scb/SteamCloudBridgeProbe.py"
        out = self._with_fake_file(fake)
        self.assertEqual(out.parent, Path(fake).parent)

    def _with_frozen(self, exe: str, fake_file: str | None = None):
        """模拟 PyInstaller onefile 运行态: sys.frozen=True, sys.executable 指向 exe."""
        s = mp.sys
        old_frozen = getattr(s, "frozen", None)
        old_exe = s.executable
        old_file = mp.__file__
        s.frozen = True
        s.executable = exe
        if fake_file:
            mp.__file__ = fake_file
        try:
            return mp._default_out_dir()
        finally:
            s.executable = old_exe
            mp.__file__ = old_file
            if old_frozen is None:
                del s.frozen
            else:
                s.frozen = old_frozen

    def test_frozen_exe_uses_exe_directory(self) -> None:
        """免装 Python 的 exe: 报告必须落在 exe 同级, 而不是临时目录."""
        out = self._with_frozen("C:/x/desktop/scb/SteamCloudBridgeProbe.exe")
        self.assertEqual(out, Path("C:/x/desktop/scb/reports"))

    def test_frozen_ignores_meipass_temp_file(self) -> None:
        """onefile 下 `__file__` 指向 `%TEMP%\\_MEIxxxx\\`, 它必须被忽略.

        否则报告会写进临时目录, 进程一退出就被删 —— 用户看到的是
        \"跑完了但什么都没生成\".
        """
        out = self._with_frozen(
            "C:/x/desktop/scb/SteamCloudBridgeProbe.exe",
            fake_file="C:/Users/x/AppData/Local/Temp/_MEI123456/SteamCloudBridgeProbe.py",
        )
        self.assertEqual(out, Path("C:/x/desktop/scb/reports"))
        self.assertNotIn("_MEI", str(out))


class TestConsoleHarden(unittest.TestCase):
    """中文 Windows 控制台是 GBK —— 游戏名里的 `™` 不能让进程崩掉.

    实测: exe 在干净目录里跑 `DARK SOULS™ III` 时抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2122'`,
    报告只写了一半就退出. 这里锁住修复.
    """

    @staticmethod
    def _gbk_stream():
        import io

        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding="gbk")

    def test_gbk_console_survives_trademark(self) -> None:
        raw, stream = self._gbk_stream()
        fixed = mp._harden_console([stream])
        self.assertEqual(len(fixed), 1)
        stream.write("DARK SOULS\u2122 III")  # 修复前此行抛 UnicodeEncodeError
        stream.flush()
        out = raw.getvalue()
        self.assertIn(b"DARK SOULS", out)
        self.assertIn(b"III", out)

    def test_ascii_console_still_writes_plain_text(self) -> None:
        import io

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii")
        mp._harden_console([stream])
        stream.write("plain ascii")
        stream.flush()
        self.assertEqual(raw.getvalue(), b"plain ascii")

    def test_none_stream_is_skipped(self) -> None:
        self.assertEqual(mp._harden_console([None]), [])

    def test_stream_without_reconfigure_is_ignored(self) -> None:
        class Dummy:
            pass

        self.assertEqual(mp._harden_console([Dummy()]), [])


class TestReportLayoutAndAudience(unittest.TestCase):
    """报告的两条硬约束 (由朋友端 v0.2.0 回传事故驱动):

    * **目录分层**: 未脱敏完整档必须收在 `private/` 下, 根目录只放可外发的
      东西 —— 否则用户最自然的动作就是把整个 reports 压回来, 完整档跟着外泄.
    * **受众正确**: §3 里的 `gen_adapter.py` 只有拿到源码的开发者能跑,
      发行包里根本没有 `tools/`, 必须写明, 否则朋友照着敲必然失败.
    """

    def _profile(self) -> dict:
        return {
            "tool": {
                "name": "machine_probe",
                "version": "0.2.0",
                "redacted": False,
                "entry": "exe-onefile",
            },
            "os": {"system": "Windows", "release": "11", "python": "3.13.14"},
            "steam": {"primary_root": HOME},
            "games": [],
        }

    def test_redact_keeps_entry_and_marks_redacted(self) -> None:
        """entry 是"怎么被拉起的", 属诊断信息, 脱敏后必须保留."""
        red = mp.redact(self._profile())
        self.assertEqual(red["tool"]["entry"], "exe-onefile")
        self.assertTrue(red["tool"]["redacted"])

    def test_redaction_note_points_at_private_dir(self) -> None:
        red = mp.redact(self._profile())
        self.assertIn("private/machine_profile.json", red["redaction"]["note"])

    def test_entry_label_covers_all_three_launch_forms(self) -> None:
        """entry 只可能是三种之一; `_MEIPASS` 在 onefile/onedir 都会设置,
        所以必须靠解包目录形态区分, 只看 hasattr 会把 onedir 误报成 onefile."""
        onefile = {"frozen": True, "_MEIPASS": r"C:\Users\me\AppData\Local\Temp\_MEI123456"}
        onedir = {"frozen": True, "_MEIPASS": r"D:\app\_internal"}
        script = {"frozen": False, "_MEIPASS": None}
        for attrs, expected in (
            (onefile, "exe-onefile"),
            (onedir, "exe-onedir"),
            (script, "python-script"),
        ):
            with self.subTest(expected=expected):
                with mock.patch.object(mp.sys, "frozen", attrs["frozen"], create=True), \
                        mock.patch.object(mp.sys, "_MEIPASS", attrs["_MEIPASS"], create=True):
                    self.assertEqual(mp._entry_label(), expected)

    def test_section3_carries_developer_only_caveat(self) -> None:
        md = mp.render_md(mp.redact(self._profile()))
        self.assertIn("把数据交回项目", md)
        self.assertRegex(md, r"发行包里没有|只有拿到源码的开发者")
        self.assertIn("machine_profile_bundle.zip", md)

    def test_section3_warns_against_private_dir(self) -> None:
        md = mp.render_md(mp.redact(self._profile()))
        self.assertIn("不要发", md)
        self.assertIn("private/", md)

    def test_exe_header_does_not_claim_installed_python(self) -> None:
        """由 exe 拉起时, 报告头必须说清解释器是**自带**的.

        否则"系统: Windows 11 / Python 3.13.14"会被读成本机装了 Python —— 而
        发行 exe 的存在意义恰恰是"朋友不用装 Python". 这是 v0.2.1 回传事故里
        `tool.entry` 已经能答、报告头却没答的那半个问题.
        """
        prof = self._profile()  # entry = exe-onefile
        md = mp.render_md(mp.redact(prof))
        header = [ln for ln in md.splitlines() if ln.startswith("- 系统:")][0]
        self.assertIn("内置 Python", header)
        self.assertIn("exe 自带", header)
        # 不能出现"光秃秃"的 Python 版本声明
        self.assertNotRegex(header, r"/ Python `3\.13\.14`$")
        # 运行方式单列一行, 人读报告里直接可见
        self.assertRegex(md, r"(?m)^- 运行方式: 单文件 exe（自带解释器")

    def test_script_header_keeps_plain_python(self) -> None:
        """.py 运行时就该照实写本机 Python 版本, 不要乱加"自带"."""
        prof = self._profile()
        prof["tool"]["entry"] = "python-script"
        md = mp.render_md(mp.redact(prof))
        header = [ln for ln in md.splitlines() if ln.startswith("- 系统:")][0]
        self.assertIn("Python `3.13.14`", header)
        self.assertNotIn("内置", header)
        self.assertRegex(md, r"(?m)^- 运行方式: Python 脚本")

    def test_unknown_entry_is_reported_conservatively(self) -> None:
        """entry 缺失/未知时不得宣称"自带解释器" —— 宁可少说."""
        prof = self._profile()
        del prof["tool"]["entry"]
        desc, bundled = mp._entry_display(None)
        self.assertFalse(bundled)
        self.assertIn("Python 脚本", desc)
        md = mp.render_md(mp.redact(prof))
        self.assertNotIn("内置 Python", md)
        self.assertRegex(md, r"(?m)^- 运行方式: Python 脚本")
        # 三个已知形态都要能翻译
        for key, expect_bundled in (
            ("exe-onefile", True),
            ("exe-onedir", True),
            ("python-script", False),
        ):
            with self.subTest(entry=key):
                _, bundled = mp._entry_display(key)
                self.assertEqual(bundled, expect_bundled)


class TestInstalledInventory(unittest.TestCase):
    """报告必须能回答"这台机器装没装某个游戏".

    事故来源: 朋友机 v0.2.1 回传档里 DS3 (`374320`) 零痕迹 —— 因为逐游戏详查
    只覆盖有云证据的候选, 而档案**没有记录已安装清单**。我据此断言"他没装 DS3",
    真相是他装了。字段缺失 ⇒ \"报告里没有\" 被读成 \"机器上没有\", 这是最危险的
    一类报告缺陷: 它不报错, 只是静默地误导.
    """

    def _prof(self, caps=None, installed=None, games=None) -> dict:
        return {
            "tool": {
                "name": "machine_probe",
                "version": "0.2.2",
                "redacted": False,
                "entry": "python-script",
            },
            "generated_at": "2026-10-12T21:00:00",
            "os": {"system": "Windows", "release": "11", "python": "3.13.14"},
            "steam": {"primary_root": HOME, "app_manifest_appids": installed or []},
            "capabilities": caps or {},
            "games": games or [],
        }

    def test_installed_inventory_survives_redaction(self) -> None:
        """已安装清单是 appid 列表 (公开商店 ID), 脱敏后必须原样保留."""
        prof = self._prof(installed=["1245620", "374320"])
        red = mp.redact(prof)
        self.assertEqual(red["steam"]["app_manifest_appids"], ["1245620", "374320"])

    def test_report_states_unprobed_count(self) -> None:
        """报告的"机器能力判定"表必须写明"已安装但未详查"的条数."""
        prof = self._prof(
            caps={"games_probed": 28, "installed_not_probed_count": 31},
            installed=["374320"],
        )
        md = mp.render_md(mp.redact(prof))
        self.assertIn("已安装但未详查", md)
        self.assertIn("31 个", md)

    def test_report_warns_against_absence_equals_uninstalled(self) -> None:
        """必须显式禁止"没看到 = 没装"的推理, 并给出强制详查与完整清单的入口."""
        prof = self._prof(caps={"games_probed": 0, "installed_not_probed_count": 0})
        md = mp.render_md(mp.redact(prof))
        self.assertIn('不是"没装"', md)
        self.assertIn("steam.app_manifest_appids", md)
        self.assertIn("--appid", md)

    def test_zero_unprobed_still_renders_row(self) -> None:
        """0 也要显示成 0, 不能整行消失 —— 否则读的人分不清"没有"和"没统计"."""
        prof = self._prof(caps={"games_probed": 5, "installed_not_probed_count": 0})
        md = mp.render_md(mp.redact(prof))
        self.assertIn("| 已安装但未详查 | 0 个 |", md)


if __name__ == "__main__":
    unittest.main()
