#!/usr/bin/env python3
"""脱敏回归锁: 真实标识符不允许出现在**会随包发出**的地方.

为什么需要它: 真实存档目录名 / accountid 曾长期躺在 `src/` 的 docstring 里
(而且明文扫 exe 扫不出来 —— 单文件版把源码 base64 了、exe 里是压缩 .pyc,
**0 命中是假阴性**), 于是随包发给了别人. 这个测试把"不许再写回来"钉死.

清单与解码逻辑都在 `tools/redaction_guard.py` (单一事实来源), 出包验收
`tools/verify_release.py` 用的是同一份.

运行:
    python -m unittest tests.test_redaction -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import redaction_guard as guard  # noqa: E402
from src.steam import accounts  # noqa: E402

GUARD_FILE = PROJECT_ROOT / "tools" / "redaction_guard.py"


class TestNoRealIdentifiersInShippedSources(unittest.TestCase):
    def test_shipped_sources_carry_no_real_identifier(self) -> None:
        hits = guard.scan_tree(PROJECT_ROOT)
        self.assertEqual(hits, [], "真实标识符又回到了会随包发出的源码里")

    def test_guard_lists_three_identifiers(self) -> None:
        """清单本身要有人盯着 —— 被清空/写坏的守卫会永远"通过"."""
        self.assertEqual(len(guard.REAL_IDENTIFIERS), 3)
        self.assertTrue(all(len(v) >= 10 for v in guard.REAL_IDENTIFIERS))

    def test_the_guard_file_itself_keeps_them_encoded(self) -> None:
        """守卫自己在 `tools/` 里 —— 它不许把清单写成明文, 否则自己就是泄露源."""
        own = GUARD_FILE.read_text(encoding="utf-8")
        for real in guard.REAL_IDENTIFIERS:
            self.assertNotIn(
                real,
                own,
                "守卫文件里出现了明文真实标识符 —— 请改回 base64 存放",
            )


class TestFakeTripleIsSelfConsistent(unittest.TestCase):
    """虚构值必须互相自洽, 否则后人"修脱敏"时会编出一组对不上的数字."""

    def test_accountid_to_hex_dir(self) -> None:
        self.assertEqual(
            accounts.accountid_to_hex_dir(guard.FAKE_ACCOUNTID), guard.FAKE_HEX_DIR
        )

    def test_hex_dir_to_accountid(self) -> None:
        self.assertEqual(
            accounts.hex_dir_to_accountid(guard.FAKE_HEX_DIR), guard.FAKE_ACCOUNTID
        )

    def test_sid64_matches_accountid(self) -> None:
        self.assertEqual(
            str(76561197960265728 + guard.FAKE_ACCOUNTID), guard.FAKE_SID64
        )


class TestDecodedSingleFileIsClean(unittest.TestCase):
    """解码级检查: 明文扫单文件版是假阴性 —— 源码是 base64 内嵌的.

    `dist/*.py` 不存在时跳过 (未构建的干净检出).
    """

    def test_every_embedded_module_is_clean(self) -> None:
        dist = PROJECT_ROOT / "dist"
        targets = sorted(dist.glob("SteamCloudBridge*.py")) if dist.is_dir() else []
        if not targets:
            self.skipTest("dist/ 为空 —— 未构建")
        for target in targets:
            blobs = guard.decoded_blobs(target.read_text(encoding="utf-8"))
            self.assertTrue(blobs, f"{target.name} 里没解出内嵌模块, 检查解析逻辑")
            for real in guard.REAL_IDENTIFIERS:
                for blob in blobs:
                    self.assertNotIn(
                        real, blob, f"{target.name} 的内嵌源码里含真实标识符 {real}"
                    )


class TestTheDetectorActuallyDetects(unittest.TestCase):
    """守卫必须是"真的能看见"的 —— 一个永远返回"干净"的守卫比没有更坏.

    这两个陷阱都实际踩过, 所以各钉一条:
    * 单文件版的源码是 **base64 内嵌**的 ⇒ 明文扫看不到;
    * exe 里的 `.pyc` 是 **zlib 压缩**的 ⇒ 明文扫同样看不到.
    """

    def test_decoder_finds_a_planted_identifier(self) -> None:
        import base64 as _b64

        planted = guard.REAL_IDENTIFIERS[0]
        src = (
            "import base64\n"
            "_EMBEDDED = {\n"
            f'    "src.steam.accounts": "{_b64.b64encode(planted.encode()).decode()}",\n'
            "}\n"
        )
        blobs = guard.decoded_blobs(src)
        self.assertTrue(blobs, "解码器没认出内嵌模块")
        self.assertIn(planted, blobs[0])

    def test_zlib_sweep_finds_a_planted_identifier(self) -> None:
        import tempfile
        import zlib

        planted = guard.REAL_IDENTIFIERS[0]
        payload = bytes(128) + zlib.compress((planted + " padding" * 20).encode()) + bytes(64)
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake.exe"
            fake.write_bytes(payload)
            hits = guard.scan_binary_file(fake)
        self.assertTrue(hits, "zlib 扫描没抓出压缩在 exe 里的真值")


if __name__ == "__main__":
    unittest.main()
