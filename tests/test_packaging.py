#!/usr/bin/env python3
"""发行包的不变量测试 —— 锁住"打包"这条链上所有"改了代码却忘了改配套"的坑.

这些断言都来自实际踩过的坑:

* **`.bat` 的硬约束**: 纯 ASCII + CRLF(发行时统一) + 只用 `%~dp0` 相对路径.
  非 ASCII 输出与批处理解析器一起用不可靠; 名字有中文的路径会让 `goto` 行为异常.
* **单文件版必须覆盖全部 `src/*`**: 少内嵌一个模块, 单文件版会在朋友机上
  `ModuleNotFoundError`, 而仓库版完全正常 —— 本地测不出来.
* **内嵌顺序不能靠名字排序**: `steam_console_send` 在 import 时就要 `win_dpi`
  已经把进程切成 DPI 感知, 排反了直接崩.
* **版本号单一来源**: 探测器 `--version`、发行包名、搬运器 `--version` 曾各写一份,
  很容易改漏一处.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import build_exe  # noqa: E402
import build_release  # noqa: E402
import build_single_file as bsf  # noqa: E402
import machine_probe as mp  # noqa: E402
from src.version import VERSION  # noqa: E402

PACKAGING = PROJECT_ROOT / "packaging"
BATS = sorted(PACKAGING.glob("*.bat"))

GOTO_RE = re.compile(r"^\s*(?:goto|call)\s+:(\w+)\s*$", re.IGNORECASE)
LABEL_RE = re.compile(r"^:(\w+)\s*$")
DRIVE_RE = re.compile(r"[A-Za-z]:\\")


class TestBatHygiene(unittest.TestCase):
    """`.bat` 的静态约束 (沙箱里没有 cmd.exe, 只能这样验)."""

    def test_there_is_at_least_one_bat(self) -> None:
        self.assertTrue(BATS, "packaging/ 下没有 .bat")

    def test_pure_ascii(self) -> None:
        for bat in BATS:
            with self.subTest(bat=bat.name):
                bad = [
                    (i + 1, ln)
                    for i, ln in enumerate(bat.read_bytes().split(b"\n"))
                    if any(c > 127 for c in ln)
                ]
                self.assertFalse(
                    bad, f"{bat.name} 含非 ASCII 字节 (中文要放 .md): {bad[:3]}"
                )

    def test_no_lone_cr(self) -> None:
        """CRLF 可以, 但**裸 CR**会让解析器错乱 —— 发行时统一转 CRLF."""
        for bat in BATS:
            with self.subTest(bat=bat.name):
                data = bat.read_bytes()
                lone = [
                    i
                    for i, c in enumerate(data)
                    if c == 0x0D and (i + 1 >= len(data) or data[i + 1] != 0x0A)
                ]
                self.assertFalse(lone, f"{bat.name} 存在裸 CR (偏移 {lone[:5]})")

    def test_ends_with_newline(self) -> None:
        for bat in BATS:
            with self.subTest(bat=bat.name):
                self.assertTrue(bat.read_bytes().endswith(b"\n"), bat.name)

    def test_has_chcp_and_cd_to_own_dir(self) -> None:
        for bat in BATS:
            text = bat.read_text(encoding="ascii")
            with self.subTest(bat=bat.name):
                self.assertIn("chcp 65001", text, bat.name)
                self.assertIn('cd /d "%~dp0"', text, bat.name)

    def test_goto_targets_exist(self) -> None:
        """`goto :x` / `call :x` 的目标标签必须真的定义了 (`:eof` 是内置的).

        悬空 goto 在 Windows 上的行为是**静默跳到文件末尾并退出** ——
        朋友那里表现为"窗口闪一下就没了", 极难定位, 所以必须静态挡住.
        """
        for bat in BATS:
            text = bat.read_text(encoding="ascii")
            labels = {m.group(1).lower() for m in
                      (LABEL_RE.match(ln) for ln in text.splitlines()) if m}
            targets = {m.group(1).lower() for m in
                       (GOTO_RE.match(ln) for ln in text.splitlines()) if m}
            dangling = sorted(targets - labels - {"eof"})
            with self.subTest(bat=bat.name):
                self.assertFalse(dangling, f"{bat.name} 悬空跳转: {dangling}")

    def test_no_absolute_drive_paths_outside_comments(self) -> None:
        """可执行行里不许出现 `X:\\` —— 一律用 `%~dp0` 相对定位.

        REM 注释里写示例路径是允许的 (那是给人看的), 所以先剔掉注释行.
        """
        for bat in BATS:
            offenders = []
            for i, ln in enumerate(bat.read_text(encoding="ascii").splitlines()):
                code = ln.strip()
                if code.upper().startswith("REM"):
                    continue
                if DRIVE_RE.search(code):
                    offenders.append((i + 1, code[:70]))
            with self.subTest(bat=bat.name):
                self.assertFalse(offenders, f"{bat.name} 用了绝对路径: {offenders[:3]}")


class TestBridgeBat(unittest.TestCase):
    """搬运器入口脚本的语义约束."""

    def setUp(self) -> None:
        self.path = PACKAGING / "RUN-BRIDGE.bat"
        self.text = self.path.read_text(encoding="ascii")

    def test_exists(self) -> None:
        self.assertTrue(self.path.is_file())

    def test_defaults_to_wizard(self) -> None:
        """双击 (不给参数) 必须落到交互向导, 而不是某个需要参数的子命令."""
        self.assertRegex(self.text, r'if not defined ARGS set "ARGS=wizard"')

    def test_locates_both_program_forms(self) -> None:
        self.assertIn("SteamCloudBridgeBridge.exe", self.text)
        self.assertIn("SteamCloudBridgeBridge.py", self.text)

    def test_reuses_the_shared_fallback_chain(self) -> None:
        """三档降级: 自带 exe -> 自带 runtime -> PATH 上的 py/python/python3."""
        for probe in ("runtime\\python.exe", "where py", "where python", "where python3"):
            with self.subTest(probe=probe):
                self.assertIn(probe, self.text)

    def test_warns_about_steam_and_confirmation(self) -> None:
        upper = self.text.upper()
        self.assertIn("STEAM", upper)
        self.assertIn("YES", upper)

    def test_forwards_arguments_verbatim(self) -> None:
        self.assertIn('set "ARGS=%*"', self.text)


class TestProbeBatSendHints(unittest.TestCase):
    """探测器入口脚本的"发哪个 / 别发哪个"提示必须与报告目录分层一致.

    背景: 未脱敏完整档从 `reports\\machine_profile.json` 挪进了
    `reports\\private\\` 之后, `.bat` 里的 `Do NOT send:` 仍指着旧路径 ——
    面客消息指向一个不存在的文件, 等于没提示. 这类漂移必须由测试挡住.
    """

    PROBE_BATS = ("RUN-PROBE.bat", "RUN-PROBE-TARGET.bat")

    def _text(self, name: str) -> str:
        return (PACKAGING / name).read_text(encoding="ascii")

    def test_do_not_send_points_at_private_dir(self) -> None:
        for name in self.PROBE_BATS:
            with self.subTest(bat=name):
                text = self._text(name)
                self.assertRegex(
                    text,
                    r"Do NOT send:\s+reports(-4b)?\\private\\machine_profile\.json",
                )

    def test_no_hint_points_at_the_old_root_path(self) -> None:
        """`...\\machine_profile.json` 前面没有 private\\ 的都算旧路径残留."""
        for name in self.PROBE_BATS:
            with self.subTest(bat=name):
                text = self._text(name)
                stale = re.findall(r"reports(-4b)?\\machine_profile\.json", text)
                self.assertEqual(stale, [], f"{name} 仍指向旧完整档路径: {stale}")

    def test_layout_constant_matches_private_subdir(self) -> None:
        """机器可读的那份布局常量也得说 private/ (别只改 .bat)."""
        text = (PROJECT_ROOT / "tools" / "machine_probe.py").read_text(encoding="utf-8")
        self.assertIn('"private"', text)
        self.assertIn("private/machine_profile.json", text)


class TestSingleFileUniverse(unittest.TestCase):
    """单文件发行版的模块清单与安装顺序."""

    def test_every_src_module_is_embedded_somewhere(self) -> None:
        """磁盘上每个 src 模块都必须至少进**某个**构建目标的清单.

        看的是所有目标的**并集**: `src/gui/*` 只出现在 gui 目标的清单里,
        不在搬运器那份里 —— 这不是遗漏, 是分层.
        """
        on_disk = {
            p.relative_to(PROJECT_ROOT).as_posix()
            for p in sorted((PROJECT_ROOT / "src").rglob("*.py"))
        }
        declared = {
            rel
            for spec in bsf.TARGETS.values()
            for _n, rel in spec["modules"]
            if rel.startswith("src/")  # tools/ 那部分另有清单 (TOOLS_MODULES)
        }
        self.assertEqual(
            on_disk,
            declared,
            "src/ 下的模块与构建清单对不上 —— 新加的模块忘记内嵌了?\n"
            f"  只在磁盘上: {sorted(on_disk - declared)}\n"
            f"  只在清单里: {sorted(declared - on_disk)}",
        )

    def test_bridge_embeds_everything_outside_the_gui_package(self) -> None:
        """**除 `src/gui/**` 外**的模块都必须在搬运器的清单里.

        单文件是靠 `sys.modules` 重建模块表的, 漏一个不会在构建期报错 ——
        只会在用户机器上跑到那一步时 `ModuleNotFoundError`. 所以按包名划一条线:
        CLI 版不带界面代码, 但界面之外的一个都不能少.
        """
        gui_prefix = "src/gui/"
        on_disk = {
            p.relative_to(PROJECT_ROOT).as_posix()
            for p in sorted((PROJECT_ROOT / "src").rglob("*.py"))
            if not p.relative_to(PROJECT_ROOT).as_posix().startswith(gui_prefix)
        }
        declared = {rel for _, rel in bsf.SRC_MODULES}
        self.assertEqual(
            on_disk,
            declared,
            "搬运器清单缺了非 GUI 模块 (单文件版会 ImportError):\n"
            f"  只在磁盘上: {sorted(on_disk - declared)}\n"
            f"  只在清单里: {sorted(declared - on_disk)}",
        )

    def test_gui_target_embeds_the_gui_package(self) -> None:
        """gui 目标必须带上整个 `src/gui/**`, 且带上全部内核."""
        declared = {rel for _n, rel in bsf.TARGETS["gui"]["modules"]}
        for rel in ("src/gui/__init__.py", "src/gui/app.py", "src/gui/service.py",
                    "src/gui/theme.py", "src/gui/widgets.py"):
            with self.subTest(rel=rel):
                self.assertIn(rel, declared)
        self.assertTrue({rel for _n, rel in bsf.SRC_MODULES} <= declared)

    def test_probe_list_is_a_subset_of_the_full_list(self) -> None:
        full = {rel for _, rel in bsf.SRC_MODULES}
        probe = {rel for _, rel in bsf.PROBE_SRC_MODULES}
        self.assertTrue(probe <= full, sorted(probe - full))

    def test_probe_list_contains_what_it_imports(self) -> None:
        """探测器入口 import 的 src 模块必须在它自己的内嵌清单里."""
        text = (PROJECT_ROOT / bsf.TARGETS["probe"]["entry"]).read_text(encoding="utf-8")
        needed = set(re.findall(r"^from (src[\w.]*) import", text, re.MULTILINE))
        declared = {n for n, _ in bsf.PROBE_SRC_MODULES}
        # `from src.steam import x` 依赖的是包本身
        self.assertTrue(needed <= declared, sorted(needed - declared))

    def test_win_dpi_is_installed_before_steam_console_send(self) -> None:
        names = [n for n, _ in bsf.TOOLS_MODULES]
        self.assertLess(names.index("win_dpi"), names.index("steam_console_send"))

    def test_topological_order_respects_real_imports(self) -> None:
        """按 AST 扫出来的依赖做拓扑排序后, 每个模块都必须排在被依赖者之后.

        逐个目标验 —— gui 目标多带一层 `src/gui/*`, 它内部的依赖关系
        (app -> widgets -> theme) 同样必须成立.
        """
        for target, spec in bsf.TARGETS.items():
            with self.subTest(target=target):
                declared = spec["modules"]
                ordered = bsf._order_modules(declared)
                known = {n for n, _ in declared}
                pos = {n: i for i, (n, _) in enumerate(ordered)}
                for name, rel in ordered:
                    deps = bsf._module_deps(
                        name, (PROJECT_ROOT / rel).read_text(encoding="utf-8"), known
                    )
                    for d in sorted(deps):
                        with self.subTest(module=name, dep=d):
                            self.assertLess(
                                pos[d], pos[name], f"{name} 排在了它依赖的 {d} 之前"
                            )

    def test_order_is_deterministic(self) -> None:
        for target, spec in bsf.TARGETS.items():
            with self.subTest(target=target):
                declared = spec["modules"]
                self.assertEqual(bsf._order_modules(declared), bsf._order_modules(declared))

    def test_cycle_is_reported_not_ignored(self) -> None:
        """循环依赖不可能靠"内嵌 + 按序 exec"还原, 必须构建期就报错."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("from b import x\n", encoding="utf-8")
            (root / "b.py").write_text("from a import y\n", encoding="utf-8")
            old = bsf.PROJECT_ROOT
            bsf.PROJECT_ROOT = root
            try:
                with self.assertRaises(SystemExit) as ctx:
                    bsf._order_modules([("a", "a.py"), ("b", "b.py")])
            finally:
                bsf.PROJECT_ROOT = old
        self.assertIn("循环依赖", str(ctx.exception))

    def test_both_targets_write_under_dist(self) -> None:
        for name, spec in bsf.TARGETS.items():
            with self.subTest(target=name):
                out = Path(spec["out"])
                self.assertEqual(out.parent.as_posix(), "dist")
                self.assertTrue((PROJECT_ROOT / spec["entry"]).is_file())

    def test_entry_sanitiser_keeps_the_file_parseable(self) -> None:
        """剥掉该剥的行之后, 入口仍然是合法 Python, 且剥干净了.

        探测器的入口**保留** `sys.path.insert(0, PROJECT_ROOT)` (单文件里它指向
        自身所在目录的上一级, 无害); 搬运器的入口必须剥掉 (它的相对位置会指错).
        所以这里按各目标自己声明的 `strip` 模式来验, 而不是假设都一样.
        """
        import ast

        for name, spec in bsf.TARGETS.items():
            with self.subTest(target=name):
                raw = (PROJECT_ROOT / spec["entry"]).read_text(encoding="utf-8")
                patched = bsf._sanitize_entry(raw, spec["strip"])
                ast.parse(patched)  # 不抛即通过
                for pat in spec["strip"]:
                    self.assertIsNone(
                        re.search(pat, patched, re.MULTILINE),
                        f"{name}: 模式 {pat!r} 仍然命中",
                    )
                self.assertNotIn("from __future__ import annotations", patched)

    def test_bridge_entry_loses_only_broken_path_lines(self) -> None:
        """搬运器的入口被剥掉的是"指错目录"的那几行, 别把有用的也剥了."""
        spec = bsf.TARGETS["bridge"]
        raw = (PROJECT_ROOT / spec["entry"]).read_text(encoding="utf-8")
        patched = bsf._sanitize_entry(raw, spec["strip"])
        for gone in (
            "sys.path.insert(0, str(PROJECT_ROOT))",
            "PROJECT_ROOT = Path(__file__).resolve().parent.parent",
        ):
            self.assertNotIn(gone, patched)
        # 这些必须还在 —— 不然单文件版会缺入口或参数
        for keep in ("def build_parser(", "def main(", 'sub.add_parser("wizard"'):
            self.assertIn(keep, patched)


class TestReleaseWiring(unittest.TestCase):
    """发行构建器的输入清单与其它模块的约定必须对齐."""

    def test_version_has_one_source(self) -> None:
        self.assertEqual(mp.VERSION, VERSION)
        self.assertEqual(build_release.RELEASE_VERSION, VERSION)
        self.assertEqual(build_release.PKG_NAME, f"SteamCloudBridge-v{VERSION}")

    def test_declared_inputs_exist(self) -> None:
        for name in build_release.DOC_FILES:
            with self.subTest(kind="doc", name=name):
                self.assertTrue((PACKAGING / name).is_file())
        for name in build_release.BAT_FILES:
            with self.subTest(kind="bat", name=name):
                self.assertTrue((PACKAGING / name).is_file())
        for src, _dst in build_release.PY_FILES:
            with self.subTest(kind="py", src=src.name):
                self.assertTrue(src.is_file())

    def test_every_shipped_bat_is_documented(self) -> None:
        """发货的 `.bat` 与面客 README 里提到的 `.bat` 必须**一一对应**.

        两头都会出事:
        * 清单里有、README 没写 —— 用户拿到一个不知道干什么的脚本;
        * README 写了、清单里没有 —— 用户照着找却找不到 (v1.1.0 差点把
          向导写成入口却忘了进清单).
        故意不发的那一个 (`RUN-CROSS-PREPARE.bat`) 因此也**不能**出现在 README 里.
        """
        shipped = set(build_release.BAT_FILES)
        text = "\n".join(
            (PACKAGING / name).read_text(encoding="utf-8")
            for name in build_release.DOC_FILES
        )
        mentioned = set(re.findall(r"RUN-[A-Z0-9-]+\.bat", text))
        self.assertEqual(shipped - mentioned, set(), "这些脚本发了但文档里没写")
        self.assertEqual(mentioned - shipped, set(), "文档写了但包里没有")

    def test_the_wizard_is_the_primary_entry_point(self) -> None:
        """v1.1.0 定的规矩: **只记一个** —— 图形向导必须排在清单第一位.

        不是洁癖: 非技术用户拿到一堆 `.bat` 会直接卡在"先开哪个".
        清单顺序就是 README 表格顺序, 第一位必须是向导.
        """
        self.assertEqual(build_release.BAT_FILES[0], "RUN-GUI.bat")
        self.assertIn("SteamCloudBridgeGUI.exe", build_release.EXE_NAMES)
        readme = (PACKAGING / "README.md").read_text(encoding="utf-8")
        self.assertIn("只需要记住 `RUN-GUI.bat` 一个", readme)

    def test_package_layout_separates_the_wizard_from_the_rest(self) -> None:
        """v1.1.1 布局: 顶层**只留**启动器 + 向导本体, 其余全部进 `support/`.

        动机是用户拿到包后的原话: 十几个文件平铺在一个文件夹里, "还是乱".
        这里锁四件事 —— 顶层清单、`support/` 前缀、清单项齐落点,
        以及 `verify_release` **独立写的那份**布局常量必须与打包器一致
        (两边一起漂 = 验收阶段形同虚设, 所以故意写两份再断言相等).
        """
        import verify_release  # tools/ 已在 sys.path 上

        self.assertEqual(build_release.dest_of("RUN-GUI.bat"), "RUN-GUI.bat")
        self.assertEqual(
            build_release.dest_of("SteamCloudBridgeGUI.exe"), "SteamCloudBridgeGUI.exe"
        )
        self.assertEqual(
            build_release.dest_of("README.md"), f"{build_release.SUPPORT_DIR}/README.md"
        )
        self.assertEqual(
            set(build_release.FRONT_NAMES),
            {"RUN-GUI.bat", "SteamCloudBridgeGUI.exe"},
            "顶层只该有启动器和向导本体",
        )
        self.assertEqual(verify_release.SUPPORT_DIR, build_release.SUPPORT_DIR)
        self.assertEqual(
            set(verify_release.FRONT_FILES), set(build_release.FRONT_NAMES)
        )
        # 每个清单项都要有落点, 而且顶层**恰好**只有那两个
        laid = {
            build_release.dest_of(n)
            for n in (
                [dst for dst, _ in build_release.SINGLE_FILES]
                + list(build_release.BAT_FILES)
                + list(build_release.DOC_FILES)
                + [dst for _src, dst in build_release.PY_FILES]
                + list(build_release.EXE_NAMES)
                + ["LICENSE", "VERSION.txt"]
            )
        }
        self.assertEqual({p for p in laid if "/" not in p}, set(build_release.FRONT_NAMES))

    def test_packaged_docs_carry_the_current_version(self) -> None:
        """面客文档头部的版本号必须等于 `src/version.py`.

        踩过: 升版本时忘了改面客文档, 于是发行包里
        `VERSION.txt` 说 0.2.2、使用说明说 0.2.1 —— 只看文件名看不出来.
        这里只检查**第一行**的版本号 (正文里的历史版本引用不改).
        v1.0.0 起面客文档只剩一份 `README.md`, 所以直接遍历 `DOC_FILES`.
        """
        self.assertTrue(build_release.DOC_FILES, "面客文档清单不能为空")
        for name in build_release.DOC_FILES:
            with self.subTest(doc=name):
                first = (PACKAGING / name).read_text(encoding="utf-8").splitlines()[0]
                found = re.findall(r"v(\d+\.\d+\.\d+)", first)
                self.assertEqual(found, [VERSION], first)

    def test_single_file_names_match_the_builder(self) -> None:
        """`build_release` 要的单文件名, 必须与 `build_single_file` 产出的同名.

        不一致的后果很隐蔽: 构建器把文件写到一个新名字上, 发行包还在拷旧名字,
        于是包里躺着上一版 —— 只有对比内容才看得出来.
        """
        want = {dst for dst, _ in build_release.SINGLE_FILES}
        got = {Path(spec["out"]).name for spec in bsf.TARGETS.values()}
        self.assertEqual(want, got)
        by_name = {Path(spec["out"]).name: spec["out"] for spec in bsf.TARGETS.values()}
        for dst, src in build_release.SINGLE_FILES:
            with self.subTest(name=dst):
                self.assertEqual(src.relative_to(PROJECT_ROOT).as_posix(), by_name[dst])

    def test_exe_names_match_the_exe_builder(self) -> None:
        want = set(build_release.EXE_NAMES)
        got = {spec.exe_name for spec in build_exe.TARGETS.values()}
        self.assertEqual(want, got)
        for spec in build_exe.TARGETS.values():
            with self.subTest(py=spec.single_file.name):
                self.assertEqual(spec.single_file.parent.name, "dist")

    def test_every_bridge_flag_used_in_docs_exists(self) -> None:
        """文档里写给朋友的命令必须真能解析 —— 否则朋友照着敲会直接报错."""
        import bridge  # tools/ 已在 sys.path 上

        parser = bridge.build_parser()
        for argv in (
            ["wizard"],
            ["wizard", "--appid", "374320"],
            ["status", "--appid", "374320"],
            ["plan", "--appid", "374320", "--source", "x.sl2"],
            ["apply", "--appid", "374320", "--source", "x.sl2", "--yes"],
            ["apply", "--appid", "374320", "--source", "x.sl2", "--then-sync", "--yes"],
            ["sync", "--appid", "374320", "--start-steam", "--enable"],
            ["channel", "--appid", "374320", "--enable"],
            ["daily", "--appid", "374320", "--manual", "--launch"],
            ["daily", "--appid", "374320", "--start-steam"],
            ["rollback", "--snapshot", "s.json"],
            ["rollback", "--snapshot", "s.json", "--yes"],
            # v1.2.0 魂二: 这三条是面客 README 第六节里写给用户的原样命令
            ["save-status"],
            ["save-out", "--to", "D:/OneDrive/saves"],
            ["save-in", "--from", "D:/OneDrive/saves"],
            ["save-in", "--from", "D:/OneDrive/saves", "--dry-run"],
            ["save-in", "--from", "D:/OneDrive/saves", "--yes"],
        ):
            with self.subTest(argv=argv):
                ns = parser.parse_args(argv)
                self.assertTrue(callable(getattr(ns, "func", None)))

    def test_bridge_exposes_version(self) -> None:
        import bridge

        opts = {
            o for a in bridge.build_parser()._actions for o in getattr(a, "option_strings", [])
        }
        self.assertIn("--version", opts)

    def test_packaging_docs_point_at_the_bridge_entry(self) -> None:
        for name in build_release.DOC_FILES:
            text = (PACKAGING / name).read_text(encoding="utf-8")
            with self.subTest(doc=name):
                self.assertIn("RUN-BRIDGE.bat", text)


class TestSyncBat(unittest.TestCase):
    """v1.0 日常入口 `RUN-SYNC.bat` 的语义约束。

    它是"装完一直生效"的等价做法：双击一次 = 点亮本会话通道 (+ 可选启动游戏)。
    这里锁住三件事 —— 默认落到 `daily`、默认**手敲**、默认参数**真能解析**。
    """

    def setUp(self) -> None:
        self.path = PACKAGING / "RUN-SYNC.bat"
        self.text = self.path.read_text(encoding="ascii")

    def test_exists(self) -> None:
        self.assertTrue(self.path.is_file())

    def test_defaults_to_daily(self) -> None:
        """双击 (不给参数) 必须落到 `daily`, 而不是交互向导或某个要参数的子命令。"""
        self.assertRegex(self.text, r'if not defined ARGS set "ARGS=daily ')

    def test_defaults_to_manual_light_up(self) -> None:
        """默认必须是**手敲**（自动发键在别人的机器上从没成功过）。"""
        m = re.search(r'if not defined ARGS set "ARGS=([^"]+)"', self.text)
        self.assertIsNotNone(m, self.text)
        self.assertIn("--manual", m.group(1))

    def test_default_args_really_parse(self) -> None:
        """`.bat` 里的默认参数必须能被真实的 CLI 解析 —— 否则双击就报错。"""
        import bridge  # tools/ 已在 sys.path 上

        m = re.search(r'if not defined ARGS set "ARGS=([^"]+)"', self.text)
        self.assertIsNotNone(m, self.text)
        argv = m.group(1).split()
        self.assertEqual(argv[0], "daily")
        ns = bridge.build_parser().parse_args(argv)
        self.assertTrue(callable(ns.func))
        self.assertEqual(ns.appid, "374320")

    def test_locates_both_program_forms(self) -> None:
        self.assertIn("SteamCloudBridgeBridge.exe", self.text)
        self.assertIn("SteamCloudBridgeBridge.py", self.text)


class TestReleaseStrayGuard(unittest.TestCase):
    """展开目录里的残留**不能**被带进包。

    踩过: 有人把上一版 zip 解压进了 `release/<pkg>/`, 构建器当时用 `rglob("*")` 列文件,
    于是那份副本被整份打进新包 —— 17,966,251 B 变 35,932,952 B, 除了体积之外没有任何迹象。
    现在只打包**清单内**的文件, 残留只警告。
    """

    def test_only_expected_files_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "RUN-PROBE.bat").write_text("x", encoding="ascii")
            (out / "README.md").write_text("x", encoding="utf-8")
            listed, stray = build_release.select_packaged_files(
                out, ["RUN-PROBE.bat", "README.md", "VERSION.txt"]
            )
            self.assertEqual([p.name for p in listed], ["README.md", "RUN-PROBE.bat"])
            self.assertEqual(stray, [])

    def test_stray_file_and_directory_are_reported_not_packed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "README.md").write_text("x", encoding="utf-8")
            (out / "leftover.txt").write_text("x", encoding="utf-8")
            nested = out / "SteamCloudBridge-v1.0.0"
            nested.mkdir()
            (nested / "README.md").write_text("old", encoding="utf-8")

            listed, stray = build_release.select_packaged_files(out, ["README.md"])
            self.assertEqual([p.name for p in listed], ["README.md"])
            self.assertEqual(
                sorted(p.relative_to(out).as_posix() for p in stray),
                ["SteamCloudBridge-v1.0.0/README.md", "leftover.txt"],
            )

    def test_missing_expected_file_is_not_invented(self) -> None:
        """清单里写了、磁盘上没有的文件不能凭空出现在打包列表里。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            listed, stray = build_release.select_packaged_files(out, ["README.md"])
            self.assertEqual(listed, [])


class TestSingleFileSelfCheckInterpreter(unittest.TestCase):
    """单文件自检的**解释器**必须可指定.

    踩过 (v1.2.0): 拿一个**没有 tkinter** 的解释器跑 `--target gui --check`,
    会得到 `!! 自检失败 (exit=2)` —— 看起来像"GUI 单文件坏了", 其实产物没问题
    (exe 自带 tkinter). 这种假失败很容易让人去改本来正确的代码, 所以:
    ① 自检要能指定解释器; ② 真因要写在输出里.
    """

    def test_check_interpreter_is_configurable(self) -> None:
        import inspect

        self.assertIn("python", inspect.signature(bsf.self_check).parameters)

    def test_cli_exposes_the_python_option(self) -> None:
        opts = {a for act in bsf._build_parser()._actions for a in getattr(act, "option_strings", [])}
        self.assertIn("--python", opts)

    def test_python_option_help_names_the_tkinter_requirement(self) -> None:
        """帮助里必须点明"要带 tkinter", 否则下次还是会踩同一个坑."""
        parser = bsf._build_parser()
        action = next(a for a in parser._actions if "--python" in getattr(a, "option_strings", []))
        self.assertIn("tkinter", action.help or "")


if __name__ == "__main__":
    unittest.main()
