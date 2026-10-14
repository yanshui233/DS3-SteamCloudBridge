#!/usr/bin/env python3
"""搬运层 (M2) 的回归测试.

覆盖 `src/transport/` 四个模块. 全部跑在 `make_fake_steam.py` 造的**假 Steam 环境**
里 (环境变量重定向 + `--steam-root`), **不接触任何真机数据**.

重点锁住三件曾经踩过 / 差点踩到的事:

1. **幽灵候选** —— `cloud_log` 的 `Watching file` 行会累积历史 (实测: 已清理的诱饵
   仍留在日志里). 若把它照单全收, 邻居文件会被当成搬运目标. 现在只有 remotecache
   **无登记**时才拿它兜底.
2. **落点不许猜** —— 没有任何证据时必须返回 `unresolved` 并拒绝执行, 绝不自己拼
   目录名 (事故 D3: 把游戏安装目录当存档目录写入).
3. **写入必须刷新 mtime** —— 保留源文件的旧 mtime 会让 Steam 打印
   `Skipping un-modified file`, 改动永远不上云. 所以断言"写入后的 mtime 是新的".
"""

from __future__ import annotations

import hashlib
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
from src.transport import autocloud, guard  # noqa: E402
from src.transport import plan as plan_mod  # noqa: E402
from src.transport import target as target_mod  # noqa: E402

DS3 = "374320"


class FakeEnvCase(unittest.TestCase):
    """公共夹具: 一个 synced 场景的假 Steam 环境."""

    scenario = "synced"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scb-transport-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.manifest = mfs.build(
            self.tmp / "env", self.scenario, mfs.DEFAULT_ACCOUNT_ID, 8, force=False
        )
        env = mock.patch.dict(os.environ, self.manifest["env"])
        env.start()
        self.addCleanup(env.stop)

        self.install = discovery.SteamInstall(
            root=Path(self.manifest["steam_root"]), source="test"
        )
        self.aid = mfs.DEFAULT_ACCOUNT_ID
        self.cloud = cloud_log.parse_cloud_log(self.install.logs / "cloud_log.txt")

    # ---- 辅助 ----

    def resolve(self, appid: str = DS3, **kw):
        # 每次都重新解析日志: 测试里会删/改 cloud_log, 不能吃 setUp 时的缓存.
        log_path = self.install.logs / "cloud_log.txt"
        cloud = (
            cloud_log.parse_cloud_log(log_path) if log_path.is_file() else cloud_log.CloudLog()
        )
        return target_mod.resolve_targets(self.install, appid, self.aid, cloud=cloud, **kw)

    def write_save_root_marker(self, accountid: int) -> Path:
        """造 %APPDATA%\\DarkSoulsIII\\steam_autocloud.vdf (记录归属账号)."""
        root = Path(self.manifest["save_file"]).parent.parent
        mfs._write_vdf(root / "steam_autocloud.vdf", "steam_autocloud.vdf",
                       {"accountid": str(accountid)})
        return root

    def make_source(self, name: str, payload: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(payload)
        return p


# ============================================================ target 解析


class TestNormalizeRel(unittest.TestCase):
    def test_backslash_and_slash_both_work(self) -> None:
        self.assertEqual(
            target_mod.normalize_rel("DarkSoulsIII/01100001499602d2/DS30000.sl2"),
            Path("DarkSoulsIII") / "01100001499602d2" / "DS30000.sl2",
        )
        self.assertEqual(
            target_mod.normalize_rel(r"DarkSoulsIII\01100001499602d2\DS30000.sl2"),
            Path("DarkSoulsIII") / "01100001499602d2" / "DS30000.sl2",
        )

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaises(ValueError):
            target_mod.normalize_rel("..\\..\\Windows\\System32\\evil.sl2")


class TestRootBases(unittest.TestCase):
    def test_uses_environment_redirect(self) -> None:
        """root 基目录必须读环境变量 —— 沙箱重定向靠的就是这个."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_appdata = Path(tmp) / "Roaming"
            with mock.patch.dict(os.environ, {"APPDATA": str(fake_appdata)}):
                bases = target_mod.root_bases(None, DS3, None)
            self.assertEqual(bases["4"].path, fake_appdata)

    def test_install_dir_root_requires_game_dir(self) -> None:
        bases_without = target_mod.root_bases(None, DS3, None)
        self.assertIsNone(bases_without["1"].path)
        bases_with = target_mod.root_bases(None, DS3, None, game_install_dir=Path("C:/G"))
        self.assertEqual(bases_with["1"].path, Path("C:/G"))


class TestResolveTargets(FakeEnvCase):
    def test_resolves_to_confirmed_by_cloud_log(self) -> None:
        res = self.resolve()
        primary = res.primary
        self.assertIsNotNone(primary)
        assert primary is not None
        self.assertEqual(primary.confidence, "confirmed-by-cloud-log")
        self.assertTrue(
            target_mod.same_path(primary.abs_path, self.manifest["save_file"]),
            f"落点应为 {self.manifest['save_file']}, 实际 {primary.abs_path}",
        )
        self.assertTrue(primary.exists)
        self.assertEqual(primary.remote_sha, self.manifest["save_sha1"])
        # 云端有副本 (synced 场景)
        self.assertTrue(primary.has_remote_copy)

    def test_account_dir_form_comes_from_machine_not_guess(self) -> None:
        """落点必须带机器自己的账号目录名, 不能靠拼."""
        res = self.resolve()
        assert res.primary is not None
        self.assertIn(self.manifest["hex_dir"], str(res.primary.abs_path))

    def test_no_ghost_candidates_from_cloud_log_history(self) -> None:
        """回归: cloud_log 里出现过的、但 remotecache 未登记的文件不得成为候选.

        真实场景: 诱饵验证器留下的 `scb-bait-*.sl2` 已从云端抹除, 但它的
        `Watching file` 行仍留在日志里. 照单全收会把幽灵文件当搬运目标.
        """
        log_path = self.install.logs / "cloud_log.txt"
        text = log_path.read_text(encoding="utf-8")
        ghost = str(Path(self.manifest["save_file"]).parent / "ghost-removed.sl2")
        text += f"[2026-10-12 01:30:16] [AppID {DS3}]     Watching file {ghost}\n"
        log_path.write_text(text, encoding="utf-8")

        cloud = cloud_log.parse_cloud_log(log_path)
        res = target_mod.resolve_targets(self.install, DS3, self.aid, cloud=cloud)
        names = [Path(str(t.abs_path)).name for t in res.targets]
        self.assertNotIn("ghost-removed.sl2", names)
        self.assertEqual(len(res.targets), 1)

    def test_cloud_log_fallback_only_when_remotecache_absent(self) -> None:
        """remotecache 缺席时, 才允许用 cloud_log 的 Watching 行兜底."""
        rc = self.install.userdata / str(self.aid) / DS3 / "remotecache.vdf"
        rc.unlink()
        res = self.resolve()
        self.assertEqual(len(res.targets), 1)
        self.assertTrue(res.targets[0].evidence[-1].startswith("cloud_log"))
        self.assertTrue(any("无条目" in n for n in res.notes))

    def test_no_evidence_at_all_refuses_to_guess(self) -> None:
        """没有任何证据 -> 不产生候选, 并明确拒绝自拼路径."""
        (self.install.userdata / str(self.aid) / DS3 / "remotecache.vdf").unlink()
        (self.install.logs / "cloud_log.txt").unlink()
        res = self.resolve()
        self.assertEqual(res.targets, [])
        self.assertIsNone(res.primary)
        self.assertTrue(any("拒绝自行拼目录名" in n for n in res.notes))

    def test_unresolvable_root_falls_back_to_cloud_log_abs_path(self) -> None:
        """root 枚举不认识时, 用 cloud_log 的同名绝对路径补上."""
        rc = self.install.userdata / str(self.aid) / DS3 / "remotecache.vdf"
        rel = rf"DarkSoulsIII\{self.manifest['hex_dir']}\DS30000.sl2"
        mfs._write_vdf(rc, DS3, {
            "ChangeNumber": "1",
            rel: {"root": "99", "size": "8", "localtime": "1", "time": "1",
                  "remotetime": "1", "sha": self.manifest["save_sha1"], "syncstate": "1"},
        })
        res = self.resolve()
        self.assertEqual(len(res.targets), 1)
        self.assertEqual(res.targets[0].confidence, "confirmed-by-cloud-log")
        self.assertTrue(
            target_mod.same_path(res.targets[0].abs_path, self.manifest["save_file"])
        )


class TestSaveRootHelpers(FakeEnvCase):
    def test_save_root_and_owner(self) -> None:
        root = self.write_save_root_marker(self.aid)
        save = Path(self.manifest["save_file"])
        self.assertTrue(target_mod.same_path(target_mod.save_root_of(save), root))
        self.assertEqual(target_mod.autocloud_accountid(root), self.aid)

    def test_missing_marker_returns_none(self) -> None:
        self.assertIsNone(target_mod.save_root_of(Path(self.manifest["save_file"])))


# ============================================================ 守卫


class TestGuards(FakeEnvCase):
    def _plan_for(self, source: Path, steam_is_running: bool = False, **kw):
        res = self.resolve()
        return plan_mod.build_plan(
            source=source, resolution=res, appid=DS3, accountid=self.aid,
            steam_is_running=steam_is_running, **kw,
        )

    def test_steam_running_blocks(self) -> None:
        src = self.make_source("copy.sl2", b"X" * 16)
        plan = self._plan_for(src, steam_is_running=True)
        self.assertTrue(plan.blocked)
        self.assertIn("STEAM_RUNNING", [g.code for g in plan.blockers])

    def test_same_file_blocks(self) -> None:
        plan = self._plan_for(Path(self.manifest["save_file"]))
        self.assertIn("SAME_FILE", [g.code for g in plan.blockers])

    def test_missing_source_blocks(self) -> None:
        plan = self._plan_for(self.tmp / "nope.sl2")
        self.assertIn("SOURCE_MISSING", [g.code for g in plan.blockers])

    def test_empty_source_blocks(self) -> None:
        src = self.make_source("empty.sl2", b"")
        plan = self._plan_for(src)
        self.assertIn("SOURCE_EMPTY", [g.code for g in plan.blockers])

    def test_cross_account_is_blocked(self) -> None:
        """目标存档根记录的账号与本机不一致 -> 拒绝 (红线)."""
        self.write_save_root_marker(self.aid + 999)
        src = self.make_source("copy.sl2", b"X" * 16)
        plan = self._plan_for(src)
        self.assertTrue(plan.blocked)
        self.assertIn("CROSS_ACCOUNT", [g.code for g in plan.blockers])

    def test_same_account_is_allowed(self) -> None:
        self.write_save_root_marker(self.aid)
        src = self.make_source("copy.sl2", b"X" * 16)
        plan = self._plan_for(src)
        self.assertNotIn("CROSS_ACCOUNT", [g.code for g in plan.guards])

    def test_unknown_account_warns_not_silent(self) -> None:
        """本机账号判不出来 (注册表 ActiveUser=0 且无唯一 userdata 线索) 时,
        跨账号红线**未生效**必须显式告警, 不许静默通过.

        回归: 旧实现 `_guard_cross_account` 遇到 `accountid is None` 直接 `return []`,
        红线连同提示一起消失 —— 报告读起来像"检查通过".
        """
        self.write_save_root_marker(self.aid)
        src = self.make_source("copy.sl2", b"X" * 16)
        plan = plan_mod.build_plan(
            source=src, resolution=self.resolve(), appid=DS3,
            accountid=None, steam_is_running=False,
        )
        codes = [g.code for g in plan.guards]
        self.assertIn("ACCOUNT_UNKNOWN", codes)
        # 没有账号可比对, 不能因此判 blocked; 但必须留痕
        self.assertFalse(plan.blocked)

    def test_target_in_install_dir_is_blocked(self) -> None:
        """事故 D3 的形态: 落点被解析到游戏安装目录."""
        rc = self.install.userdata / str(self.aid) / DS3 / "remotecache.vdf"
        rel = rf"DarkSoulsIII\{self.manifest['hex_dir']}\DS30000.sl2"
        mfs._write_vdf(rc, DS3, {
            "ChangeNumber": "1",
            rel: {"root": "1", "size": "8", "localtime": "1", "time": "1",
                  "remotetime": "0", "sha": "deadbeef", "syncstate": "1"},
        })
        manifests = discovery.load_app_manifests(
            discovery.load_libraries(self.install)
        )
        game_dir = manifests[DS3].install_dir
        res = self.resolve(game_install_dir=game_dir)
        plan = plan_mod.build_plan(
            source=self.make_source("copy.sl2", b"X" * 16), resolution=res,
            appid=DS3, accountid=self.aid, steam_is_running=False,
        )
        self.assertTrue(plan.blocked)
        self.assertIn("TARGET_IS_INSTALL_DIR", [g.code for g in plan.blockers])

    def test_no_target_blocks_with_actionable_hint(self) -> None:
        (self.install.userdata / str(self.aid) / DS3 / "remotecache.vdf").unlink()
        (self.install.logs / "cloud_log.txt").unlink()
        res = self.resolve()
        plan = plan_mod.build_plan(
            source=self.make_source("copy.sl2", b"X" * 16), resolution=res,
            appid=DS3, accountid=self.aid, steam_is_running=False,
        )
        self.assertTrue(plan.blocked)
        g = next(x for x in plan.blockers if x.code == "TARGET_UNRESOLVED")
        self.assertIn("启动一次该游戏", g.remedy)


class TestGuardCloud(FakeEnvCase):
    scenario = "disabled"

    def test_cloud_disabled_warns_but_does_not_block(self) -> None:
        res = self.resolve()
        self.assertTrue(res.cloud_disabled)
        src = self.make_source("copy.sl2", b"X" * 16)
        plan = plan_mod.build_plan(
            source=src, resolution=res, appid=DS3, accountid=self.aid,
            steam_is_running=False,
        )
        self.assertFalse(plan.blocked)
        codes = [g.code for g in plan.warnings]
        self.assertIn("CLOUD_DISABLED", codes)
        # 回归 (D7): 这条来自**全文件统计**, 文案必须自曝出处, 否则会被当成"当前就是禁用".
        msg = next(g.message for g in plan.warnings if g.code == "CLOUD_DISABLED")
        self.assertIn("历史统计", msg)
        self.assertIn("全文件", msg)


class TestSteamRunningProbe(unittest.TestCase):
    def test_returns_bool_or_none_never_raises(self) -> None:
        value = guard.steam_running()
        self.assertIn(value, (True, False, None))


# ============================================================ 计划


class TestBuildPlan(FakeEnvCase):
    def _plan(self, source: Path, **kw):
        res = self.resolve()
        entry = self.cloud.entries.get(DS3)
        return plan_mod.build_plan(
            source=source, resolution=res, appid=DS3, name="DARK SOULS III",
            accountid=self.aid, steam_is_running=False,
            cloud_patterns=entry.rule_patterns if entry else [],
            **kw,
        )

    def test_identical_target_skips(self) -> None:
        src = self.make_source("same.sl2", Path(self.manifest["save_file"]).read_bytes())
        plan = self._plan(src)
        self.assertEqual(plan.verdict, plan_mod.VERDICT_SKIP)
        self.assertFalse(plan.will_write)

    def test_different_content_plans_copy(self) -> None:
        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)
        plan = self._plan(src, backup_dir=self.tmp / "bak")
        self.assertEqual(plan.verdict, plan_mod.VERDICT_COPY)
        self.assertTrue(plan.will_write)
        kinds = [s.kind for s in plan.steps]
        self.assertEqual(kinds[:2], ["backup", "write"])
        self.assertIn("trigger-sync", kinds)
        self.assertIsNotNone(plan.backup_path)

    def test_companion_files_reported_with_staleness(self) -> None:
        """伴生文件必须出现在计划里 —— 这是 4b 的风险点."""
        save_dir = Path(self.manifest["save_file"]).parent
        (save_dir / "DS30000.co2").write_bytes(b"companion-co2" * 32)
        (save_dir / "DS30000.ds3os").write_bytes(b"companion-ds3os" * 32)
        # 把伴生文件 mtime 推到很久以前
        old = time.time() - 400 * 86400
        for n in ("DS30000.co2", "DS30000.ds3os"):
            os.utime(save_dir / n, (old, old))

        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)
        plan = self._plan(src)
        names = {c.name for c in plan.companions}
        self.assertIn("DS30000.co2", names)
        self.assertIn("DS30000.ds3os", names)
        # *.sl2 才走云, 伴生文件不走
        for c in plan.companions:
            self.assertFalse(c.cloud_synced, f"{c.name} 不该被判为云同步")
        self.assertTrue(any(c.stale for c in plan.companions))
        self.assertTrue(any("伴生文件" in n for n in plan.notes))

    def test_plan_is_json_serialisable(self) -> None:
        import json

        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)
        payload = self._plan(src).to_dict()
        json.dumps(payload, ensure_ascii=False)  # 不抛异常即可


class TestSha1(unittest.TestCase):
    def test_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.bin"
            data = b"SCB" * 1000
            p.write_bytes(data)
            self.assertEqual(plan_mod.sha1_of(p), hashlib.sha1(data).hexdigest())

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(plan_mod.sha1_of(Path("C:/definitely/not/here.bin")), "")


# ============================================================ 执行器


class TestApply(FakeEnvCase):
    def _copy_plan(self, src: Path, backup_dir: Path | None = None):
        res = self.resolve()
        return plan_mod.build_plan(
            source=src, resolution=res, appid=DS3, accountid=self.aid,
            steam_is_running=False, backup_dir=backup_dir,
        )

    def test_dry_run_touches_nothing(self) -> None:
        """关键: dry-run 必须一个字节都不写."""
        save = Path(self.manifest["save_file"])
        before = save.read_bytes()
        before_mtime = save.stat().st_mtime
        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)

        result = autocloud.apply_plan(self._copy_plan(src, self.tmp / "bak"), apply=False)

        self.assertFalse(result.applied)
        self.assertTrue(result.dry_run)
        self.assertEqual(save.read_bytes(), before)
        self.assertEqual(save.stat().st_mtime, before_mtime)
        self.assertFalse((self.tmp / "bak").exists())
        self.assertTrue(any("dry-run" in a for a in result.actions))

    def test_apply_writes_backs_up_and_verifies(self) -> None:
        save = Path(self.manifest["save_file"])
        original = save.read_bytes()
        payload = b"SCB-NEW-SAVE\n" + bytes(range(256)) * 4
        src = self.make_source("mod.sl2", payload)
        bak = self.tmp / "bak"

        with mock.patch.object(autocloud, "steam_running", return_value=False):
            result = autocloud.apply_plan(self._copy_plan(src, bak), apply=True)

        self.assertTrue(result.applied, result.error)
        self.assertTrue(result.verified)
        self.assertEqual(save.read_bytes(), payload)
        self.assertEqual(result.target_sha1_after, hashlib.sha1(payload).hexdigest())

        # 备份必须存在且是原始内容
        backups = list(bak.glob("*.scb-bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

        # 没有临时文件残留
        self.assertEqual(list(save.parent.glob("*.scb-tmp")), [])

    def test_apply_refreshes_mtime_instead_of_copying_source_mtime(self) -> None:
        """回归锁: 写入后 mtime 必须是**新的**, 不能沿用源文件的旧 mtime.

        保留旧 mtime 会让 Steam 打印 `Skipping un-modified file`, 改动永不上云.
        """
        save = Path(self.manifest["save_file"])
        payload = b"SCB-NEW-SAVE\n" + b"Y" * 128
        src = self.make_source("mod.sl2", payload)
        old = time.time() - 30 * 86400
        os.utime(src, (old, old))
        src_mtime = src.stat().st_mtime

        with mock.patch.object(autocloud, "steam_running", return_value=False):
            autocloud.apply_plan(self._copy_plan(src, self.tmp / "bak"), apply=True)

        written_mtime = save.stat().st_mtime
        self.assertGreater(written_mtime, src_mtime)
        self.assertLess(abs(written_mtime - time.time()), 120)

    def test_apply_atomic_replaces_whole_file(self) -> None:
        """替换后不应残留旧内容的尾巴 (原子替换而不是就地覆盖)."""
        save = Path(self.manifest["save_file"])
        original_len = save.stat().st_size
        payload = b"S" * (original_len // 2 + 1)
        src = self.make_source("mod.sl2", payload)

        with mock.patch.object(autocloud, "steam_running", return_value=False):
            result = autocloud.apply_plan(self._copy_plan(src, self.tmp / "bak"), apply=True)

        self.assertTrue(result.applied)
        self.assertEqual(save.stat().st_size, len(payload))

    def test_apply_refuses_when_steam_running(self) -> None:
        """执行前重查 Steam —— 防"计划时没跑、执行时跑了"的竞态."""
        save = Path(self.manifest["save_file"])
        before = save.read_bytes()
        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)

        with mock.patch.object(autocloud, "steam_running", return_value=True):
            result = autocloud.apply_plan(self._copy_plan(src, self.tmp / "bak"), apply=True)

        self.assertFalse(result.applied)
        self.assertIn("Steam", result.error)
        self.assertEqual(save.read_bytes(), before)

    def test_apply_refuses_blocked_plan(self) -> None:
        res = self.resolve()
        plan = plan_mod.build_plan(
            source=Path(self.manifest["save_file"]),  # 源==目标 -> blocked
            resolution=res, appid=DS3, accountid=self.aid, steam_is_running=False,
        )
        result = autocloud.apply_plan(plan, apply=True)
        self.assertFalse(result.applied)
        self.assertIn("拒绝执行", result.error)

    def test_apply_writes_journal(self) -> None:
        import json

        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)
        journal = self.tmp / "journal.jsonl"

        with mock.patch.object(autocloud, "steam_running", return_value=False):
            result = autocloud.apply_plan(
                self._copy_plan(src, self.tmp / "bak"), apply=True, journal_path=journal
            )

        self.assertTrue(result.applied)
        lines = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines() if x]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["appid"], DS3)
        self.assertTrue(lines[0]["verified"])
        self.assertEqual(lines[0]["target_sha1_after"], hashlib.sha1(src.read_bytes()).hexdigest())

    def test_backup_failure_aborts_before_overwrite(self) -> None:
        """备份失败必须中止 —— 宁可不搬, 也不能在没有退路时覆盖存档."""
        save = Path(self.manifest["save_file"])
        before = save.read_bytes()
        src = self.make_source("mod.sl2", b"SCB-DIFFERENT\n" + b"Z" * 64)

        with mock.patch.object(autocloud, "steam_running", return_value=False), mock.patch.object(
            autocloud, "backup_file", side_effect=OSError("disk full")
        ):
            result = autocloud.apply_plan(self._copy_plan(src, self.tmp / "bak"), apply=True)

        self.assertFalse(result.applied)
        self.assertIn("备份失败", result.error)
        self.assertEqual(save.read_bytes(), before)


class TestAtomicWrite(unittest.TestCase):
    def test_writes_content_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            src = d / "src.bin"
            dst = d / "dst.bin"
            src.write_bytes(b"hello-scb")
            dst.write_bytes(b"old-content-longer")
            autocloud.atomic_write_fresh(src, dst)
            self.assertEqual(dst.read_bytes(), b"hello-scb")
            self.assertEqual(list(d.glob("*.scb-tmp")), [])


if __name__ == "__main__":
    unittest.main()
