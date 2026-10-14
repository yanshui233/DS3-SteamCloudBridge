#!/usr/bin/env python3
"""把 `dist/SteamCloudBridge*.py` 打成**免装 Python 的单文件 exe**.

朋友电脑上常常没有 Python —— 这个脚本产出的 exe 让他双击即可运行,
不需要安装任何东西.

用法:

    python tools/build_exe.py                    # 两个目标都构建并校验
    python tools/build_exe.py --target bridge
    python tools/build_exe.py --check            # 只校验已有产物
    python tools/build_exe.py --python <py.exe>  # 指定带 PyInstaller 的解释器

产物:

    build/pyi-dist/dist-<时间戳>/SteamCloudBridgeProbe.exe    <- 交给 build_release.py
    build/pyi-dist/dist-<时间戳>/SteamCloudBridgeBridge.exe   <- 同上
    build/pyi-dist/dist-<时间戳>/SteamCloudBridgeGUI.exe      <- 图形向导 (无控制台)

设计取舍:

* `--onefile`  单文件, 双击即跑。代价是首次启动要解压到临时目录 (约 1-2 秒)。
* `--console`  给 CLI 工具保留输出窗口 —— 没有输出窗口用户会以为它没反应。
  `gui` 目标用 `--windowed`: 双击就是一个软件, 不该跟着弹一个黑框。
* 目标程序是**纯标准库** (向导用的是 tkinter), 所以不需要 `--hidden-import`;
  单文件里的 `src/*` 是用 `sys.modules` 重建的, 不经过真实 import, PyInstaller
  也扫不到, 因此无遗漏风险。tkinter 的 tcl/tk 数据由 PyInstaller 自动收集。
* `--noupx`    不做 UPX 压缩 —— 避免被杀软启发式误报 (工具读 Steam 用户目录、
  搬运器还会写存档, 被误报会让朋友直接不敢用)。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BUILD_DIR = PROJECT_ROOT / "build"
DIST_PATH = BUILD_DIR / "pyi-dist"
WORK_PATH = BUILD_DIR / "pyi-build"
SPEC_PATH = BUILD_DIR


class ExeTarget(NamedTuple):
    """一个 exe 目标的全部参数.

    用命名元组而不是裸元组: 加字段时不会让调用方的解包静默错位 ——
    这里踩过, 测试里 `for a, b, c in TARGETS.values()` 直接就 ValueError 了.
    """

    single_file: Path  # 单文件发行版 (dist/*.py)
    exe_name: str
    title: str
    console: bool  # True = 保留控制台 (CLI); False = windowed (图形界面)


# 目标名 -> 目标参数
TARGETS: dict[str, ExeTarget] = {
    "probe": ExeTarget(
        PROJECT_ROOT / "dist" / "SteamCloudBridgeProbe.py",
        "SteamCloudBridgeProbe.exe",
        "只读探测器",
        True,
    ),
    "bridge": ExeTarget(
        PROJECT_ROOT / "dist" / "SteamCloudBridgeBridge.py",
        "SteamCloudBridgeBridge.exe",
        "存档搬运器",
        True,
    ),
    "gui": ExeTarget(
        PROJECT_ROOT / "dist" / "SteamCloudBridgeGUI.py",
        "SteamCloudBridgeGUI.exe",
        "图形向导",
        False,  # windowed: 双击就是一个软件, 不该有个黑框跟着弹出来
    ),
}


def _new_run_dir(root: Path, prefix: str) -> Path:
    """每次构建都开一个全新的目录.

    PyInstaller **写 exe / 分析中间结果之前会先删除同名旧文件**, 而这一删除会撞上
    环境的批量删除保护 (`SAFE_DELETE_BULK_CONFIRM_REQUIRED`) —— 表现为 PyInstaller
    退出码 1, 而目录里还躺着上一次的旧产物, 于是"构建看着像失败、其实用了旧 exe".
    输出(以及 workpath)都落到全新目录里, 就没有旧文件可删, 构建不依赖任何删除权限.
    """
    run = root / datetime.datetime.now().strftime(f"{prefix}-%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    return run


def latest_exe(exe_name: str) -> Path | None:
    """在 `build/pyi-dist` 下找该名字最近一次构建的 exe (兼容放在根下的老布局)."""
    if not DIST_PATH.is_dir():
        return None
    cands = [p for p in DIST_PATH.rglob(exe_name) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def _candidate_pythons() -> list[Path]:
    """候选解释器, 按优先级排列.

    1. 环境变量 `SCB_BUILD_PYTHON` —— 显式指定, 优先
    2. 当前解释器 `sys.executable`
    3. PATH 上的 `python` / `python3` / `py`
    """
    out: list[Path] = []
    env = os.environ.get("SCB_BUILD_PYTHON")
    if env:
        out.append(Path(env))
    out.append(Path(sys.executable))
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    # 去重并保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_pyinstaller(python: Path) -> bool:
    try:
        r = subprocess.run(
            [str(python), "-c", "import PyInstaller; print(PyInstaller.__version__)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def find_python(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"指定的解释器不存在: {p}")
        if not _has_pyinstaller(p):
            raise SystemExit(f"{p} 里没有 PyInstaller, 先装: {p} -m pip install pyinstaller")
        return p
    for cand in _candidate_pythons():
        if cand.is_file() and _has_pyinstaller(cand):
            return cand
    raise SystemExit(
        "找不到带 PyInstaller 的 Python 解释器。\n"
        "装一个即可:\n"
        "  python -m pip install pyinstaller\n"
        "或用 --python <python.exe> / 环境变量 SCB_BUILD_PYTHON 显式指定。"
    )


def build(python: Path, target: str) -> Path:
    dist_py, exe_name, title, console = TARGETS[target]
    if not dist_py.is_file():
        raise SystemExit(f"缺少单文件发行版: {dist_py}\n先跑: python tools/build_single_file.py")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    dist = _new_run_dir(DIST_PATH, f"dist-{target}")
    work = _new_run_dir(WORK_PATH, f"work-{target}")
    exe = dist / exe_name

    cmd = [
        str(python),
        "-m",
        "PyInstaller",
        "--onefile",  # 单文件, 双击即用
        "--console" if console else "--windowed",  # CLI 留输出窗口; 向导不要黑框
        "--noupx",  # 不压缩, 降低杀软误报
        "--clean",
        "--noconfirm",
        "--name",
        exe.stem,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(SPEC_PATH),
        str(dist_py),
    ]
    print(f"[exe] ({title}) " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if r.returncode != 0:
        raise SystemExit(f"PyInstaller 退出码 {r.returncode} (目标 {target})")
    if not exe.is_file():
        raise SystemExit(f"PyInstaller 返回成功, 但没有产出 {exe}")
    return exe


def verify(exe: Path, target: str) -> None:
    """校验产物: 存在 / 新鲜 / 非空 / 是 PE / 能真跑起来 (不是只查文件)."""
    dist_py, exe_name, _title, console = TARGETS[target]
    if not exe.is_file():
        raise SystemExit(f"未产出 exe: {exe}")

    # 新鲜度检查: exe 必须比源单文件新.
    # 这条专门抓"PyInstaller 静默失败、目录里还躺着上一次的旧 exe" —— 那种情况下
    # 体积合理 / PE 头正常 / --version 也能跑, 全都通过, 只有时间戳能戳穿它.
    if dist_py.is_file() and exe.stat().st_mtime < dist_py.stat().st_mtime:
        raise SystemExit(
            f"{exe.name} 比 {dist_py.name} 旧 —— 打包很可能没有真正执行, "
            "仍在使用上一次的产物. 请检查 PyInstaller 退出码与上方日志."
        )

    size = exe.stat().st_size
    print(f"[exe] {exe.name}  {size:,} B  sha256:{_sha256(exe)[:16]}")
    if size < 1_000_000:
        raise SystemExit(f"exe 只有 {size:,} B, 明显偏小, 打包可能不完整")

    head = exe.read_bytes()[:2]
    if head != b"MZ":
        raise SystemExit(f"不是合法 PE 文件 (前两字节 {head!r})")
    print("[exe] PE 头 MZ ✓")

    # 探针按目标选: windowed 进程**没有 stdout**, `--version` 拿到的是空字符串,
    # 那个检查对它就等于没检查. 改用 "把窗口真建起来再关掉" —— 这同时是唯一能证明
    # tkinter 的 tcl/tk 数据确实被打进 exe 的办法.
    probe = ["--version"] if console else ["--smoke"]
    r = subprocess.run(
        [str(exe), *probe],
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = (r.stdout or r.stderr or "").strip()
    print(f"[exe] 实跑 {' '.join(probe)} -> rc={r.returncode}  {out!r}")
    if r.returncode != 0:
        raise SystemExit(f"exe 跑不起来 (rc={r.returncode})")
    if console and not out:
        raise SystemExit("exe 没有打印版本号 —— 产物可能不完整")
    print("[exe] 校验通过")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建免装 Python 的单文件 exe")
    ap.add_argument("--check", action="store_true", help="只校验最近一次的产物, 不重新构建")
    ap.add_argument("--python", default=None, help="指定带 PyInstaller 的 python.exe")
    ap.add_argument(
        "--target", choices=["all", *TARGETS], default="all", help="构建哪个目标 (默认 all)"
    )
    args = ap.parse_args(argv)

    names = list(TARGETS) if args.target == "all" else [args.target]

    if args.check:
        missing = []
        for name in names:
            found = latest_exe(TARGETS[name][1])
            if found is None:
                missing.append(TARGETS[name][1])
                continue
            verify(found, name)
        if missing:
            raise SystemExit("没有可校验的 exe: " + ", ".join(missing) + " (先跑一次构建)")
        return 0

    python = find_python(args.python)
    print(f"[exe] 使用解释器: {python}")
    version = subprocess.run(
        [str(python), "-c", "import PyInstaller;print(PyInstaller.__version__)"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"[exe] PyInstaller : {version}")
    built: list[Path] = []
    for name in names:
        exe = build(python, name)
        verify(exe, name)
        built.append(exe)
    print()
    for exe in built:
        print(f"[exe] 产物: {exe}")
    print("[exe] 下一步: python tools/build_release.py    (会把它们打进发行包)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
