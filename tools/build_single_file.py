#!/usr/bin/env python3
"""把内核 + 入口工具打包成**单个 .py**，让用户机不需要 clone 整个仓库.

产物:

    dist/SteamCloudBridgeProbe.py    只读机器探测器  (入口 tools/machine_probe.py)
    dist/SteamCloudBridgeBridge.py   存档搬运器      (入口 tools/bridge.py)
    dist/SteamCloudBridgeGUI.py      图形向导        (入口 tools/bridge_gui.py)

用户拿到这一个文件后:

    python SteamCloudBridgeProbe.py            # Windows 上双击 RUN-PROBE.bat 也行
    python SteamCloudBridgeBridge.py status --appid 374320
    python SteamCloudBridgeGUI.py              # 图形向导 (需要带 tkinter 的 Python)

**为什么不是简单拼接**: 内核各模块之间是靠模块名互调的
(`discovery.find_installations()` / `vdf.parse_file()` / `from .. import vdf`),
扁平成一份文件会让这些引用全部失效. 所以这里把每个模块源码 base64 内嵌,
在文件启动时用 `types.ModuleType` + `exec` **重建 `src.*` 的 sys.modules 表**,
让真实的 import 机制照常工作 —— 相对导入、包属性、`__all__` 全部保持原语义.

代价是文件约 250-500KB (base64 膨胀 33%), 换来的好处是**行为与仓库版完全一致**,
不需要维护两套逻辑.

内嵌模块的**顺序即安装顺序**: 构建期用 AST 扫出每个模块的真实 import 关系, 做一次
拓扑排序 (同批就绪者保持声明顺序), 再按该顺序写进文件. 手工维护顺序太脆弱 ——
`from .guard import Guard` 这类语句在 `guard` 尚未执行时会直接抛
`ModuleNotFoundError`; 而按名字排序同样会崩 (名字序会把 `steam_console_send` 排到
它依赖的 `win_dpi` 前面). 现在这两类问题都由 `_order_modules` 统一兜住, 循环依赖
会在构建期报错而不是留到运行时.

用法:
    python tools/build_single_file.py                    # 两个目标都构建
    python tools/build_single_file.py --target probe
    python tools/build_single_file.py --check            # 生成后在干净目录自检
"""

from __future__ import annotations

import argparse
import ast
import base64
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- 模块清单
# `src` 全量 (搬运器要用到全部; 探测器只用其中一部分, 见 TARGETS).
SRC_MODULES: list[tuple[str, str]] = [
    ("src", "src/__init__.py"),
    ("src.console", "src/console.py"),
    ("src.vdf", "src/vdf.py"),
    ("src.version", "src/version.py"),
    ("src.steam", "src/steam/__init__.py"),
    ("src.steam.accounts", "src/steam/accounts.py"),
    ("src.steam.cloud_log", "src/steam/cloud_log.py"),
    ("src.i18n", "src/i18n.py"),
    ("src.steam.discovery", "src/steam/discovery.py"),
    ("src.steam.launch", "src/steam/launch.py"),
    ("src.steam.localconfig", "src/steam/localconfig.py"),
    ("src.steam.processes", "src/steam/processes.py"),
    ("src.steam.remotecache", "src/steam/remotecache.py"),
    ("src.transport", "src/transport/__init__.py"),
    ("src.transport.autocloud", "src/transport/autocloud.py"),
    ("src.transport.cloudchannel", "src/transport/cloudchannel.py"),
    ("src.transport.crossmachine", "src/transport/crossmachine.py"),
    ("src.transport.ds2", "src/transport/ds2.py"),
    ("src.transport.guard", "src/transport/guard.py"),
    ("src.transport.plan", "src/transport/plan.py"),
    ("src.transport.snapshot", "src/transport/snapshot.py"),
    ("src.transport.target", "src/transport/target.py"),
    ("src.transport.trigger", "src/transport/trigger.py"),
    ("src.transport.verify", "src/transport/verify.py"),
    ("src.transport.wizard", "src/transport/wizard.py"),
]

# 探测器只需要这几个 (体积小一点); 顺序同上, 父包在前.
PROBE_SRC_MODULES: list[tuple[str, str]] = [
    ("src", "src/__init__.py"),
    ("src.console", "src/console.py"),
    ("src.vdf", "src/vdf.py"),
    ("src.version", "src/version.py"),
    ("src.steam", "src/steam/__init__.py"),
    ("src.steam.accounts", "src/steam/accounts.py"),
    ("src.steam.cloud_log", "src/steam/cloud_log.py"),
    ("src.steam.discovery", "src/steam/discovery.py"),
    ("src.steam.localconfig", "src/steam/localconfig.py"),
    ("src.steam.remotecache", "src/steam/remotecache.py"),
    # 只为 `console_ack` (核对 `testappcloudpaths` 的执行回执).
    # `cloudchannel` 本身零项目依赖, 带进来不牵出别的模块.
    ("src.transport", "src/transport/__init__.py"),
    ("src.transport.cloudchannel", "src/transport/cloudchannel.py"),
]

# 顶层工具模块. `win_dpi` 必须在 `steam_console_send` 之前:
# 后者 import 时就调用 ctypes 装 user32, 而 DPI 感知必须在那之前设好.
TOOLS_MODULES: list[tuple[str, str]] = [
    ("win_dpi", "tools/win_dpi.py"),
    ("steam_console_send", "tools/steam_console_send.py"),
]

# 图形向导 = 搬运器的全部内核 + GUI 层. 工具侧只要 `win_dpi`:
# 向导**不自动发键** (手敲是既定路径), 所以用不到 console 注入那套.
GUI_TOOLS_MODULES: list[tuple[str, str]] = [
    ("win_dpi", "tools/win_dpi.py"),
]

GUI_SRC_MODULES: list[tuple[str, str]] = SRC_MODULES + [
    ("src.gui", "src/gui/__init__.py"),
    ("src.gui.theme", "src/gui/theme.py"),
    ("src.gui.service", "src/gui/service.py"),
    ("src.gui.widgets", "src/gui/widgets.py"),
    ("src.gui.app", "src/gui/app.py"),
]

# entry 被内联到文件中部, 这些行在单文件里没有意义 / 会指到错误目录, 一律剥掉.
COMMON_STRIP = [r"^\s*from __future__ import annotations\s*$"]
BROKEN_PATH_STRIP = [
    # `Path(__file__).resolve().parent.parent` 在单文件里会指到"单文件所在盘的上一级"
    r"^PROJECT_ROOT = Path\(__file__\)\.resolve\(\)\.parent\.parent\s*$",
    r"^sys\.path\.insert\(0, str\(PROJECT_ROOT\)\)\s*$",
    r"^sys\.path\.insert\(0, str\(Path\(__file__\)\.resolve\(\)\.parent\).*$",
]

HEADER_TMPL = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SteamCloudBridge {title} (单文件发行版).

自动生成, 请勿手改 —— 源码在 src/ 与 {entry}, 重新生成:

    python tools/build_single_file.py --target {name}

纯标准库, 无需 pip install.

{usage}
"""

from __future__ import annotations

import base64 as _b64
import sys as _sys
import types as _types

# 内嵌模块源码 (base64): 模块名 -> 源码. **顺序即安装顺序** (父包/被依赖者在前).
_EMBEDDED = {{
'''

RUNTIME_TMPL = '''

def _install_embedded_modules() -> None:
    """把内嵌源码还原成真实的模块, 放进 sys.modules.

    这样 `from .. import vdf` / `discovery.find_installations()` 这类既有写法
    不需要任何改写就能工作 —— 单文件版与仓库版行为一致.

    **按 `_EMBEDDED` 的插入顺序安装**, 该顺序是构建期按真实 import 关系拓扑排序出来的
    (见 tools/build_single_file.py 的 `_order_modules`), 保证任何一条
    `from .x import y` 执行时 `y` 已经存在. 排序不能省: 名字序会把
    `steam_console_send` 排到 `win_dpi` 前面.
    """
    # 包必须带 `__path__`, 否则 `from .guard import ...` 会报
    # "'src.transport' is not a package"
    for _name in _PACKAGES:
        if _name in _sys.modules:
            continue
        _pkg = _types.ModuleType(_name)
        _pkg.__package__ = _name
        _pkg.__path__ = []
        _pkg.__file__ = f"<single-file:{{_name}}/__init__>"
        _sys.modules[_name] = _pkg

    for _name in list(_EMBEDDED):
        _mod = _sys.modules.get(_name)
        if _mod is None:
            _mod = _types.ModuleType(_name)
            _sys.modules[_name] = _mod
        _mod.__package__ = _name.rpartition(".")[0]
        if _name in _PACKAGES:
            _mod.__path__ = []
        _mod.__file__ = f"<single-file:{{_name}}>"
        _src = _b64.b64decode(_EMBEDDED[_name]).decode("utf-8")
        try:
            exec(compile(_src, _mod.__file__, "exec"), _mod.__dict__)
        except ModuleNotFoundError as _exc:
            # 图形向导会被内嵌模块一并安装, 其中界面层 import tkinter.
            # 精简过的 Python (没装 tcl/tk) 上会在这里炸 —— 与其甩一段
            # 堆栈, 不如直接说清怎么办. 其它缺模块照旧原样抛出.
            if (_exc.name or "").split(".")[0] == "tkinter":
                _sys.stderr.write(
                    "这个文件需要带图形界面组件 (tkinter) 的 Python, "
                    "但当前解释器没有.\\n"
                    "改用命令行版 SteamCloudBridgeBridge 功能完全一样; "
                    "或装回带 tcl/tk 的官方 Python.\\n"
                )
                raise SystemExit(2)
            raise

        _parent, _, _leaf = _name.rpartition(".")
        if _parent and _parent in _sys.modules:
            setattr(_sys.modules[_parent], _leaf, _mod)


_install_embedded_modules()


# ==========================================================================
# 以下为 {entry} 原样内联 (它负责 CLI 入口)
# ==========================================================================
'''

TARGETS: dict[str, dict] = {
    "probe": {
        "title": "单文件机器探测器",
        "modules": PROBE_SRC_MODULES,
        "entry": "tools/machine_probe.py",
        "out": "dist/SteamCloudBridgeProbe.py",
        "usage": (
            "用法:\n"
            "    python SteamCloudBridgeProbe.py                  # 自动选目标游戏\n"
            "    python SteamCloudBridgeProbe.py --appid 374320   # 指定游戏\n"
            "    python SteamCloudBridgeProbe.py --list           # 只列可探测 appid\n"
            "    python SteamCloudBridgeProbe.py --zip            # 额外产出回传 zip\n"
            "    python SteamCloudBridgeProbe.py --no-markers     # 跳过 autocloud 声明扫描 (更快)"
        ),
        "strip": COMMON_STRIP,
        # (参数, 期望产物文件名或 None); `{out}` 会被替换成临时目录
        # 期望的是**根目录**的脱敏档 —— 未脱敏完整档现在收在 `private/`,
        # 根目录只留可外发的东西 (见 machine_probe 的目录分层).
        "checks": [
            (["--list", "--no-markers"], None),
            (
                ["--appid", "374320", "--no-markers", "--out-dir", "{out}"],
                "machine_profile.share.json",
            ),
        ],
    },
    "bridge": {
        "title": "单文件存档搬运器",
        "modules": TOOLS_MODULES + SRC_MODULES,
        "entry": "tools/bridge.py",
        "out": "dist/SteamCloudBridgeBridge.py",
        "usage": (
            "用法 (与仓库版 tools/bridge.py 完全一致):\n"
            "    python SteamCloudBridgeBridge.py status --appid 374320\n"
            "    python SteamCloudBridgeBridge.py plan   --appid 374320 --source D:/DS30000.sl2\n"
            "    python SteamCloudBridgeBridge.py apply  --appid 374320 --source D:/DS30000.sl2 --yes\n"
            "    python SteamCloudBridgeBridge.py --help"
        ),
        "strip": BROKEN_PATH_STRIP + COMMON_STRIP,
        "checks": [
            (["--help"], None),
            (["status", "--appid", "374320"], None),
        ],
    },
    "gui": {
        "title": "单文件图形向导",
        "modules": GUI_TOOLS_MODULES + GUI_SRC_MODULES,
        "entry": "tools/bridge_gui.py",
        "out": "dist/SteamCloudBridgeGUI.py",
        "usage": (
            "用法 (双击即用; 需要 Python 带 tkinter):\n"
            "    python SteamCloudBridgeGUI.py              # 打开向导\n"
            "    python SteamCloudBridgeGUI.py --smoke      # 只建界面然后退出 (自检)\n"
            "    python SteamCloudBridgeGUI.py --version"
        ),
        "strip": BROKEN_PATH_STRIP + COMMON_STRIP,
        "checks": [
            (["--version"], None),
            # 真把窗口建起来再关掉 —— 这是唯一能证明"tkinter 相关资源确实打进单文件了"
            # 的检查 (只 import 不建窗口的话, 缺 tcl/tk 数据也能过).
            (["--smoke"], None),
        ],
    },
}


def _b64_source(path: Path) -> str:
    return base64.b64encode(path.read_text(encoding="utf-8").encode("utf-8")).decode("ascii")


def _module_deps(name: str, src: str, known: set[str]) -> set[str]:
    """该模块**在本组内**依赖的其它模块 (用于拓扑排序).

    只关心能被 `_install_embedded_modules` 满足的那些 —— 标准库 / 第三方一律忽略.
    解析用的是 AST, 不是正则: 注释与字符串里的 `import` 不该被当成依赖.
    """
    deps: set[str] = set()
    # 父包必须先于子包存在 (`from .. import x` 要求 `src` 已在 sys.modules)
    parent = name.rpartition(".")[0]
    if parent in known:
        deps.add(parent)

    pkg = name.rpartition(".")[0]
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known:
                    deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                if node.module:
                    cand = f"{base}.{node.module}" if base else node.module
                    if cand in known:
                        deps.add(cand)
                else:  # `from . import guard` —— 依赖的是各个被导入的子模块
                    for alias in node.names:
                        cand = f"{base}.{alias.name}" if base else alias.name
                        if cand in known:
                            deps.add(cand)
            elif node.module and node.module in known:
                deps.add(node.module)
    return deps


def _order_modules(modules: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按真实 import 关系做拓扑排序 (同批就绪者保持声明顺序).

    没有这一步, `from .guard import Guard` 会在 `guard` 尚未执行时抛
    `ModuleNotFoundError` / `ImportError`. 循环依赖直接报错 —— 那种结构本来就
    不可能靠"内嵌 + 按序 exec"还原.
    """
    names = [n for n, _ in modules]
    by_name = {n: rel for n, rel in modules}
    known = set(names)
    deps = {
        n: _module_deps(n, (PROJECT_ROOT / by_name[n]).read_text(encoding="utf-8"), known)
        for n in names
    }

    order: list[str] = []
    placed: set[str] = set()
    while len(order) < len(names):
        progressed = False
        for n in names:
            if n in placed:
                continue
            if deps[n] <= placed:
                order.append(n)
                placed.add(n)
                progressed = True
        if not progressed:
            stuck = [n for n in names if n not in placed]
            raise SystemExit(
                "内嵌模块之间存在循环依赖, 无法单文件化: " + ", ".join(stuck)
            )
    return [(n, by_name[n]) for n in order]


def _sanitize_entry(text: str, patterns: list[str]) -> str:
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.MULTILINE)
    return text


def build(target_name: str) -> Path:
    spec = TARGETS[target_name]
    declared: list[tuple[str, str]] = spec["modules"]
    entry: str = spec["entry"]

    missing = [rel for _, rel in declared if not (PROJECT_ROOT / rel).is_file()]
    if not (PROJECT_ROOT / entry).is_file():
        missing.append(entry)
    if missing:
        raise SystemExit("缺少源文件: " + ", ".join(missing))

    modules = _order_modules(declared)
    packages = [n for n, rel in modules if rel.endswith("__init__.py")]

    parts: list[str] = [
        HEADER_TMPL.format(
            title=spec["title"], entry=entry, name=target_name, usage=spec["usage"]
        )
    ]
    for name, rel in modules:
        parts.append(f'    "{name}": "{_b64_source(PROJECT_ROOT / rel)}",\n')
    parts.append("}\n\n")
    parts.append("# 包 (需要 `__path__`, 否则相对导入报 not a package)\n")
    parts.append("_PACKAGES = {\n")
    for name in packages:
        parts.append(f'    "{name}",\n')
    parts.append("}\n")
    parts.append(RUNTIME_TMPL.format(entry=entry))

    entry_text = _sanitize_entry(
        (PROJECT_ROOT / entry).read_text(encoding="utf-8"), spec["strip"]
    )
    parts.append(entry_text)
    parts.append(
        "\n\n# " + "=" * 74 + "\n"
        f"# 入口由上面内联的 {Path(entry).stem} 段提供 (自带 `if __name__ == '__main__'`).\n"
        "# " + "=" * 74 + "\n"
    )

    target = PROJECT_ROOT / spec["out"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(parts), encoding="utf-8")
    return target


def self_check(target_name: str, target: Path, *, python: str | None = None) -> int:
    """在临时目录里只带这一个文件跑, 确认无仓库依赖.

    `python` 指定用哪个解释器跑自检 (默认当前解释器). 图形目标**必须**给一个带
    tkinter 的解释器 —— 否则会得到一个误导性的"自检失败" (见下面的真因诊断).
    """
    spec = TARGETS[target_name]
    interp = python or sys.executable
    for extra, expect in spec["checks"]:
        cmd_desc = " ".join(extra)
        print(f"\n自检: [{target_name}] {cmd_desc} (干净临时目录) ...")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            isolated = Path(td) / target.name
            shutil.copy2(target, isolated)
            args = [td if a == "{out}" else a for a in extra]
            proc = subprocess.run(  # noqa: S603
                [interp, str(isolated), *args],
                cwd=td,
                capture_output=True,
                text=True,
                timeout=300,
            )
            head = (proc.stdout or "").strip().splitlines()[:12]
            for line in head:
                print("  " + line)
            if proc.stderr.strip():
                tail = proc.stderr.strip().splitlines()[-8:]
                print("  --- stderr ---")
                for line in tail:
                    print("  " + line)
            if proc.returncode != 0:
                print(f"  !! 自检失败 (exit={proc.returncode})")
                # 最常见的**假失败**: 拿一个没有 tkinter 的解释器去验图形版.
                # 产物本身没问题 (exe 自带 tkinter), 但这里会报成"失败", 很容易
                # 被读成"GUI 单文件坏了". 把真因写出来, 并给出可照抄的复验命令.
                if "tkinter" in proc.stderr:
                    print(
                        f"     真因: 当前解释器 {sys.executable} 没有 tkinter ——\n"
                        f"     这是**环境**问题, 不是产物问题 (exe 自带 tkinter, 故 exe 自检照过).\n"
                        f"     用带 tkinter 的解释器复验:\n"
                        f'         python tools/build_single_file.py --target {target_name} --check'
                        f' --python "<带 tkinter 的 python.exe>"'
                    )
                return 1
            if expect and not (Path(td) / expect).is_file():
                print(f"  !! 自检失败: 未产出 {expect}")
                return 1
            print("  OK: 单文件在干净目录下独立可用.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="生成单文件发行版")
    ap.add_argument(
        "--target",
        choices=["all", *TARGETS],
        default="all",
        help="构建哪个目标 (默认 all)",
    )
    ap.add_argument("--check", action="store_true", help="生成后在干净目录自检")
    ap.add_argument(
        "--python",
        default=None,
        help="跑自检用的解释器 (默认当前解释器; 验图形目标时必须带 tkinter)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    names = list(TARGETS) if args.target == "all" else [args.target]
    rc = 0
    for name in names:
        target = build(name)
        text = target.read_text(encoding="utf-8")
        modules = TARGETS[name]["modules"]
        print(f"\n已生成: {target}")
        print(f"  行数: {text.count(chr(10)) + 1}  大小: {target.stat().st_size:,} 字节")
        print(f"  内嵌模块: {len(modules)} 个 (构建期已按 import 依赖拓扑排序)")
        if args.check:
            rc |= self_check(name, target, python=args.python)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
