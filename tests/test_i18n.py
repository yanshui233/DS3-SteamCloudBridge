#!/usr/bin/env python3
"""多语言层 (`src/i18n.py`) 的不变量测试.

设计要点是"**中文原文就是 key**" (见 `src/i18n.py` 的模块 docstring). 这套设计
把"漏翻一条"从"界面上蹦一句中文"降级成"测试红" —— 前提是下面这几条断言一直在:

* **对齐**: 源码里每个 `i18n.t("…")` 的 key 都必须在 `CATALOG` 里有英文, 反过来
  `CATALOG` 里也不许留死条目 (改了文案忘了删旧条目, 表会越滚越大);
* **渲染期取值**: `STEPS` / `FAQ` 是模块级常量, 它们的文案**不**在建模块时翻译,
  而是渲染那一刻才 `i18n.t(...)`. 一旦有人图省事写成 `STEPS = (i18n.t("…"), …)`,
  语言就被冻结在 import 时刻 —— 这是本次实际踩过的坑 (`FAQ` 是 `AnnAssign`,
  第一版扫描漏了它), 所以单独有一条钉子;
* **中文不许当逻辑用**: 比较 / 下标 / 字典键 / `.get()` 参数里出现中文, 翻译之后
  会静默改变行为. 逻辑层 (`src/**` 除 `i18n.py` 外) 必须干净;
* **只有图形入口改语言**: 命令行版永远是中文 (用户存了英文, 再跑命令行不该看到
  半中半英). 这条还有个具体依赖: `tools/bridge.py` 靠 `"跨账号" in res.error`
  分支退出码 —— 要是 CLI 也跟着切语言, 那个判断会静默失效.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import i18n  # noqa: E402

try:  # 图形冒烟用; 精简解释器 (没有 tkinter) 上整类跳过
    import tkinter  # noqa: F401
except Exception:  # noqa: BLE001
    tkinter = None  # type: ignore[assignment]

SRC = PROJECT_ROOT / "src"
CJK = re.compile(r"[\u4e00-\u9fff]")

#: **故意**不翻译的片段: 语言选择器上必须写母语名 —— 把"中文"翻成"Chinese"会让
#: 只认母语的人在两个拉丁字母选项里找不到自己那一项. 检查"还有没有残留中文"时
#: 先把这些小片段摘掉再判, 否则一句"点右上角的 中文 / English"会被误报.
UNT_ALWAYS = ("中文 / English", "English", "中文")


def _leftover_cjk(text: str) -> bool:
    """摘掉故意保留的母语名之后, 还剩不剩中文."""
    for token in UNT_ALWAYS:
        text = text.replace(token, "")
    return bool(CJK.search(text))


# 把中文当"逻辑"用的四个位置. 这些地方的中文会参与程序行为, 翻译后行为就变了.
LOOKUP_ATTRS = ("get", "setdefault", "pop")


def _module_level_names(path: Path) -> dict[str, ast.AST]:
    """拿到模块顶层的 `名字 -> 值节点` (含 `x: T = …` 这种 AnnAssign)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out[node.target.id] = node.value
    return out


def _t_keys_in(path: Path) -> set[str]:
    """源码里所有 `i18n.t("<字面量>")` 的 key."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr != "t":
            continue
        if not isinstance(fn.value, ast.Name) or fn.value.id != "i18n":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            keys.add(first.value)
    return keys


def _all_t_keys() -> set[str]:
    out: set[str] = set()
    for base in (SRC, PROJECT_ROOT / "tools"):
        for p in sorted(base.rglob("*.py")):
            out |= _t_keys_in(p)
    return out


def _render_time_keys() -> set[str]:
    """`STEPS` / `FAQ` 里的字符串 —— 它们不写 `i18n.t`, 但在渲染时会被翻译."""
    values = _module_level_names(SRC / "gui" / "app.py")
    out: set[str] = set()
    for name in ("STEPS", "FAQ"):
        node = values.get(name)
        if node is None:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.add(sub.value)
    return out


def _all_string_literals() -> set[str]:
    """源码里出现过的全部字符串字面量 (含 docstring 之外的常量表).

    "死条目"按这个口径判: 只要 key 的原文在代码里还存在, 就不能算死 —— 因为
    `STEPS` / `FAQ` / `ds2.PROFILE.notes` 这类**模块级常量**里的文案本来就只以
    普通字面量出现, 等到用的时候才 `i18n.t()`. 反过来, 文案改了或者那块代码删了,
    旧条目就会从这里消失, 于是被揪出来.
    """
    out: set[str] = set()
    for base in (SRC, PROJECT_ROOT / "tools"):
        for p in sorted(base.rglob("*.py")):
            if p.name == "i18n.py":  # 表自己不算
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    out.add(node.value)
    return out


def _chinese_used_as_logic(path: Path) -> list[tuple[str, int, str]]:
    """找出"把中文当程序逻辑"的位置, 返回 `(类别, 行号, 片段)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node

    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value)):
            continue
        up = parent.get(id(node))
        why = None
        if isinstance(up, ast.Compare) and (up.left is node or node in up.comparators):
            why = "compare"
        elif isinstance(up, ast.Subscript) and (
            up.slice is node or (isinstance(up.slice, ast.Tuple) and node in up.slice.elts)
        ):
            why = "subscript"
        elif isinstance(up, ast.Dict) and node in up.keys:
            why = "dict-key"
        elif (
            isinstance(up, ast.Call)
            and isinstance(up.func, ast.Attribute)
            and up.func.attr in LOOKUP_ATTRS
            and node in up.args
        ):
            why = f"call-{up.func.attr}"
        if why:
            found.append((why, node.lineno, node.value))
    return found


class _LangIsolated(unittest.TestCase):
    """把模块级语言状态和环境变量包起来, 免得用例之间互相污染."""

    def setUp(self) -> None:
        self._lang = i18n.current()
        self._env = {k: os.environ.get(k) for k in ("SCB_LANG", "SCB_SETTINGS")}
        self._tmp = tempfile.TemporaryDirectory(prefix="scb-i18n-")
        os.environ["SCB_SETTINGS"] = str(Path(self._tmp.name) / "settings.json")
        os.environ.pop("SCB_LANG", None)

    def tearDown(self) -> None:
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        i18n.set_lang(self._lang)
        self._tmp.cleanup()

    @property
    def settings_file(self) -> Path:
        return Path(self._tmp.name) / "settings.json"


# ---------------------------------------------------------------- 表与源码对齐


class TestCatalogAlignsWithSource(unittest.TestCase):
    def test_every_t_key_has_english(self) -> None:
        """源码里 `i18n.t("…")` 的每个 key 都要有英文. 漏一个就红."""
        used = _all_t_keys()
        missing = sorted(used - set(i18n.keys()))
        self.assertEqual(missing, [], f"这些 key 没有英文: {missing[:5]}")

    def test_render_time_keys_have_english(self) -> None:
        """`STEPS` / `FAQ` 的文案在渲染时才翻译, 同样必须在表里."""
        keys = _render_time_keys()
        self.assertGreater(len(keys), 10, "没扫到 STEPS/FAQ, 扫描逻辑可能失效了")
        missing = sorted(keys - set(i18n.keys()))
        self.assertEqual(missing, [], f"STEPS/FAQ 缺英文: {missing[:5]}")

    def test_no_entries_for_removed_copy(self) -> None:
        """表里不留死条目: 文案改了、那块代码删了, 旧条目会越积越多."""
        alive = _all_string_literals()
        dead = sorted(set(i18n.keys()) - alive)
        self.assertEqual(dead, [], f"这些条目在源码里已经找不到了: {dead[:5]}")

    def test_every_value_is_a_real_english_sentence(self) -> None:
        for key, val in i18n.CATALOG.items():
            with self.subTest(key=key[:40]):
                self.assertTrue(val.strip(), "英文是空的")
                self.assertNotEqual(val, key, "英文与中文原文一模一样, 多半是忘了翻")
                self.assertFalse(
                    _leftover_cjk(val), f"英文里还有中文: {val[:60]!r}"
                )

    def test_placeholder_order_is_preserved(self) -> None:
        """`{0}` / `{1}` 的**个数**必须一致 —— 少一个 `format` 就会抛."""
        for key, val in i18n.CATALOG.items():
            with self.subTest(key=key[:40]):
                want = {m for m in re.findall(r"\{(\d+)\}", key)}
                got = {m for m in re.findall(r"\{(\d+)\}", val)}
                self.assertEqual(got, want, f"占位符对不上: {key[:40]!r} -> {val[:40]!r}")

    def test_table_is_sorted(self) -> None:
        """表按 key 排序 —— 只是为了让 diff 干净、人好查."""
        self.assertEqual(list(i18n.CATALOG), sorted(i18n.CATALOG))


class TestNoChineseInLogic(unittest.TestCase):
    def test_library_layer_never_uses_chinese_as_logic(self) -> None:
        """`src/**` (除 i18n 表本身) 里不许有中文参与比较/下标/键/查表."""
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "i18n.py":  # 这张表本身就是"中文 -> 英文"的键, 天然豁免
                continue
            for why, lineno, text in _chinese_used_as_logic(path):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno} [{why}] {text[:40]}")
        self.assertEqual(offenders, [], "中文被当逻辑用了:\n  " + "\n  ".join(offenders[:10]))

    def test_gui_entry_never_uses_chinese_as_logic(self) -> None:
        path = PROJECT_ROOT / "tools" / "bridge_gui.py"
        offenders = [f"{why}@{lineno}: {text[:40]}" for why, lineno, text in _chinese_used_as_logic(path)]
        self.assertEqual(offenders, [], f"图形入口里中文当逻辑用: {offenders}")

    def test_module_constants_are_not_translated_at_import(self) -> None:
        """`STEPS = (i18n.t("…"), …)` 这种写法会把语言冻结在 import 时刻.

        踩过: `FAQ` 是 `AnnAssign`, 第一版扫描只认 `Assign`, 于是它被包了一层 ——
        界面语言切换后 FAQ 永远是导入时那种语言.
        """
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            for name, value in _module_level_names(path).items():
                for sub in ast.walk(value):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "t"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "i18n"
                    ):
                        offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{name}")
        self.assertEqual(offenders, [], f"模块级常量在 import 时就被翻译了: {offenders}")

    def test_only_the_gui_entry_sets_the_language(self) -> None:
        """只有图形入口能改语言 (命令行版必须永远是中文)."""
        callers: set[str] = set()
        for base in (SRC, PROJECT_ROOT / "tools"):
            for path in sorted(base.rglob("*.py")):
                if path.name == "i18n.py":
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "set_lang"
                    ):
                        callers.add(path.relative_to(PROJECT_ROOT).as_posix())
        self.assertEqual(
            callers,
            {"src/gui/app.py"},
            "只有图形入口可以切语言; 命令行版跟着切会让 tools/bridge.py 里"
            '"跨账号" in res.error 这类判断静默失效',
        )


# ---------------------------------------------------------------- 行为


class TestTranslation(_LangIsolated):
    def test_chinese_mode_returns_the_source_verbatim(self) -> None:
        i18n.set_lang("zh")
        for key in list(i18n.keys())[:50]:
            self.assertEqual(i18n.t(key), key)

    def test_english_mode_returns_the_translation(self) -> None:
        i18n.set_lang("en")
        for key in list(i18n.keys())[:50]:
            self.assertEqual(i18n.t(key), i18n.CATALOG[key])

    def test_unknown_key_falls_back_to_the_source(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(i18n.t("这条根本没翻过"), "这条根本没翻过")

    def test_english_needs_no_format_when_no_placeholder(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(i18n.t("检查环境").format(), "Check setup")

    def test_templates_still_format(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(i18n.t("{0} 个文件 · {1}").format(3, "OK"), "3 files · OK")

    def test_set_lang_normalizes_and_rejects(self) -> None:
        self.assertEqual(i18n.set_lang("EN"), "en")
        self.assertEqual(i18n.set_lang(" zh "), "zh")
        for bad in ("", "fr", "zh-CN", "english", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    i18n.set_lang(bad)  # type: ignore[arg-type]

    def test_lang_metadata(self) -> None:
        self.assertEqual(i18n.LANGS, ("zh", "en"))
        self.assertEqual(i18n.DEFAULT_LANG, "zh", "默认必须是中文: 命令行版与既有测试都基于它")
        self.assertEqual(set(i18n.LANG_LABEL), set(i18n.LANGS))
        self.assertTrue(i18n.is_supported("zh"))
        self.assertTrue(i18n.is_supported("EN"))
        self.assertFalse(i18n.is_supported("de"))
        self.assertFalse(i18n.is_supported(None))


class TestSettings(_LangIsolated):
    def test_path_follows_the_override(self) -> None:
        self.assertEqual(i18n.settings_path(), self.settings_file)

    def test_default_path_is_under_the_user_profile(self) -> None:
        os.environ.pop("SCB_SETTINGS", None)
        path = i18n.settings_path()
        self.assertEqual(path.name, "settings.json")
        self.assertIn("SteamCloudBridge", str(path))

    def test_round_trip(self) -> None:
        self.assertIsNone(i18n.load_saved())
        self.assertTrue(i18n.save_lang("en"))
        self.assertEqual(i18n.load_saved(), "en")
        self.assertTrue(i18n.save_lang("zh"))
        self.assertEqual(i18n.load_saved(), "zh")

    def test_save_is_atomic_and_leaves_no_temp_file(self) -> None:
        i18n.save_lang("en")
        leftovers = [p.name for p in self.settings_file.parent.iterdir() if p.name != "settings.json"]
        self.assertEqual(leftovers, [], f"写设置留下了临时文件: {leftovers}")

    def test_save_merges_instead_of_clobbering(self) -> None:
        self.settings_file.write_text(json.dumps({"lang": "zh", "keep": 1}), encoding="utf-8")
        i18n.save_lang("en")
        data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(data, {"lang": "en", "keep": 1})

    def test_broken_or_unsupported_values_read_as_none(self) -> None:
        for raw in ("{ not json", '["en"]', '{"lang": "de"}', '{"lang": null}', "null"):
            with self.subTest(raw=raw):
                self.settings_file.write_text(raw, encoding="utf-8")
                self.assertIsNone(i18n.load_saved())

    def test_save_rejects_unsupported_codes(self) -> None:
        self.assertFalse(i18n.save_lang("de"))
        self.assertFalse(self.settings_file.exists())

    def test_save_failure_is_not_fatal(self) -> None:
        """写不进去只返回 False —— 设置目录不可写不该让整个界面崩掉."""
        # 普通深目录其实会被 `mkdir(parents=True)` 建出来. 真正的"不可写"用一个
        # **文件**占住父目录的位置: 建目录那步就会失败.
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        os.environ["SCB_SETTINGS"] = str(blocker / "settings.json")
        self.assertFalse(i18n.save_lang("en"))


class TestDetect(_LangIsolated):
    def test_explicit_wins_over_everything(self) -> None:
        i18n.save_lang("zh")
        os.environ["SCB_LANG"] = "zh"
        self.assertEqual(i18n.detect("en"), "en")

    def test_env_beats_the_saved_setting(self) -> None:
        i18n.save_lang("en")
        os.environ["SCB_LANG"] = "zh"
        self.assertEqual(i18n.detect(), "zh")

    def test_saved_setting_is_used_when_env_is_absent(self) -> None:
        i18n.save_lang("en")
        self.assertEqual(i18n.detect(), "en")

    def test_falls_back_to_a_supported_language(self) -> None:
        self.assertIn(i18n.detect(), i18n.LANGS)

    def test_explicit_and_env_are_not_persisted(self) -> None:
        """`--lang en` 是"这一次这么跑", 不该改用户存下来的偏好."""
        i18n.save_lang("zh")
        i18n.detect("en")
        os.environ["SCB_LANG"] = "en"
        i18n.detect()
        self.assertEqual(i18n.load_saved(), "zh")

    def test_unknown_explicit_value_is_ignored(self) -> None:
        i18n.save_lang("zh")
        self.assertEqual(i18n.detect("fr"), "zh")

    def test_system_language_is_none_or_supported(self) -> None:
        got = i18n.system_lang()
        self.assertTrue(got is None or got in i18n.LANGS)


class TestLanguageDependentArtifacts(_LangIsolated):
    """极少数**跟着语言走**的落盘文件名 —— 只有"给人读、程序不按名找"的才行."""

    def test_pack_note_filename_follows_the_language(self) -> None:
        from src.transport import ds2

        i18n.set_lang("zh")
        self.assertEqual(ds2.note_name(), ds2.NOTE_NAME)
        i18n.set_lang("en")
        self.assertEqual(ds2.note_name(), ds2.NOTE_NAME_EN)
        self.assertNotEqual(ds2.NOTE_NAME, ds2.NOTE_NAME_EN)

    def test_real_contracts_never_follow_the_language(self) -> None:
        """清单名是**真契约**: 导入端按它找文件, 绝不能跟着语言变."""
        from src.transport import ds2

        for lang in i18n.LANGS:
            with self.subTest(lang=lang):
                i18n.set_lang(lang)
                self.assertEqual(ds2.MANIFEST_NAME, "savepack.json")
                self.assertEqual(ds2.PACK_SCHEMA, "scb-savepack/1")


# ---------------------------------------------------------------- 图形冒烟

#: 子进程里跑的探针. **为什么要另起进程**: 同一个进程里反复"建窗 -> 销毁 -> 再建窗",
#: tkinter 的 `Variable.__del__` 会在解释器收尾时撞上已消失的 Tk 解释器, 打出一堆
#: `Tcl_AsyncDelete: async handler deleted by the wrong thread`, 严重时直接把进程 abort
#: —— 测试套件会莫名其妙地整批挂掉. 交给子进程就没有这个问题, 顺带还验证了"换一台
#: 干净解释器能不能真的把英文界面建起来".
_GUI_PROBE = r'''
import json, os, re, sys, time
sys.path.insert(0, os.getcwd())
import tkinter as tk
from src import i18n
from src.gui.app import build_app

CJK = re.compile(r"[\u4e00-\u9fff]")
ALLOWED = {"先读我.txt", "正常", "注意", "需处理", "说明", "中文", "English"}
# 故意不翻译的片段: 语言选择器上必须写母语名 (见 tests/test_i18n.py 的说明).
UNT_ALWAYS = ("中文 / English", "English", "中文")


def leftover_cjk(text):
    for tok in UNT_ALWAYS:
        text = text.replace(tok, "")
    return bool(CJK.search(text))


i18n.set_lang("en")
app = build_app()
app.update_idletasks()


def pump(seconds=60.0):
    """跑事件循环直到没有后台任务在跑 (第 1 页会自动做一次环境体检)."""
    deadline = time.time() + seconds
    while app._busy and time.time() < deadline:
        app.update()
        time.sleep(0.02)
    app.update()
    return not app._busy


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def texts(w):
    out = []
    try:
        val = w.cget("text")
    except Exception:
        val = None
    if isinstance(val, str) and val.strip():
        out.append(val)
    if isinstance(w, tk.Text):
        try:
            body = w.get("1.0", "end-1c")
        except Exception:
            body = ""
        if body.strip():
            out.append(body)
    return out


seen, leftover, total = set(), [], 0
for index in range(len(app._pages)):
    app.go(index)
    app.update_idletasks()
    for widget in walk(app):
        for text in texts(widget):
            total += 1
            if text in seen or text.strip() in ALLOWED:
                continue
            seen.add(text)
            if leftover_cjk(text):
                leftover.append(text)

settled = pump()  # 等首页那次自动体检跑完, 否则切语言会被"正在忙"挡住

# 热切换: 英文 -> 中文 -> 英文. 页码与已检查的环境都不该丢.
pages = len(app._pages)
app.go(2)
pump(5.0)
env_before = app.env
app.set_language("zh")
app.update_idletasks()
to_zh = {"lang": i18n.current(), "pages": len(app._pages), "page": app._page,
         "env_kept": app.env is env_before, "saved": i18n.load_saved()}
# 非法值必须当场报错, 而不是把窗口改坏.
try:
    app.set_language("?")
    rejected = False
except ValueError:
    rejected = True
app.set_language("en")
app.update_idletasks()
to_en = {"lang": i18n.current(), "pages": len(app._pages), "page": app._page}

print(json.dumps({"total": total, "leftover": leftover, "to_zh": to_zh,
                  "to_en": to_en, "rejected": rejected, "settled": settled},
                 ensure_ascii=False))
sys.stdout.flush()
os._exit(0)  # 不让 tkinter 参与收尾: 这里已经拿到结论了
'''


@unittest.skipIf(tkinter is None, "本机解释器没有 tkinter (发行版自带)")
class TestGuiSmoke(_LangIsolated):
    """真把窗口建起来, 抓全部控件文字 —— 静态扫描覆盖不到的拼接文案由它兜底."""

    def _probe(self) -> dict:
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-c", _GUI_PROBE],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if proc.returncode != 0:
            if "TclError" in proc.stderr or "no display" in proc.stderr.lower():
                self.skipTest(f"没有可用的图形环境: {proc.stderr.strip()[:120]}")
            self.fail(f"图形探针失败 (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_every_page_renders_without_leftover_chinese(self) -> None:
        data = self._probe()
        self.assertGreater(data["total"], 100, "一个控件文字都没抓到, 探针可能失效了")
        self.assertEqual(data["leftover"], [], f"英文界面里有残留中文: {data['leftover'][:5]}")

    def test_switching_language_rebuilds_the_window_in_place(self) -> None:
        data = self._probe()
        zh, en = data["to_zh"], data["to_en"]
        self.assertEqual(zh["lang"], "zh")
        self.assertEqual(en["lang"], "en")
        for state, label in ((zh, "英文->中文"), (en, "中文->英文")):
            self.assertEqual(state["pages"], 5, f"{label}: 切语言后页数变了")
            self.assertEqual(state["page"], 2, f"{label}: 切语言后应停在原来那一页")
        self.assertTrue(zh["env_kept"], "切语言不该把已检查的环境丢掉")
        self.assertEqual(zh["saved"], "zh", "选过的语言要写进设置, 下次打开还记得")
        self.assertTrue(data["rejected"], "非法语言码应当场报错, 而不是悄悄退回默认")

    def test_entry_point_accepts_lang_and_rejects_unknown(self) -> None:
        """`tools/bridge_gui.py --lang` 是用户唯一能手动指定语言的入口."""
        import subprocess

        entry = PROJECT_ROOT / "tools" / "bridge_gui.py"
        ok = subprocess.run(
            [sys.executable, str(entry), "--lang", "en", "--smoke"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "SCB_SETTINGS": str(self.settings_file)},
            timeout=120,
        )
        self.assertEqual(ok.returncode, 0, f"英文冒烟失败: {ok.stdout}\n{ok.stderr}")

        bad = subprocess.run(
            [sys.executable, str(entry), "--lang", "fr", "--smoke"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        self.assertNotEqual(bad.returncode, 0, "不认识的语言应该被拦下来")


if __name__ == "__main__":
    unittest.main()
