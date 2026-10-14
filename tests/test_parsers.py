"""解析器冒烟测试: 不依赖真实 Steam 环境, 用构造样本验证核心逻辑.

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import vdf  # noqa: E402
from src.steam import accounts, cloud_log, discovery, processes, remotecache  # noqa: E402


class TestVdf(unittest.TestCase):
    def test_basic_nesting(self) -> None:
        text = '''
        "root"
        {
            // 注释应被忽略
            "name"    "DARK SOULS III"
            "nested"
            {
                "k"   "v"
            }
        }
        '''
        data = vdf.parse(text)
        self.assertEqual(data["root"]["name"], "DARK SOULS III")
        self.assertEqual(data["root"]["nested"]["k"], "v")

    def test_windows_path_backslash_preserved(self) -> None:
        """Windows 路径中的反斜杠必须原样保留 (不能当成转义符吃掉)."""
        text = '"a"\n{\n\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"\n}\n'
        data = vdf.parse(text)
        self.assertEqual(data["a"]["path"], r"C:\Program Files (x86)\Steam")

    def test_escaped_quote(self) -> None:
        text = '"a"\n{\n\t"k"\t\t"he said \\"hi\\""\n}\n'
        self.assertEqual(vdf.parse(text)["a"]["k"], 'he said "hi"')

    def test_first_key_is_case_insensitive(self) -> None:
        node = {"AppID": "374320"}
        self.assertEqual(vdf.first_key(node, "appid"), "374320")


class TestAccounts(unittest.TestCase):
    def test_known_pair(self) -> None:
        """本机实测过的真实换算: accountid 1234567890 <-> 十六进制 01100001499602d2."""
        aid = 1234567890
        self.assertEqual(accounts.accountid_to_steamid64(aid), 76561199194833618)
        self.assertEqual(accounts.accountid_to_hex_dir(aid), "01100001499602d2")
        self.assertEqual(accounts.steamid64_to_accountid(76561199194833618), aid)

    def test_roundtrip(self) -> None:
        for aid in (1, 12345, 999999999):
            self.assertEqual(
                accounts.steamid64_to_accountid(accounts.accountid_to_steamid64(aid)), aid
            )

    def test_hex_dir_reverse(self) -> None:
        """hex 目录名反查 accountid, 非法输入必须是 None (不许瞎猜)."""
        self.assertEqual(accounts.hex_dir_to_accountid("01100001499602d2"), 1234567890)
        self.assertEqual(accounts.hex_dir_to_accountid("01100001499602D2"), 1234567890)
        for bad in ("", "zzzz", "0000000000000000", "x" * 17):
            self.assertIsNone(accounts.hex_dir_to_accountid(bad), msg=bad)

    def test_accountid_from_save_dir(self) -> None:
        p = Path(r"C:\Users\u\AppData\Roaming\DarkSoulsIII\01100001499602d2\DS30000.sl2")
        self.assertEqual(accounts.accountid_from_save_dir(p), 1234567890)
        self.assertIsNone(accounts.accountid_from_save_dir(Path("D:/save/DS30000.sl2")))

    def test_find_accountid_for_app_unique_only(self) -> None:
        """注册表兜底: 只在 `userdata/<id>/<appid>/remotecache.vdf` **唯一命中**时给账号.

        本机实测 `userdata\\` 下有 150+ 个账号目录, 多命中必须放弃 —— 猜错会把
        "别人的云端副本" 当成自己的.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNone(accounts.find_accountid_for_app(root, "374320"))
            (root / "userdata" / "1234567890" / "374320").mkdir(parents=True)
            (root / "userdata" / "1234567890" / "374320" / "remotecache.vdf").write_text("x")
            self.assertEqual(accounts.find_accountid_for_app(root, "374320"), 1234567890)
            self.assertIsNone(accounts.find_accountid_for_app(root, "999999"))
            (root / "userdata" / "999" / "374320").mkdir(parents=True)
            (root / "userdata" / "999" / "374320" / "remotecache.vdf").write_text("x")
            self.assertIsNone(accounts.find_accountid_for_app(root, "374320"))


class TestProcesses(unittest.TestCase):
    """`src/steam/processes.py`: 只做不依赖"真机上真有该进程"的用例."""

    def test_missing_image_has_no_pids(self) -> None:
        self.assertEqual(processes.find_pids("scb-no-such-process-xyz.exe"), [])

    def test_start_epoch_none_for_missing_image(self) -> None:
        self.assertIsNone(processes.steam_start_epoch("scb-no-such-process-xyz.exe"))

    def test_steam_start_epoch_is_sane_or_none(self) -> None:
        """Steam 在跑 -> 近期时间戳; 没在跑 -> None. 都不许是垃圾值."""
        t = processes.steam_start_epoch()
        if t is not None:
            self.assertGreater(t, 1_600_000_000)  # 晚于 2020-09


class TestRemoteCache(unittest.TestCase):
    def _write(self, tmp: Path, body: str) -> Path:
        p = tmp / "remotecache.vdf"
        p.write_text(body, encoding="utf-8")
        return p

    def test_parse_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write(
                tmp,
                '"374320"\n{\n'
                '\t"DS30000.sl2"\n\t{\n\t\t"root"\t\t"4"\n\t\t"size"\t\t"9307472"\n'
                '\t\t"sha"\t\t"abc"\n\t\t"syncstate"\t\t"1"\n\t}\n'
                '\t"notes.txt"\n\t{\n\t\t"root"\t\t"12"\n\t\t"size"\t\t"10"\n\t}\n'
                "}\n",
            )
            cache = remotecache.parse_remotecache(p, appid="374320")
            self.assertEqual(len(cache.entries), 2)
            self.assertEqual(cache.roots, {"4": 1, "12": 1})
            by_name = {e.name: e for e in cache.entries}
            self.assertEqual(by_name["DS30000.sl2"].size, 9307472)
            self.assertEqual(by_name["DS30000.sl2"].root, "4")

    def test_change_number_and_ever_advanced(self) -> None:
        """`ChangeNumber` 是 Steam 落盘结果, 是"曾推进过"的权威判据.

        2026-10-11 21:33 DS3 **首次**上传成功: 根节点 `ChangeNumber` 0 → 1,
        叶子 `remotetime` 0 → 1791725553. cloud_log 里最后一条 `Need to sync`
        仍是 `'0' to '0'` (它打印在上传**之前**), 只看日志会把刚传成功的游戏
        报成"从未推进".
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write(
                tmp,
                '"374320"\n{\n'
                '\t"ChangeNumber"\t\t"1"\n'
                '\t"ostype"\t\t"0"\n'
                '\t"DarkSoulsIII/01100001499602d2/DS30000.sl2"\n\t{\n'
                '\t\t"root"\t\t"4"\n'
                '\t\t"size"\t\t"9307472"\n'
                '\t\t"localtime"\t\t"1791725554"\n'
                '\t\t"remotetime"\t\t"1791725553"\n'
                '\t\t"sha"\t\t"ec4d2bd1"\n'
                "\t}\n}\n",
            )
            cache = remotecache.parse_remotecache(p, appid="374320")
            self.assertEqual(cache.change_number, 1)
            self.assertTrue(cache.ever_advanced)
            self.assertTrue(cache.upload_verified)
            self.assertEqual(cache.max_remotetime, 1791725553)
            self.assertEqual(cache.pending_upload, [])
            self.assertEqual(len(cache.entries), 1)  # `ChangeNumber` 不当成文件叶子

    def test_change_number_zero_means_never_submitted(self) -> None:
        """上传前的状态: 已本地登记 (localtime/sha), 但云端变更号与 remotetime 都是 0."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write(
                tmp,
                '"374320"\n{\n'
                '\t"ChangeNumber"\t\t"0"\n'
                '\t"DS30000.sl2"\n\t{\n\t\t"root"\t\t"4"\n'
                '\t\t"localtime"\t\t"1791642079"\n\t\t"remotetime"\t\t"0"\n'
                "\t}\n}\n",
            )
            cache = remotecache.parse_remotecache(p, appid="374320")
            self.assertEqual(cache.change_number, 0)
            self.assertFalse(cache.ever_advanced)
            self.assertFalse(cache.upload_verified)
            self.assertEqual(cache.pending_upload, ["DS30000.sl2"])

    def test_calibration_picks_correct_base(self) -> None:
        """标定必须靠真实存在性: 只有文件真的在那个基目录下才算命中."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "Roaming" / "StardewValley").mkdir(parents=True)
            (tmp / "Roaming" / "StardewValley" / "SaveGameInfo").write_text("x")
            (tmp / "Local").mkdir()

            records = [
                ("4", "StardewValley/SaveGameInfo", {}),
                ("4", "StardewValley/SaveGameInfo", {}),
            ]
            bases = {"%APPDATA%": tmp / "Roaming", "%LOCALAPPDATA%": tmp / "Local"}
            cal = remotecache.calibrate_roots(records, bases=bases)
            self.assertEqual(cal["4"].base_label, "%APPDATA%")
            self.assertEqual(cal["4"].hits, 2)
            self.assertEqual(cal["4"].confidence, "high")

    def test_calibration_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            records = [("3", "../../../etc/passwd", {})]
            cal = remotecache.calibrate_roots(records, bases={"b": tmp})
            self.assertEqual(cal["3"].hits, 0)

    def test_upload_verified_false_when_remotetime_zero(self) -> None:
        """本机 DS3 实况: 文件已登记进 remotecache, 但 remotetime 恒为 0 = 从未上云.

        这是本项目用 `remotetime` 而非 cloud_log 的 `Successfully synced` 判定的
        原因 —— Auto-Cloud 上传方向不打印完成行 (见 cloud_log.cloud_roundtrip_ok).
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write(
                tmp,
                '"374320"\n{\n\t"ChangeNumber"\t\t"0"\n'
                '\t"DarkSoulsIII/01100001499602d2/DS30000.sl2"\n\t{\n'
                '\t\t"root"\t\t"4"\n\t\t"size"\t\t"9307472"\n'
                '\t\t"localtime"\t\t"1789916991"\n\t\t"time"\t\t"1789917000"\n'
                '\t\t"remotetime"\t\t"0"\n\t\t"sha"\t\t"71e413d1"\n'
                '\t\t"syncstate"\t\t"3"\n\t}\n}\n',
            )
            cache = remotecache.parse_remotecache(p, appid="374320")
            self.assertFalse(cache.upload_verified)
            self.assertEqual(cache.max_remotetime, 0)
            self.assertEqual(
                cache.pending_upload, ["DarkSoulsIII/01100001499602d2/DS30000.sl2"]
            )
            self.assertFalse(cache.to_dict()["upload_verified"])

    def test_upload_verified_true_when_remotetime_positive(self) -> None:
        """对照游戏 241100/730 实况: remotetime > 0 = 云端确有副本."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write(
                tmp,
                '"241100"\n{\n\t"ChangeNumber"\t\t"2"\n'
                '\t"1234567890/config/preferences.vdf"\n\t{\n'
                '\t\t"root"\t\t"1"\n\t\t"localtime"\t\t"1598232224"\n'
                '\t\t"remotetime"\t\t"1598232224"\n\t\t"syncstate"\t\t"1"\n\t}\n}\n',
            )
            cache = remotecache.parse_remotecache(p, appid="241100")
            self.assertTrue(cache.upload_verified)
            self.assertEqual(cache.max_remotetime, 1598232224)
            self.assertEqual(cache.pending_upload, [])

    def test_pending_upload_ignores_entries_without_local_marker(self) -> None:
        """没有本地时间戳/sha 的空条目不算"待上传" (避免把纯占位算进来)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write(
                tmp,
                '"1"\n{\n\t"ghost.dat"\n\t{\n\t\t"root"\t\t"4"\n'
                '\t\t"size"\t\t"0"\n\t\t"remotetime"\t\t"0"\n\t}\n}\n',
            )
            cache = remotecache.parse_remotecache(p, appid="1")
            self.assertEqual(cache.pending_upload, [])
            self.assertFalse(cache.upload_verified)


class TestGameNameMatch(unittest.TestCase):
    """候选目录收敛: 完全匹配优先于部分匹配."""

    def test_exact_match_on_fromsoftware_dir(self) -> None:
        p = Path(r"C:\Users\x\AppData\Roaming\DarkSoulsIII\01100001499602d2")
        level, reason = discovery.match_game_name(p, ["DARK SOULS\u2122 III", "DARK SOULS III"])
        self.assertEqual(level, "exact")
        self.assertIn("DarkSoulsIII", reason)

    def test_roman_numeral_suffix_is_only_partial(self) -> None:
        """DarkSoulsII 对 DARK SOULS III 只能是 partial, 不能是 exact.

        这正是"部分匹配容易假阳性"的场景, 所以调用方必须优先用 exact 收敛.
        """
        p = Path(r"C:\Users\x\AppData\Roaming\DarkSoulsII\01100001499602d2")
        level, _ = discovery.match_game_name(p, ["DARK SOULS III"])
        self.assertEqual(level, "partial")

    def test_no_match_without_usable_name(self) -> None:
        p = Path(r"C:\Users\x\AppData\Roaming\SomeGame\1234567890")
        level, reason = discovery.match_game_name(p, ["", "abc"])
        self.assertEqual((level, reason), ("", ""))

    def test_local_low_style_path(self) -> None:
        p = Path(
            r"C:\Users\x\AppData\LocalLow\Team Cherry\Hollow Knight Silksong\1234567890"
        )
        level, _ = discovery.match_game_name(p, ["Hollow Knight Silksong"])
        self.assertEqual(level, "exact")


class TestAcRootResolution(unittest.TestCase):
    def test_known_ac_dir_names(self) -> None:
        home = Path(os.path.expanduser("~"))
        roaming = discovery.resolve_ac_root_dir("WinAppDataRoaming")
        self.assertIsNotNone(roaming)
        self.assertEqual(roaming.name, "Roaming")

        locallow = discovery.resolve_ac_root_dir("WinAppDataLocalLow")
        self.assertEqual(locallow, home / "AppData" / "LocalLow")

    def test_game_install_requires_context(self) -> None:
        self.assertIsNone(discovery.resolve_ac_root_dir("GameInstall"))
        fake = Path(r"D:\Steam\steamapps\common\SomeGame")
        self.assertEqual(discovery.resolve_ac_root_dir("GameInstall", fake), fake)

    def test_unknown_dir_name(self) -> None:
        self.assertIsNone(discovery.resolve_ac_root_dir("TotallyBogus"))


class TestCloudLog(unittest.TestCase):
    def _parse(self, body: str) -> cloud_log.CloudLog:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cloud_log.txt"
            p.write_text(body, encoding="utf-8")
            return cloud_log.parse_cloud_log(p)

    def test_watching_files_extracted(self) -> None:
        log = self._parse(
            "[2026-01-01 00:00:00] [AppID 1245620] Running AutoCloud on launch. "
            "Looking for existing files to watch:\n"
            "[2026-01-01 00:00:00] [AppID 1245620]     Watching file "
            r"C:\Users\x\AppData\Roaming\EldenRing\76561199194833618\ER0000.sl2" + "\n"
            "[2026-01-01 00:00:00] [AppID 1245620] AutoCloud done. Watching 1 files\n"
        )
        e = log.entries["1245620"]
        self.assertEqual(len(e.watching_files), 1)
        self.assertTrue(e.watching_files[0].endswith("ER0000.sl2"))
        self.assertEqual(e.watching_count, 1)

    def test_disabled_plus_zero_change_number_is_strong_evidence(self) -> None:
        log = self._parse(
            "[t] [AppID 374320] AutoCloud is disabled. Run \"testappcloudpaths 374320\" "
            "in the console to enable it\n"
            "[t] [AppID 374320] Need to sync from local change number '0' to global "
            "change number '0' (full), but not attempting now\n"
        )
        e = log.entries["374320"]
        self.assertEqual(e.cloud_state, "disabled")
        self.assertTrue(e.never_synced)
        self.assertFalse(e.no_change_advance)

    def test_zero_change_number_without_disabled_is_weak_only(self) -> None:
        """没有禁用记录时, 两侧全 0 只能给"弱提示", 不能断言从未同步."""
        log = self._parse(
            "[t] [AppID 999] Need to sync from local change number '0' to global "
            "change number '0' (full)\n"
        )
        e = log.entries["999"]
        self.assertFalse(e.never_synced)
        self.assertTrue(e.no_change_advance)

    def test_sync_direction(self) -> None:
        log = self._parse(
            "[t] [AppID 1] Need to sync from local change number '0' to global change number '24'\n"
            "[t] [AppID 2] Need to sync from local change number '48' to global change number '47'\n"
        )
        self.assertEqual(log.entries["1"].sync_direction, "local-behind")
        self.assertEqual(log.entries["2"].sync_direction, "local-ahead")
        self.assertTrue(log.entries["1"].ever_advanced)

    def test_upload_batch_initiated_is_the_real_proof(self) -> None:
        """`Upload batch initiated` = 上传真的动手; `Upload complete in build list` = 完成.

        实测 (2026-10-05): 账号 987654321 的 730 有这两行, 而当前账号 (1234567890)
        的 DS3 一次都没有 —— 这正是"该账号游戏云存上传已停止"的直接证据,
        比 `remotetime` 更早可见, 且不依赖具体机制.
        """
        log = self._parse(
            "[t] [AppID 730] Need to upload file cs2_user_convars.vcfg\n"
            "[t] [AppID 730] Upload batch initiated, ID: 542229626652940354, "
            "ChangeNumber: 10, App Build ID: 25000182\n"
            "[t] [AppID 730] Upload complete in build list\n"
            "[t] [AppID 374320] Persisting file C:\\x\\DS30000.sl2 to the cloud\n"
        )
        e730 = log.entries["730"]
        self.assertTrue(e730.upload_initiated)
        self.assertEqual(e730.last_upload_change_number, 10)
        self.assertEqual(e730.upload_complete_count, 1)
        self.assertEqual(e730.upload_batches, [(542229626652940354, 10)])

        # DS3: 有 Persisting 却从未发起上传 —— 账号级云同步停摆的典型特征
        ds3 = log.entries["374320"]
        self.assertFalse(ds3.upload_initiated)
        self.assertEqual(ds3.upload_complete_count, 0)
        self.assertTrue(ds3.queued_not_uploaded)

    def test_upload_ok_vs_delete_ok(self) -> None:
        """`Upload OK for file` = 文件真上去了; `Delete OK for file` = 云端那份被删了.

        实测 PEAK (3527290): `Need to delete file ...quicksave.peak` →
        `Delete OK for file ...` → `Upload complete, result OK` →
        `empty vector, deleting remotecache.vdf`. 它的 `upload_result_ok_count > 0`
        但**没有**云端副本, 所以判据必须落在 `uploaded_files` 上 ——
        拿 `result OK` 当"上传成功"会把"云端副本被删"误判成"已有副本".
        """
        log = self._parse(
            "[t] [AppID 4659620] Upload batch initiated, ID: 1, ChangeNumber: 1, App Build ID: 2\n"
            "[t] [AppID 4659620] Upload OK for file score.dat\n"
            "[t] [AppID 4659620] Upload OK for file replay/th6_01.rpy\n"
            "[t] [AppID 4659620] Upload complete, result OK\n"
            "[t] [AppID 3527290] Upload batch initiated, ID: 2, ChangeNumber: 1, App Build ID: 3\n"
            "[t] [AppID 3527290] Delete OK for file LandCrab/PEAK/quicksave.peak\n"
            "[t] [AppID 3527290] Upload complete, result OK\n"
        )
        th = log.entries["4659620"]
        self.assertTrue(th.upload_confirmed)
        self.assertEqual(sorted(th.uploaded_files), ["replay/th6_01.rpy", "score.dat"])
        self.assertEqual(th.upload_result_ok_count, 1)
        self.assertFalse(th.cloud_deleted)

        peak = log.entries["3527290"]
        self.assertFalse(peak.upload_confirmed)      # 没有 Upload OK, 只有 Delete OK
        self.assertTrue(peak.cloud_deleted)
        self.assertEqual(peak.deleted_files, ["LandCrab/PEAK/quicksave.peak"])
        self.assertEqual(peak.upload_result_ok_count, 1)  # 有 result OK 也不能当上传成功


class TestAutoCloudRules(unittest.TestCase):
    """`Evaluating rule` 才是自动云游戏的真相源 (2026-10-05 实机反推).

    教训: `Watching file` 只在本机已有云端缓存时才打印, 拿它当唯一依据会把
    "云从未通过"的游戏误判成"无云存".
    """

    def _parse(self, body: str) -> cloud_log.CloudLog:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cloud_log.txt"
            p.write_text(body, encoding="utf-8")
            return cloud_log.parse_cloud_log(p)

    _DS3_EXIT_SCAN = (
        "[t] [AppID 374320] Need to sync from local change number '0' to global "
        "change number '0' (full), but not attempting now\n"
        "[t] [AppID 374320] Running AutoCloud on exit. Looking for new and updated files\n"
        '[t] [AppID 374320]     Evaluating rule 0 with root="WinAppDataRoaming" '
        'path="DarkSoulsIII" pattern="*.sl2" (recursive)\n'
        r"[t] [AppID 374320]         Found 1 files that match "
        "C:\\Users\\x\\AppData\\Roaming\\DarkSoulsIII\\01100001499602d2\\*.sl2\n"
        r"[t] [AppID 374320]     Persisting file "
        "C:\\Users\\x\\AppData\\Roaming\\DarkSoulsIII\\01100001499602d2\\DS30000.sl2 "
        "to the cloud\n"
        "[t] [AppID 374320] AutoCloud complete\n"
        "[t] [AppID 374320] Need to upload file "
        "DarkSoulsIII/01100001499602d2/DS30000.sl2\n"
    )

    def test_rule_is_captured_with_hits(self) -> None:
        e = self._parse(self._DS3_EXIT_SCAN).entries["374320"]
        self.assertEqual(len(e.rules), 1)
        rule = e.rules[0]
        self.assertEqual(rule["root"], "WinAppDataRoaming")
        self.assertEqual(rule["path"], "DarkSoulsIII")
        self.assertEqual(rule["pattern"], "*.sl2")
        self.assertTrue(rule["recursive"])
        self.assertEqual([h["count"] for h in rule["hits"]], [1])

    def test_rule_gives_file_set_without_watching_file(self) -> None:
        """没有任何 `Watching file` 也要能推出被同步的文件集."""
        e = self._parse(self._DS3_EXIT_SCAN).entries["374320"]
        self.assertEqual(e.watching_files, [])
        self.assertEqual(e.rule_patterns, ["*.sl2"])
        self.assertEqual(e.rule_file_names, ["DS30000.sl2"])
        self.assertEqual(len(e.declared_file_set), 1)
        self.assertTrue(e.declared_file_set[0].endswith("DS30000.sl2"))
        self.assertTrue(e.has_rule_evidence)
        self.assertEqual(e.mechanism, "auto-cloud")

    def test_persisted_without_roundtrip_is_queued_not_uploaded(self) -> None:
        """有 Persisting 但两侧 change number 还是 0 到 0 = 本地入队, 未真的上云."""
        e = self._parse(self._DS3_EXIT_SCAN).entries["374320"]
        self.assertFalse(e.cloud_roundtrip_ok)
        self.assertTrue(e.queued_not_uploaded)

    def test_successfully_synced_marks_roundtrip_ok(self) -> None:
        e = self._parse(
            self._DS3_EXIT_SCAN
            + "[t] [AppID 374320] Successfully synced to ChangeNumber 7\n"
        ).entries["374320"]
        self.assertTrue(e.cloud_roundtrip_ok)
        self.assertEqual(e.synced_change_number, 7)
        self.assertFalse(e.queued_not_uploaded)

    def test_already_synced_counts_as_roundtrip(self) -> None:
        e = self._parse(
            "[t] [AppID 241100] Currently already synced to global change "
            "number '2', should be nothing to download\n"
        ).entries["241100"]
        self.assertTrue(e.cloud_roundtrip_ok)
        self.assertEqual(e.synced_change_number, 2)

    def test_need_upload_alone_is_not_queued_not_uploaded(self) -> None:
        """回归: `Need to upload file` 在 API 型游戏里也会出现, 不能当未上传判据."""
        e = self._parse(
            "[t] [AppID 730] Need to upload file cfg/config.cfg\n"
        ).entries["730"]
        self.assertEqual(e.persisted_files, [])
        self.assertFalse(e.queued_not_uploaded)
        self.assertEqual(e.cloud_rel_paths, ["cfg/config.cfg"])

    def test_orphan_found_line_is_kept(self) -> None:
        """规则头被日志轮转切掉时, `Found` 行仍要保留为降级证据."""
        e = self._parse(
            r"[t] [AppID 999] Found 2 files that match C:\x\save\*.dat" + "\n"
        ).entries["999"]
        self.assertEqual(len(e.rules), 1)
        self.assertIsNone(e.rules[0]["pattern"])
        self.assertEqual(e.rules[0]["hits"][0]["count"], 2)

    def test_merge_dedupes_identical_rules(self) -> None:
        """同一规则在轮转的多个日志文件里重复出现时, 命中要合并而不是复制节点."""
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "cloud_log.txt"
            b = Path(td) / "cloud_log.previous.txt"
            a.write_text(self._DS3_EXIT_SCAN, encoding="utf-8")
            b.write_text(self._DS3_EXIT_SCAN, encoding="utf-8")
            merged = cloud_log.parse_cloud_logs([a, b])
        e = merged.entries["374320"]
        self.assertEqual(len(e.rules), 1)
        self.assertEqual(len(e.rules[0]["hits"]), 1)
        self.assertEqual(len(e.persisted_files), 1)

    _DISABLED_LINE = (
        '[t] [AppID 374320] AutoCloud is disabled. Run "testappcloudpaths 374320" '
        "in the console to enable it\n"
    )

    def test_toggle_effective_when_rule_follows_disabled(self) -> None:
        """`testappcloudpaths` 生效不会打印 enabled.

        它只让"禁用行"停止出现并开始打印规则行. 日志里旧的禁用行永远留着,
        所以只能比先后 —— 只看计数会把已点亮的游戏一直报成"禁用".
        """
        e = self._parse(self._DISABLED_LINE + self._DS3_EXIT_SCAN).entries["374320"]
        self.assertTrue(e.toggle_effective)
        self.assertEqual(e.cloud_state, "enabled")
        self.assertFalse(e.never_synced)
        self.assertTrue(e.no_change_advance)

    def test_toggle_not_effective_when_disabled_is_last(self) -> None:
        """禁用行出现在最后 => 命令还没生效 (或已经因重启 Steam 失效)."""
        e = self._parse(self._DS3_EXIT_SCAN + self._DISABLED_LINE).entries["374320"]
        self.assertFalse(e.toggle_effective)
        self.assertEqual(e.cloud_state, "disabled")
        self.assertTrue(e.never_synced)

    # 2026-10-11 实测样本: 开关已点亮, 但这一局走的是 AC Launch 路径,
    # 只打印 `Running AutoCloud on launch` + `Watching file`, **不打印任何规则行**.
    _DS3_LAUNCH_WATCH = (
        "[t] [AppID 374320] Starting sync (AC Launch,down,)\n"
        "[t] [AppID 374320] AutoCloud checking local state for user 1\n"
        "[t] [AppID 374320] Attempting to sync from local change number '0' "
        "to global change number '0' (full)\n"
        "[t] [AppID 374320] Need to upload file "
        "DarkSoulsIII/01100001499602d2/DS30000.sl2\n"
        r"[t] [AppID 374320]     Watching file "
        "C:\\Users\\x\\AppData\\Roaming\\DarkSoulsIII\\01100001499602d2\\DS30000.sl2\n"
        "[t] [AppID 374320] AutoCloud done. Watching 1 files\n"
    )

    def test_toggle_effective_when_watching_follows_disabled(self) -> None:
        """禁用行 + 其后的 `Watching file` => 开关已生效.

        旧逻辑只把"规则行/落盘行"当生效证据; 但 `testappcloudpaths` 生效后
        Steam 走 AC Launch 路径时可能**只打印 Watching** (禁用期不会进入文件枚举,
        所以 Watching 行本身就是"开关已生效"的直接证据). 漏掉它会把已点亮的游戏
        误报成 `game-toggle-off`.
        """
        e = self._parse(self._DISABLED_LINE + self._DS3_LAUNCH_WATCH).entries["374320"]
        self.assertTrue(e.toggle_effective)
        self.assertEqual(e.cloud_state, "enabled")
        self.assertFalse(e.never_synced)
        self.assertEqual(e.watching_files, [
            r"C:\Users\x\AppData\Roaming\DarkSoulsIII\01100001499602d2\DS30000.sl2"
        ])

    def test_toggle_off_again_when_disabled_after_watching(self) -> None:
        """监视行之后又出现禁用行 (重启 Steam 清掉了开关) => 仍判禁用."""
        e = self._parse(self._DS3_LAUNCH_WATCH + self._DISABLED_LINE).entries["374320"]
        self.assertFalse(e.toggle_effective)
        self.assertEqual(e.cloud_state, "disabled")

    def test_incomplete_sync_detected(self) -> None:
        log = self._parse(
            "[t] [AppID 105600] AutoCloud detected incomplete sync after previous run\n"
        )
        self.assertEqual(log.entries["105600"].incomplete_sync_count, 1)
        self.assertIn("105600", log.incomplete_appids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
