#!/usr/bin/env python3
"""交互向导 (`src/transport/wizard.py`) 的分支测试.

向导是朋友机上唯一会**写盘**的路径, 所以它每一处"该不该继续"的判断都要钉死:
没有源文件不许往下走、计划被拦不许往下走、没有输入 `YES` 不许写盘。
这些在真机上没法反复试 (会真写存档), 所以流程被写成纯函数 + 三个注入点
(`prompt` / `echo` / `invoke`), 整条交互在这里跑完.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.transport import wizard  # noqa: E402


class Harness:
    """假键盘 / 假屏幕 / 假执行器."""

    def __init__(self, answers: list[str], rcs: dict[str, int] | None = None,
                 default_rc: int = 0) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []
        self.calls: list[list[str]] = []
        self._rcs = dict(rcs or {})  # 子命令名 -> 退出码
        self._default_rc = default_rc

    def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if not self._answers:
            return ""
        return self._answers.pop(0)

    def echo(self, msg: str = "") -> None:
        self.lines.append(msg)

    def invoke(self, argv) -> int:
        argv = list(argv)
        self.calls.append(argv)
        if argv and argv[0] in self._rcs:
            return self._rcs[argv[0]]
        return self._default_rc

    # -- 便捷断言 -------------------------------------------------------
    def text(self) -> str:
        return "\n".join(self.lines)

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def apply_argv(self) -> list[str] | None:
        for c in self.calls:
            if c and c[0] == "apply":
                return c
        return None


def run_wizard(h: Harness, **kw):
    kw.setdefault("default_appid", "374320")
    kw.setdefault("default_backup_dir", "D:/bk")
    return wizard.run(prompt=h.prompt, echo=h.echo, invoke=h.invoke, **kw)


class TestStripQuotes(unittest.TestCase):
    def test_paired_quotes_from_drag_and_drop(self) -> None:
        self.assertEqual(wizard.strip_quotes('"C:\\a b\\DS30000.sl2"'), "C:\\a b\\DS30000.sl2")

    def test_one_sided_quote(self) -> None:
        self.assertEqual(wizard.strip_quotes('"C:\\x.sl2'), "C:\\x.sl2")
        self.assertEqual(wizard.strip_quotes('C:\\x.sl2"'), "C:\\x.sl2")

    def test_padding(self) -> None:
        self.assertEqual(wizard.strip_quotes('  "C:\\x.sl2"  '), "C:\\x.sl2")

    def test_empty(self) -> None:
        self.assertEqual(wizard.strip_quotes("   "), "")


class TestAbortPaths(unittest.TestCase):
    def test_no_source_aborts_before_plan(self) -> None:
        h = Harness(["374320", ""])
        out = run_wizard(h)
        self.assertEqual(out.rc, 0)
        self.assertIn("abort-no-source", out.names)
        self.assertEqual(h.call_names(), ["status"], "只有诊断这一步该发生")
        self.assertIn("什么都没有改动", h.text())

    def test_missing_source_file_aborts(self) -> None:
        h = Harness(["374320", "D:/definitely/not/here.sl2"])
        out = run_wizard(h)
        self.assertEqual(out.rc, 2)
        self.assertIn("abort-source-missing", out.names)
        self.assertEqual(h.call_names(), ["status"])
        self.assertIn("找不到这个文件", h.text())

    def test_plan_blocked_stops_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "incoming.sl2"
            src.write_bytes(b"x" * 32)
            h = Harness(["374320", str(src), ""], rcs={"plan": 2})
            out = run_wizard(h)
        self.assertEqual(out.rc, 2)
        self.assertIn("abort-plan-blocked", out.names)
        self.assertEqual(h.call_names(), ["status", "plan"], "被拦下就不该走到 apply")
        self.assertNotIn("apply", h.call_names())
        self.assertIn("Steam 还在运行", h.text())
        self.assertIn("没有写入任何东西", h.text())

    def test_plan_error_other_than_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "incoming.sl2"
            src.write_bytes(b"x" * 32)
            h = Harness(["374320", str(src), ""], rcs={"plan": 7})
            out = run_wizard(h)
        self.assertEqual(out.rc, 7)
        self.assertIn("abort-plan-error", out.names)
        self.assertNotIn("apply", h.call_names())

    def test_confirmation_requires_the_word_yes(self) -> None:
        # 大小写与首尾空格容错 (朋友可能打 "yes"), 但**必须**是 yes 这个词本身 ——
        # "y" / "好" / "ok" 一律算取消, 免得手滑就写盘.
        for wrong in ("y", "YES!", "", "确认", "ok", "1", "取消"):
            with self.subTest(answer=wrong), tempfile.TemporaryDirectory() as td:
                src = Path(td) / "incoming.sl2"
                src.write_bytes(b"x" * 32)
                h = Harness(["374320", str(src), "", wrong])
                out = run_wizard(h)
                self.assertEqual(out.rc, 0, wrong)
                self.assertIn("abort-not-confirmed", out.names, wrong)
                self.assertNotIn("apply", h.call_names(), wrong)
                self.assertFalse(out.applied, wrong)

    def test_yes_is_case_and_whitespace_tolerant(self) -> None:
        for good in ("YES", "yes", "Yes", "  yes  "):
            with self.subTest(answer=good), tempfile.TemporaryDirectory() as td:
                src = Path(td) / "incoming.sl2"
                src.write_bytes(b"x" * 32)
                h = Harness(["374320", str(src), "", good, "n"])
                out = run_wizard(h)
                self.assertIn("apply", h.call_names(), good)
                self.assertTrue(out.applied, good)

    def test_strips_quotes_off_the_dragged_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "incoming.sl2"
            src.write_bytes(b"x" * 32)
            h = Harness(["374320", f'"{src}"', "", "YES", "n"])
            out = run_wizard(h)
        self.assertEqual(out.rc, 0)
        argv = h.apply_argv()
        self.assertIsNotNone(argv)
        self.assertIn(str(src), argv)


class TestHappyPath(unittest.TestCase):
    def _run(self, upload_answer: str, plan_rc: int = 0, apply_rc: int = 0,
             default_upload: bool = True):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        src = Path(td.name) / "DS30000.sl2"
        src.write_bytes(b"S" * 64)
        h = Harness(
            ["", str(src), "", "YES", upload_answer],
            rcs={"plan": plan_rc, "apply": apply_rc},
        )
        out = run_wizard(h, default_upload=default_upload)
        return h, out, src

    def test_default_appid_used_when_blank(self) -> None:
        h, out, _ = self._run("")
        self.assertEqual(h.calls[0][:3], ["status", "--appid", "374320"])
        self.assertIn("status", out.names)

    def test_upload_defaults_to_yes(self) -> None:
        h, out, src = self._run("")
        argv = h.apply_argv()
        self.assertIn("--then-sync", argv)
        self.assertIn("--yes", argv)
        self.assertIn(str(src), argv)
        self.assertIn("upload", out.names)
        self.assertTrue(out.applied)
        self.assertEqual(out.rc, 0)

    def test_upload_can_be_skipped(self) -> None:
        h, out, _ = self._run("n")
        self.assertNotIn("--then-sync", h.apply_argv())
        self.assertIn("upload-skip", out.names)

    def test_upload_default_can_be_flipped(self) -> None:
        h, out, _ = self._run("", default_upload=False)
        self.assertNotIn("--then-sync", h.apply_argv())
        self.assertIn("upload-skip", out.names)

    def test_backup_dir_is_passed_to_both_plan_and_apply(self) -> None:
        h, _out, _ = self._run("n")
        for name in ("plan", "apply"):
            argv = next(c for c in h.calls if c[0] == name)
            self.assertIn("--backup-dir", argv)
            self.assertEqual(argv[argv.index("--backup-dir") + 1], "D:/bk")
        self.assertEqual(
            h.apply_argv()[h.apply_argv().index("--snapshot-dir") + 1], "D:/bk"
        )

    def test_custom_backup_dir_answer_wins(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        src = Path(td.name) / "DS30000.sl2"
        src.write_bytes(b"S" * 64)
        h = Harness(["", str(src), "E:/my-backup", "YES", "n"])
        run_wizard(h)
        argv = h.apply_argv()
        self.assertEqual(argv[argv.index("--backup-dir") + 1], "E:/my-backup")

    def test_apply_failure_is_reported_not_swallowed(self) -> None:
        h, out, _ = self._run("n", apply_rc=1)
        self.assertEqual(out.rc, 1)
        self.assertFalse(out.applied)
        self.assertIn("apply-failed", out.names)
        self.assertIn("搬运没有成功", h.text())

    def test_banner_mentions_the_three_hard_rules(self) -> None:
        h, _out, _ = self._run("n")
        text = h.text()
        self.assertIn("YES", text)
        self.assertIn("Steam", text)
        self.assertIn("备份", text)


if __name__ == "__main__":
    unittest.main()
