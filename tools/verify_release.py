#!/usr/bin/env python3
"""把**发行 zip** 当成用户拿到的东西, 解压到干净目录并逐个真跑一遍.

为什么需要这一步: `build_release.py` 只证明"文件被放进了包里", 不证明
"用户解压后能用". 两者会在这些地方分叉 ——

* 清单里漏了一个文件 (包看着正常, 少一个 exe);
* `.bat` 在某次编辑里被写成纯 LF (部分 Windows 上 `goto` 行为异常);
* exe 是从旧位置拷来的, 或者被安全策略剥离过;
* 单文件版在"没有仓库在旁边"的环境里其实起不来.

`build_exe.py` 的校验跑的是 `build/pyi-dist/` 下的产物, 而不是包里的那一份;
这里跑的是**解压出来的那一份**, 才是用户真正会双击的东西.

用法:

    python tools/verify_release.py                     # 验最新版本的 zip
    python tools/verify_release.py --zip <path>        # 指定 zip
    python tools/verify_release.py --keep              # 保留解压目录 (排查用)

退出码: 0 全通过 / 1 有失败项.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RELEASE_DIR = PROJECT_ROOT / "release"

# 包内布局 (必须与 `build_release.dest_of` 一致): 顶层只留**启动器 + 向导本体**,
# 其余全部在 `support/`. 这里独立写一份而不是 import, 是为了让"布局漂了"
# 这件事在验收阶段就炸出来, 而不是两边一起漂.
SUPPORT_DIR = "support"
FRONT_FILES = ("RUN-GUI.bat", "SteamCloudBridgeGUI.exe")


def _latest_zip() -> Path:
    if not RELEASE_DIR.is_dir():
        raise SystemExit(f"没有 release 目录: {RELEASE_DIR}")
    cands = sorted(
        RELEASE_DIR.glob("SteamCloudBridge-v*.zip"), key=lambda p: p.stat().st_mtime
    )
    if not cands:
        raise SystemExit("release/ 下没有 SteamCloudBridge-v*.zip, 先跑 build_release.py")
    return cands[-1]


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_bats(pkg: Path, failures: list[str]) -> None:
    """每个 `.bat`: CRLF + 纯 ASCII + `goto` 目标齐全.

    这三条都是"静默出错"类型: 纯 LF 的 bat 里 `goto` 会跳到奇怪的位置;
    非 ASCII 在 GBK 控制台下会乱码甚至中断解析; 悬空 `goto` 直接静默退出 ——
    用户看到的是"双击了没反应".
    """
    for p in sorted(pkg.rglob("*.bat")):
        raw = p.read_bytes()
        crlf = raw.count(b"\n") == raw.count(b"\r\n") and raw.count(b"\r") == raw.count(
            b"\r\n"
        )
        ascii_ok = all(c < 128 for c in raw)
        text = raw.decode("ascii", errors="replace")
        labels = set(re.findall(r"^:([A-Za-z0-9_]+)", text, re.M))
        gotos = set(re.findall(r"goto\s+:?([A-Za-z0-9_]+)", text, re.I))
        dangling = {g for g in gotos if g.lower() != "eof" and g not in labels}
        ok = crlf and ascii_ok and not dangling
        detail = f"CRLF={crlf} ASCII={ascii_ok} 悬空goto={sorted(dangling) or '无'}"
        rel = p.relative_to(pkg).as_posix()
        print(f"[verify]   {rel:<40} {'OK ' if ok else '!! '}{detail}")
        if not ok:
            failures.append(f"{rel}: {detail}")


def _check_redaction(pkg: Path, failures: list[str]) -> None:
    """包内不许出现真实标识符 —— **必须解码级**, 明文扫一定看不到.

    单文件版把源码 base64 内嵌、exe 里是压缩 .pyc, 所以明文扫"0 命中"是假阴性.
    清单与解码逻辑在 `tools/redaction_guard.py` (与回归锁共用).
    """
    try:
        import redaction_guard as guard
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import redaction_guard as guard
        except ImportError:
            print("[verify] !! 找不到 redaction_guard, 跳过脱敏检查")
            failures.append("脱敏检查: 无法导入 redaction_guard")
            return
    hits = guard.scan_tree(pkg, everything=True)
    if hits:
        for h in hits:
            print(f"[verify] !! {h}")
        failures.append(f"脱敏: 包内含真实标识符 {len(hits)} 处")
    else:
        print("[verify]   包内无真实标识符 (含解码级检查)  OK")


def _run(cmd: list[str], failures: list[str], tag: str) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[verify] {tag} -> 无法启动: {exc}")
        failures.append(f"{tag}: {exc}")
        return
    out = (r.stdout or r.stderr or "").strip().replace("\n", " | ")[:160]
    print(f"[verify] {tag} -> rc={r.returncode} {out!r}")
    if r.returncode != 0:
        failures.append(f"{tag}: rc={r.returncode}")


def verify(zip_path: Path, keep: bool, python: str) -> int:
    print(f"[verify] zip: {zip_path}")
    print(f"[verify]      {zip_path.stat().st_size:,} B   sha256:{_sha256(zip_path)}")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        roots = {n.split("/")[0] for n in names}
        if len(roots) != 1:
            print(f"[verify] !! zip 里不止一个顶层目录: {roots} (解压会散一地)")
            return 1
        inner = roots.pop()
        if "/" in inner or inner.endswith(".zip"):
            print(f"[verify] !! 顶层目录名可疑: {inner!r}")
            return 1
        # 嵌套包事故: 展开目录里混进了上一版 zip 的解压副本
        nested = [n for n in names if n.lower().endswith(".zip")]
        if nested:
            print(f"[verify] !! 包里套着 zip: {nested} (大概率是展开目录的残留)")
            return 1
        print(f"[verify] 顶层目录: {inner}  文件数: {len(names)}")

    tmp = Path(tempfile.mkdtemp(prefix="scb-release-"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        pkg = tmp / inner
        failures: list[str] = []

        print("[verify] -- 包内布局 (用户解压后第一眼看到的) --")
        tops = sorted(p.name for p in pkg.iterdir())
        want_tops = sorted([*FRONT_FILES, SUPPORT_DIR])
        if tops == want_tops:
            print(f"[verify]   顶层 {tops}  OK")
        else:
            print(f"[verify] !! 顶层是 {tops}, 期望 {want_tops}")
            failures.append(f"顶层布局: {tops} != {want_tops}")

        print("[verify] -- 批处理纪律 --")
        _check_bats(pkg, failures)

        print("[verify] -- 脱敏 (明文 + 解码级) --")
        _check_redaction(pkg, failures)

        print("[verify] -- exe (用户双击的东西) --")
        for rel, probe in (
            (f"{SUPPORT_DIR}/SteamCloudBridgeProbe.exe", ["--version"]),
            (f"{SUPPORT_DIR}/SteamCloudBridgeBridge.exe", ["--version"]),
            # windowed 进程没有 stdout, `--version` 拿到空串等于没检查 ——
            # 用"把窗口真建起来再关掉", 这同时证明 tcl/tk 数据确实在包里.
            ("SteamCloudBridgeGUI.exe", ["--smoke"]),
        ):
            target = pkg / rel
            if not target.is_file():
                print(f"[verify] {rel} -> 不在包里")
                failures.append(f"{rel}: 缺失")
                continue
            _run([str(target), *probe], failures, f"{rel} {' '.join(probe)}")

        print("[verify] -- 单文件 .py (带 tkinter 的解释器) --")
        for name in (
            "SteamCloudBridgeProbe.py",
            "SteamCloudBridgeBridge.py",
            "SteamCloudBridgeGUI.py",
        ):
            rel = f"{SUPPORT_DIR}/{name}"
            target = pkg / rel
            if not target.is_file():
                print(f"[verify] {rel} -> 不在包里")
                failures.append(f"{rel}: 缺失")
                continue
            _run([python, str(target), "--version"], failures, f"{rel} --version")

        print()
        if failures:
            print(f"[verify] 失败 {len(failures)} 项:")
            for f in failures:
                print("           " + f)
            print(f"[verify] 解压目录保留在: {tmp}")
            return 1
        print("[verify] 全部通过 —— 这个 zip 解压后可以直接用.")
        return 0
    finally:
        if not keep:
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:  # pragma: no cover - 沙箱里可能被占用
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="解压发行包并逐个真跑一遍")
    ap.add_argument("--zip", default=None, help="指定 zip (默认取 release/ 下最新的)")
    ap.add_argument("--keep", action="store_true", help="保留解压目录 (排查用)")
    ap.add_argument(
        "--python",
        default=sys.executable,
        help="跑单文件版用的解释器 (必须带 tkinter; 默认当前解释器)",
    )
    args = ap.parse_args(argv)
    zip_path = Path(args.zip) if args.zip else _latest_zip()
    if not zip_path.is_file():
        raise SystemExit(f"zip 不存在: {zip_path}")
    print(f"[verify] 时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return verify(zip_path, args.keep, args.python)


if __name__ == "__main__":
    raise SystemExit(main())
