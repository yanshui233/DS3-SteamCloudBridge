#!/usr/bin/env python3
"""把项目打成**给非技术朋友测试**的发行包.

产物:

    release/SteamCloudBridge-v<版本>/          展开目录 (解压即用)
    release/SteamCloudBridge-v<版本>.zip       发给朋友的就是它

包里有什么 (全部面向"拿到就能跑"而非"clone 仓库"). v1.1.1 起**分层摆放** ——
顶层只留用户要双击的两样, 其余收进 `support/`, 免得一堆文件平铺时找不到入口:

    RUN-GUI.bat                   <== 平时就双击这个 (打开图形向导)
    SteamCloudBridgeGUI.exe       向导本体 (windowed, 无黑框)

    support/RUN-PROBE.bat         一键只读探测 (找 exe -> 单文件 -> Python)
    support/RUN-PROBE-TARGET.bat  专项探测 374320 / 1245620
    support/RUN-CLOUD-BAIT.bat    云接受性验证 (诱饵法)
    support/RUN-BRIDGE.bat        一键搬运向导 (唯一会写盘的那个; 需输入 YES)
    support/RUN-CROSS-VERIFY.bat  跨机往返 阶段2: 触发下行 + 比对字节 (不必启动游戏)
    support/RUN-SYNC.bat          命令行版日常入口 (点亮本通道 + 启动游戏)
    support/SteamCloudBridgeProbe.py     单文件只读探测器
    support/SteamCloudBridgeBridge.py    单文件搬运器 (含 xfer-push / xfer-verify)
    support/SteamCloudBridgeGUI.py       单文件图形向导 (需带 tkinter 的 Python)
    support/SteamCloudBridgeProbe.exe    可选: PyInstaller 产物 (免装 Python)
    support/SteamCloudBridgeBridge.exe   可选: 同上
    support/cloud_bait_check.py          诱饵法的实现 (需要 Python 3)
    support/README.md            **唯一一份说明** (原先 8 篇 01~08 合并于此)
    support/LICENSE
    support/VERSION.txt

    ⚠ 除 `RUN-GUI.bat` 外, 所有 `.bat` 都假设"自己旁边的那一套东西"在一起
      (它们一律用 `%~dp0` 找同目录的 exe/README), 所以把它们整体挪进
      `support/` 后**内部一行都不用改** —— 改的只有顶层那个 `RUN-GUI.bat`.

文档纪律: 面客说明**只保留一份 README.md**. 8 篇编号文档的后果是
朋友在包里看到 8 个 .md 不知道该开哪个 —— 合并后所有操作都在这一个文件里.

两条硬约束 (都是踩过坑才写下的):

* **不做批量删除**. 全部"覆盖写"而不是"先清空再写" —— 显式删除会触发环境的
  批量删除保护 (`SAFE_DELETE_BULK_CONFIRM_REQUIRED`), 让发布流程莫名中断.
* **`.bat` 统一 CRLF + 纯 ASCII**. 纯 LF 的 .bat 在部分 Windows 上 `goto` 行为异常;
  非 ASCII 输出与批处理解析器一起用不可靠 (中文一律放 `.md`).

用法:
    python tools/build_release.py
    python tools/build_release.py --exe build/pyi-dist/.../SteamCloudBridgeBridge.exe
    python tools/build_release.py --no-exe --no-zip
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.version import VERSION  # noqa: E402

RELEASE_VERSION = VERSION  # 版本号唯一来源: src/version.py
PKG_NAME = f"SteamCloudBridge-v{RELEASE_VERSION}"

DIST = PROJECT_ROOT / "dist"
PACKAGING = PROJECT_ROOT / "packaging"

# 单文件发行版: (包内文件名, 源文件). 三个工具都要带 ——
# 探测器回答"能不能", 搬运器负责"真的搬", 向导负责"让人会用".
SINGLE_FILES = [
    ("SteamCloudBridgeProbe.py", DIST / "SteamCloudBridgeProbe.py"),
    ("SteamCloudBridgeBridge.py", DIST / "SteamCloudBridgeBridge.py"),
    ("SteamCloudBridgeGUI.py", DIST / "SteamCloudBridgeGUI.py"),
]

# 8 篇编号文档 (01~08) 合并为**单一** README.md.
# 理由: 朋友拿到包后要自己判断"先开哪一份"本身就是成本; 合并后开哪个没有歧义.
DOC_FILES = [
    "README.md",
]
BAT_FILES = [
    # v1.1.0: **日常入口** —— 双击打开图形向导, 一步步引导. 包里所有其它 .bat
    # 都退成"命令行/查问题"用; 非技术用户只需要认识这一个.
    "RUN-GUI.bat",
    "RUN-PROBE.bat",
    "RUN-PROBE-TARGET.bat",  # 4b 专项: 只扫 DS3(374320) + 艾尔登法环(1245620)
    "RUN-CLOUD-BAIT.bat",  # 云接受性验证 (诱饵法)
    "RUN-BRIDGE.bat",  # v0.2.0: 搬运向导 (唯一会写盘的入口)
    # v0.2.7: 跨机往返 阶段 2/2 (在对方机器跑, 不启动游戏).
    # 注意: 阶段 1/2 的 RUN-CROSS-PREPARE.bat **故意不随包发** ——
    # 它只在"本机/发起侧"跑, 而且要求旁边放一份 .sl2 载荷; 放进包里只会误导对方.
    "RUN-CROSS-VERIFY.bat",
    # v1.0.0: 日常使用入口 (命令行版) —— 点亮本会话云通道 + 顺手启动游戏.
    # 双击 = `daily --appid 374320 --manual --manual-wait 240 --launch`.
    # 手敲优先: 不碰 GUI, 只把要敲的那一行打出来.
    "RUN-SYNC.bat",
]
# 独立工具: 需要 Python 3, 但它回答的是搬运器唯一的赌注假设.
PY_FILES = [
    (PROJECT_ROOT / "tools" / "cloud_bait_check.py", "cloud_bait_check.py"),
]

EXE_NAMES = [
    "SteamCloudBridgeProbe.exe",
    "SteamCloudBridgeBridge.exe",
    "SteamCloudBridgeGUI.exe",
]
EXE_DIST = PROJECT_ROOT / "build" / "pyi-dist"

# --- 包内布局 (自 v1.1.1 起) -------------------------------------------------
# 顶层**只留用户要双击的两样**: 启动器 + 向导本体; 其余全部进 support/.
# 理由: 17 个文件平铺时, 非技术用户分不清"先开哪个"; 外层只剩一个 `.bat` +
# 一个同名 `.exe` 之后, 这个判断不需要再做.
SUPPORT_DIR = "support"
FRONT_NAMES = ("RUN-GUI.bat", "SteamCloudBridgeGUI.exe")


def dest_of(name: str) -> str:
    """包内目标的**相对路径** (POSIX 斜杠): 向导相关留顶层, 其余进 `support/`.

    只在 `build_release` 内部用; 各文件的输入清单 (BAT_FILES 等) 保持**原来的
    纯文件名** —— 布局是"落点"问题, 不该渗进"发货清单"的语义里.
    """
    return name if name in FRONT_NAMES else f"{SUPPORT_DIR}/{name}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def select_packaged_files(
    out_dir: Path, expected: list[str]
) -> tuple[list[Path], list[Path]]:
    """从展开目录里挑出**清单内**的文件, 并回报清单外的残留.

    返回 `(打包列表, 残留列表)`. 残留只报告、不打包 —— 展开目录是"覆盖写"而不是
    "先清空再写", 历史残留 (典型: 有人把上一版 zip 解压到了这个目录里) 否则会被
    `rglob` 整份带进新包, 而且除了体积之外看不出异常。
    """
    listed = sorted(p for p in (out_dir / n for n in expected) if p.is_file())
    stray = sorted(p for p in out_dir.rglob("*") if p.is_file() and p not in listed)
    return listed, stray


def _write_crlf(src: Path, dst: Path) -> None:
    """批处理文件统一 CRLF —— 纯 LF 的 .bat 在部分 Windows 上会出现 goto 行为异常."""
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    dst.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


def latest_exe(exe_name: str) -> Path | None:
    """在 `build/pyi-dist` 下取该名字最近一次构建的 exe.

    `build_exe.py` 每次把产物落在独立的 `dist-<时间戳>` 目录里 (为了不依赖删除权限),
    所以这里按 mtime 取最新, 同时兼容直接放在 `pyi-dist/` 根下的老布局.
    """
    if not EXE_DIST.is_dir():
        return None
    cands = [p for p in EXE_DIST.rglob(exe_name) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def _resolve_exes(explicit: list[str]) -> dict[str, Path | None]:
    """每个 exe 名 -> 路径 (没找到就是 None)."""
    found: dict[str, Path | None] = {n: latest_exe(n) for n in EXE_NAMES}
    for raw in explicit:
        p = Path(raw)
        if not p.is_file():
            raise SystemExit(f"--exe 指定的文件不存在: {p}")
        if p.name not in found:
            raise SystemExit(
                f"--exe 的文件名不认识: {p.name}\n可接受: {', '.join(EXE_NAMES)}"
            )
        found[p.name] = p
    return found


def build(exes: dict[str, Path | None], do_zip: bool) -> Path:
    for dst_name, src in SINGLE_FILES:
        if not src.is_file():
            raise SystemExit(
                f"缺少单文件发行版: {src}\n先跑: python tools/build_single_file.py"
            )
    for name in DOC_FILES:
        if not (PACKAGING / name).is_file():
            raise SystemExit(f"缺少朋友向文档: {PACKAGING / name}")
    for bat in BAT_FILES:
        if not (PACKAGING / bat).is_file():
            raise SystemExit(f"缺少一键脚本: {PACKAGING / bat}")
    for src, _ in PY_FILES:
        if not src.is_file():
            raise SystemExit(f"缺少工具脚本: {src}")

    out_dir = PROJECT_ROOT / "release" / PKG_NAME
    # 全部采用"覆盖写"而不是"先清空再写": 显式删除会触发环境的批量删除保护
    # (SAFE_DELETE_BULK_CONFIRM_REQUIRED), 让发布流程莫名中断.
    out_dir.mkdir(parents=True, exist_ok=True)

    def place(src: Path, rel: str) -> Path:
        """落到包内相对路径处 (自动建 `support/`) —— 覆盖写, 从不先删."""
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst

    for dst_name, src in SINGLE_FILES:
        place(src, dest_of(dst_name))
    for bat in BAT_FILES:
        dst = out_dir / dest_of(bat)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _write_crlf(PACKAGING / bat, dst)
    for name in DOC_FILES:
        place(PACKAGING / name, dest_of(name))
    for src, dst_name in PY_FILES:
        place(src, dest_of(dst_name))
    place(PROJECT_ROOT / "LICENSE", dest_of("LICENSE"))

    with_exe = [n for n, p in exes.items() if p and p.is_file()]
    for name in with_exe:
        place(exes[name], dest_of(name))
    exe_note = (
        f"（已包含 {len(with_exe)}/{len(EXE_NAMES)}，免装 Python）"
        if with_exe
        else "（未包含：需要本机已安装 Python 3）"
    )

    version_txt = out_dir / dest_of("VERSION.txt")
    version_txt.parent.mkdir(parents=True, exist_ok=True)
    version_txt.write_text(
        "\n".join(
            [
                f"SteamCloudBridge {RELEASE_VERSION}",
                "",
                f"构建时间   : {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"Windows exe: {exe_note}",
                "",
                "这个文件夹里怎么摆的:",
                "",
                "  RUN-GUI.bat                   <== 平时就双击这个",
                "  SteamCloudBridgeGUI.exe           向导本体 (和启动器并排)",
                f"  {SUPPORT_DIR}\\                        其余全部文件都收在这里面",
                "",
                "工具说明 (都在 " + SUPPORT_DIR + "\\ 里):",
                "",
                "  图形向导 (RUN-GUI.bat / SteamCloudBridgeGUI.exe)   <== 平时就用这个",
                "      双击打开一个窗口, 左边五步, 跟着点就行, 不用记任何命令:",
                "        1 检查环境  2 备份存档  3 每次玩之前  4 遇到问题  5 魂2 存档",
                "      只有你点'备份'或'导入'时才会写盘。",
                "",
                "      第 5 步说明: 魂二(黑暗之魂2)本身没有 Steam 云存档 ——",
                "      不是设置没开, 是游戏没这个功能, 也没有开关可点。",
                "      所以它把存档导出成一个文件夹, 放进你的网盘同步目录或 U 盘,",
                "      换台机器再导入回来。同样只搬字节, 不读不改存档内容。",
                "",
                "  探测器 (RUN-PROBE.bat / SteamCloudBridgeProbe.py)",
                "      只读: 枚举本机 Steam 云存, 产出脱敏报告 (可安全公开)。",
                "      不读取/修改真实存档内容, 不改 Steam 文件, 默认不联网。",
                "",
                "  搬运器 (RUN-BRIDGE.bat / SteamCloudBridgeBridge.py)",
                "      M2/M3 搬运: 把一份你自己账号的 .sl2 放到正确位置, 可选推上云。",
                "      会写盘, 但: 需输入 YES 才写; 写入前强制备份 + 拍快照;",
                "                Steam 运行中直接拒绝; 跨账号直接拒绝;",
                "                写坏可用 rollback 子命令退回 (见 README.md)。",
                "",
                "      魂二用同一条命令行的另外三个子命令 (同样只搬字节):",
                "          save-status                       看魂二存档在哪 (只读)",
                "          save-out --to <文件夹>            导出成一个包",
                "          save-in  --from <文件夹> --yes    导回来 (写前强制备份)",
                "      不加 --yes 就是只检查不写盘。详见 README.md 第六节。",
                "",
                "  日常入口 (RUN-SYNC.bat) —— 不想开窗口时用这个",
                "      每次要玩之前双击一次: 点亮本会话云通道 + 帮你启动游戏。",
                f"      手敲那两行的抄写卡在 {SUPPORT_DIR}\\README.md 第三节。",
                "",
                "边界: 只搬同账号自己存档的字节; 不解析存档内容; 不改 Steam 安装目录。",
                "",
                f"全部说明: {SUPPORT_DIR}\\README.md (包里唯一一份文档)。",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # 只把**清单里**列出的文件算进包 —— 展开目录是"覆盖写"而非"先清空再写",
    # 所以历史残留会活下来。踩过: 把上一版 zip 解压进 out_dir 后, 旧的 rglob 把那份
    # 副本整个打进新包 (17.9 MB → 35.9 MB), 而且只在体积上看得出来。
    expected = [
        dest_of(n)
        for n in (
            [dst for dst, _ in SINGLE_FILES]
            + list(BAT_FILES)
            + list(DOC_FILES)
            + [dst for _src, dst in PY_FILES]
            + ["LICENSE", "VERSION.txt"]
            + list(with_exe)
        )
    ]
    listed, stray = select_packaged_files(out_dir, expected)
    if stray:
        print(f"[release] ⚠ 展开目录里有 {len(stray)} 个清单外的残留文件 (不会进包):")
        for p in stray:
            print(f"           {p.relative_to(out_dir).as_posix()}")
        print("[release]   确认没用就手动删掉; 想让它进包, 就加进对应清单.")

    print(f"[release] {PKG_NAME}")
    for p in listed:
        rel = p.relative_to(out_dir).as_posix()
        print(f"  {rel:<40} {p.stat().st_size:>9,} B   sha256:{_sha256(p)[:12]}")
    total = sum(p.stat().st_size for p in listed)
    front = [p for p in listed if p.parent == out_dir]
    print(f"[release] {len(listed)} 个文件, 展开后 {total:,} B")
    print(
        f"[release] 布局: 顶层 {len(front)} 个 ({', '.join(p.name for p in front)})"
        f" + {SUPPORT_DIR}/ {len(listed) - len(front)} 个"
    )

    if do_zip:
        zip_path = PROJECT_ROOT / "release" / f"{PKG_NAME}.zip"
        # 以 "w" 直接覆盖, 不预先 unlink (同上, 避开删除保护)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in listed:
                zf.write(p, f"{PKG_NAME}/{p.relative_to(out_dir).as_posix()}")
        print()
        print(f"[release] zip -> {zip_path}")
        print(f"[release]        {zip_path.stat().st_size:,} B   sha256:{_sha256(zip_path)}")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="组装给朋友测试的发行包")
    ap.add_argument(
        "--exe",
        action="append",
        default=[],
        help="可选: 显式指定某个 exe (可重复). 默认自动取 build/pyi-dist 下最新的两个",
    )
    ap.add_argument("--no-exe", action="store_true", help="不带任何 exe")
    ap.add_argument("--no-zip", action="store_true", help="只展开目录, 不打包 zip")
    args = ap.parse_args(argv)
    exes = {n: None for n in EXE_NAMES} if args.no_exe else _resolve_exes(args.exe)
    build(exes, do_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
