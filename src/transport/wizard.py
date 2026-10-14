"""朋友向的**交互式向导** —— 搬运器的"下一步该做什么"。

为什么单独一层而不是写进 `.bat`
================================
一键脚本里做不了"提问 → 判断 → 中止"这类流程: `cmd` 的 `call`/`goto` 与编码
都很容易踩坑, 而这个沙箱里 `cmd.exe` 被安全策略禁掉, 连"跑一遍看看"都做不到.
把流程写成纯 Python 之后, 三个外部动作 (`prompt` / `echo` / `invoke`) 全部靠参数
注入, 于是整条交互路径能在单测里跑完 —— 不需要真键盘、真 Steam、真存档.

`invoke(argv)` 由调用方提供 (生产环境就是 `bridge.main`), 所以本模块**不 import
bridge**, 不存在循环依赖; 也正因为每个动作都是一次完整的 CLI 调用,
向导的行为与手打命令**逐字一致**, 不会出现"向导里能过、命令行里过不了"的分叉.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

__all__ = ["PStep", "WizardResult", "run", "strip_quotes"]


@dataclass
class PStep:
    """向导走过的一步 (给测试断言用)."""

    name: str
    detail: str = ""


@dataclass
class WizardResult:
    rc: int
    steps: list[PStep] = field(default_factory=list)
    applied: bool = False

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.steps]


def strip_quotes(text: str) -> str:
    """去掉拖拽进 cmd 的路径外面的引号.

    Windows 上把文件拖进命令行会自动加双引号, 而且有时会留下尾部空格.
    这里还顺手处理"引号成对但里面又带空格"和"只加了一边"两种脏输入.
    """
    s = text.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    elif s.startswith('"'):
        s = s[1:]
    elif s.endswith('"'):
        s = s[:-1]
    return s.strip()


BANNER = """
================================================================
 SteamCloudBridge 搬运向导
================================================================
 它做三件事:
   1. 只读诊断   —— 这台机器上该游戏的存档落点在哪、云通道通不通
   2. 生成计划   —— 告诉你"将要写什么、写到哪、备不备份"(不写盘)
   3. 真正搬运   —— 需要你输入 YES 才会写, 写之前自动备份 + 拍快照

 三条硬规则:
   * 只搬**你自己这个 Steam 账号**的存档, 跨账号会被直接拒绝
   * Steam 必须**完全退出**(写入过程中 Steam 开着会与云端抢文件)
   * 随时可以退回去: 备份目录 + 快照文件就是后悔药
================================================================
"""


def run(
    *,
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
    invoke: Callable[[Sequence[str]], int],
    default_appid: str = "374320",
    default_backup_dir: str = "backup",
    default_upload: bool = True,
) -> WizardResult:
    """走完整个向导. 返回 `WizardResult` (含走的步骤, 便于测试与日志).

    `prompt(text) -> str`  读一行输入 (空串表示用户直接回车)
    `echo(text)`           输出一行
    `invoke(argv) -> int`  执行一次 CLI, 返回退出码
    """
    out = WizardResult(rc=0)

    def step(name: str, detail: str = "") -> None:
        out.steps.append(PStep(name, detail))

    echo(BANNER)

    # ------------------------------------------------------------ 1) 诊断
    raw = prompt(f"1) 游戏 app id  [{default_appid}]: ")
    appid = raw.strip() or default_appid
    step("status", f"appid={appid}")
    echo("")
    echo("--- 只读诊断 (不会改任何文件) ---")
    invoke(["status", "--appid", appid])
    echo("")

    # ------------------------------------------------------------ 2) 源文件
    raw = prompt("2) 要放进去的存档文件 (.sl2) 完整路径\n   (可以把文件直接拖进这个窗口): ")
    source = strip_quotes(raw)
    if not source:
        echo("")
        echo("没有给源文件 —— 到此为止, 什么都没有改动.")
        step("abort-no-source")
        return out
    step("source", source)

    path = Path(source)
    if not path.is_file():
        echo("")
        echo(f"找不到这个文件: {source}")
        echo("请确认路径没写错; 用拖拽的话不要手动改引号.")
        out.rc = 2
        step("abort-source-missing")
        return out

    # ------------------------------------------------------------ 3) 备份目录
    raw = prompt(f"3) 备份目录 (覆盖前的旧存档会放在这里)  [{default_backup_dir}]: ")
    backup_dir = strip_quotes(raw) or default_backup_dir
    step("backup-dir", backup_dir)
    echo("")

    # ------------------------------------------------------------ 4) 计划 (dry-run)
    echo("--- 生成搬运计划 (dry-run, 不写盘) ---")
    rc = invoke(
        [
            "plan",
            "--appid",
            appid,
            "--source",
            str(path),
            "--backup-dir",
            backup_dir,
        ]
    )
    step("plan", f"rc={rc}")
    echo("")
    if rc == 2:
        echo("计划被拦下了 —— 上面写了原因. 常见三种:")
        echo("  * Steam 还在运行      -> 完全退出 Steam 再来")
        echo("  * 这是别的账号的存档  -> 本项目只搬你自己账号的档, 不绕")
        echo("  * 没找到落点/文件不符 -> 确认该游戏在这台机器上有云存档痕迹")
        echo("这次没有写入任何东西.")
        out.rc = 2
        step("abort-plan-blocked")
        return out
    if rc != 0:
        echo(f"生成计划时出错 (退出码 {rc}), 上面是详细信息. 这次没有写入任何东西.")
        out.rc = rc
        step("abort-plan-error")
        return out

    # ------------------------------------------------------------ 5) 确认
    echo("计划跑通了, 没有写入任何东西.")
    echo("")
    raw = prompt("4) 确认真正写入? 这一步会改你的存档文件. 输入 YES 继续: ")
    if raw.strip().upper() != "YES":
        echo("")
        echo("已取消 —— 什么都没有改动.")
        step("abort-not-confirmed")
        return out
    step("confirmed")

    # ------------------------------------------------------------ 6) 是否上云
    default_hint = "Y/n" if default_upload else "y/N"
    raw = prompt(f"5) 写完以后顺带上传到 Steam 云? [{default_hint}]: ").strip().lower()
    if raw == "":
        upload = default_upload
    else:
        upload = raw in ("y", "yes")
    step("upload" if upload else "upload-skip")

    argv: list[str] = [
        "apply",
        "--appid",
        appid,
        "--source",
        str(path),
        "--backup-dir",
        backup_dir,
        "--snapshot-dir",
        backup_dir,
        "--yes",
    ]
    if upload:
        argv.append("--then-sync")
        echo("")
        echo("(会自己拉起 Steam 并把这次会话的云通道点亮, 之后触发上传并校验.)")
        echo("(Steam 启动期间请勿手动关闭它, 最长等 150 秒.)")

    # ------------------------------------------------------------ 7) 执行
    echo("")
    echo("--- 开始搬运 ---")
    rc = invoke(argv)
    out.rc = rc
    out.applied = rc == 0
    step("apply", f"rc={rc}")
    echo("")
    if rc == 0:
        echo("搬运完成. 后悔药: 上面打印的备份目录里放着旧档, 快照文件 (.snapshot.json)")
        echo("可以直接喂给 `rollback` 子命令退回原状.")
    else:
        echo(f"搬运没有成功 (退出码 {rc}). 上面是原因.")
        echo("如果已经写过盘但结果不对, 用上面的备份目录 / 快照文件回滚.")
        step("apply-failed")
    return out
