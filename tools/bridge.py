#!/usr/bin/env python3
"""SteamCloudBridge 搬运器 CLI (M2: dry-run 优先; M3: 云通道守卫 + 快照/回滚).

用法:

    # 只读诊断: 这台机器上该游戏的云存落点在哪、云通道什么状态
    python tools/bridge.py status --appid 374320

    # 搬运计划 (默认 dry-run, 不写任何文件)
    python tools/bridge.py plan --appid 374320 --source D:\\backup\\DS30000.sl2

    # 真跑 (必须显式 --yes; 会先备份目标文件)
    python tools/bridge.py apply --appid 374320 --source D:\\backup\\DS30000.sl2 \\
        --yes --backup-dir D:\\scb-backup

M3 新增 (云通道 + 同步 + 回滚 + trigger-sync):

    # 会话级诊断: 本 Steam 会话里 DS3 的云通道点亮了吗
    python tools/bridge.py channel --appid 374320

    # 点亮 (需要 Steam 在运行; 控制台走 ctypes 发送)
    python tools/bridge.py channel --appid 374320 --enable

    # 触发上行同步 + 校验 + 失败重试 (不必启动游戏)
    python tools/bridge.py sync --appid 374320

    # 自愈式同步: Steam 没在跑就自己拉起, 通道没点亮就自己点亮
    python tools/bridge.py sync --appid 374320 --start-steam --enable

    # 从快照回滚 (默认 dry-run)
    python tools/bridge.py rollback --snapshot D:\\scb\\374320__DS30000.sl2__123.json --yes

    # trigger-sync: 写完就推上云 (一条命令走完搬运 + 上传)
    python tools/bridge.py apply --appid 374320 --source D:\\backup\\DS30000.sl2 \\
        --backup-dir D:\\scb-backup --then-sync --yes

v0.2.0 新增 (朋友向):

    # 交互式向导: 诊断 -> 计划 -> 确认 -> 搬运 -> (可选) 上云
    python tools/bridge.py wizard

v1.0.0 新增 (日常使用):

    # 点亮本会话云通道 -> 顺手启动游戏 (游戏退出时 Steam 自己上传)
    python tools/bridge.py daily --appid 374320 --manual --launch

设计: 落点解析 / 守卫 / 计划 / 执行 / 云通道 / 快照 / 触发编排 / 交互向导都在
`src/transport/` 里 (`wizard.py` 就是向导流程), 本文件只负责串起来与打印.
`--steam-root` 与 `--account` 用于把视野限制在指定环境内 (沙箱测试 / 多账户机器).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ 自身 (steam_console_send)

from src.console import harden_console  # noqa: E402
from src.steam import accounts, cloud_log, discovery, launch, processes, remotecache  # noqa: E402
from src.transport import autocloud, cloudchannel, snapshot, trigger, verify  # noqa: E402
from src.transport import crossmachine  # noqa: E402
from src.transport import ds2  # noqa: E402  # 魂二: 没有 Steam 云, 走"指定目录"传输层
from src.transport import plan as plan_mod  # noqa: E402
from src.transport import target as target_mod  # noqa: E402
from src.transport import wizard as wizard_mod  # noqa: E402
from src.version import VERSION  # noqa: E402  # 版本号唯一来源 (src/version.py)


# ---------------------------------------------------------------- 环境准备


def pick_install(args: argparse.Namespace) -> discovery.SteamInstall:
    """决定用哪个 Steam 安装 (`--steam-root` 完全覆盖)."""
    if args.steam_root:
        inst = discovery.SteamInstall(root=Path(args.steam_root), source="--steam-root")
        if not inst.root.is_dir():
            raise SystemExit(f"--steam-root 指向的目录不存在: {inst.root}")
        try:
            inst.libraries = discovery.load_libraries(inst)
        except Exception:
            inst.libraries = []
        return inst

    installs = discovery.find_installations()
    if not installs:
        raise SystemExit("未找到任何 Steam 安装; 用 --steam-root 显式指定")
    return installs[0]


def pick_account(args: argparse.Namespace, install=None) -> int | None:
    if args.account:
        try:
            return int(args.account)
        except ValueError:
            raise SystemExit(f"--account 必须是数字: {args.account!r}")
    aid = accounts.get_active_accountid()
    if aid is not None:
        return aid
    # 注册表 ActiveUser 常为 0 (未记住登录 / 无登录态) —— 实测本机 Steam 正常运行
    # 该值仍是 0. 此时用本地线索兜底: `userdata/<id>/<appid>/remotecache.vdf` 唯一命中.
    # 不兜底的话会读不到 remotecache -> 云端副本/CN 全丢 + 跨账号红线静默失效.
    if install is not None:
        return accounts.find_accountid_for_app(install.root, str(args.appid))
    return None


def load_cloud(install: discovery.SteamInstall) -> cloud_log.CloudLog:
    p = install.logs / "cloud_log.txt"
    if p.is_file():
        return cloud_log.parse_cloud_log(p)
    return cloud_log.CloudLog()


def cloud_log_path(install: discovery.SteamInstall) -> Path:
    return install.logs / "cloud_log.txt"


def console_log_path(install: discovery.SteamInstall) -> Path:
    return install.logs / "console_log.txt"


def _detect_channel(install: discovery.SteamInstall, appid: str) -> cloudchannel.ChannelState:
    """同时读 `cloud_log` 与 `console_log` 判定会话云通道.

    只看 `cloud_log` 会把"刚 `testappcloudpaths` 点亮、还没跑过同步"误判成未点亮
    (点亮命令只写 console_log), 从而让 `sync` 错拒. 见 `cloudchannel.detect_channel_from_files`.

    另外带上 `steam.exe` 的启动时间做**兜底会话边界**: Steam 重启后 `cloud_log` 不一定
    立刻写新的 `Client version:` 行 (实测), 没有这个兜底就会把上一会话的"已点亮"证据
    当成本会话的 -> 假阳性.
    """
    return cloudchannel.detect_channel_from_files(
        appid,
        cloud_log_path(install),
        console_log_path(install),
        steam_start_epoch=processes.steam_start_epoch(),
    )


def make_sender(*, open_console: bool = False) -> cloudchannel.Sender:
    """造一个"把命令敲进 Steam 控制台"的发送器 (ctypes 通路).

    `Add-Type` / `WScript.Shell` 在本环境被安全策略拦, 只有 Python + ctypes 可用 ——
    复用 `tools/steam_console_send.py`.

    自动化测试钩子: 环境变量 `SCB_CONSOLE_FAKE=<文件路径>` 时**不碰 GUI**,
    把命令追加写进该文件并视为送达. 隔离端到端测试没法在沙箱里驱动真 Steam 的
    GUI 输入, 只能这样把"发送"这一步钉死. **它只替换发送动作, 不绕过任何守卫.**
    """

    def _send(command: str) -> bool:
        if os.environ.get("SCB_CONSOLE_FAKE_FAIL", "").strip() in ("1", "true", "yes"):
            # 测试钩子: 强制"自动键入失败", 用来测**手动接管**那条回退路径.
            # 它只让发送返回失败, 不绕过任何守卫 (工具照样拒绝回车).
            print("  (测试钩子) 模拟自动键入失败")
            return False
        fake = os.environ.get("SCB_CONSOLE_FAKE", "").strip()
        if fake:
            try:
                p = Path(fake)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(command + "\n")
                print(f"  (测试钩子) 已记录命令: {command}")
                return True
            except OSError as exc:
                print(f"  ! 测试钩子写入失败: {exc}")
                return False

        try:
            import steam_console_send as sc  # type: ignore
        except Exception as exc:  # pragma: no cover - 环境缺 GUI 时
            print(f"  ! 无法加载控制台发送器: {exc}")
            return False
        try:
            # `steam://open/console` 只需一次: 它把"控制台"标签激活并让焦点落进输入行.
            # 重试时不必再打开 (省 4s), 但**每次都必须点一下输入行**.
            if open_console and not _send._opened:  # type: ignore[attr-defined]
                os.startfile("steam://open/console")  # noqa: S606
                time.sleep(4.0)
                _send._opened = True  # type: ignore[attr-defined]
            hwnd = sc.find_steam_main()
            if not hwnd:
                print("  ! 没找到 Steam 主窗口 (Steam 没在运行?)")
                return False
            # **必须确认前台确实是 Steam** —— 否则盲发按键会打进别的应用
            if not sc.focus_verified(hwnd):
                fg = sc.foreground_hwnd()
                print(f"  ! 无法把 Steam 拉到前台 (当前前台 hwnd={fg}), 已中止发送")
                return False
            # **必须点一下控制台输入行, 并回读校验字符真的进去了** —— 只拉前台不够;
            # 而这套点击配方本身不是 100% 稳定, 所以由 send_command 闭环重试.
            if not sc.send_command(hwnd, command):
                print("  ! 键入未能进入控制台输入行 (已重试), 未按回车")
                try:  # 失败必须可诊断: 打出窗口尺寸/DPI/墨迹, 下次不用猜
                    m = sc.window_metrics(hwnd)
                    print(f"  诊断: Steam 窗口 {m['w']}x{m['h']} @{m['dpi']}dpi, "
                          f"输入行墨迹={m['ink']} (空框约 0)")
                except Exception:
                    pass
                return False
            return True
        except Exception as exc:
            print(f"  ! 发送失败: {exc}")
            return False

    _send._opened = False  # type: ignore[attr-defined]
    return _send


def remote_probe(install: discovery.SteamInstall, accountid: int | None, appid: str):
    """返回一个闭包: 读 `remotecache.vdf` 拿某文件的最新云端登记."""
    def _probe(rel_path: str) -> tuple[str, int]:
        if accountid is None:
            return "", 0
        rc = install.userdata / str(accountid) / str(appid) / "remotecache.vdf"
        if not rc.is_file():
            return "", 0
        try:
            cache = remotecache.parse_remotecache(rc, appid=str(appid))
        except Exception:
            return "", 0
        want = verify.norm_rel(rel_path)
        for e in cache.entries:
            if verify.norm_rel(e.name) == want:
                return e.sha or "", int(e.remotetime or 0)
        return "", 0

    return _probe


def _resolve(args: argparse.Namespace):
    """返回 (install, accountid, cloud, manifests, resolution)."""
    install = pick_install(args)
    accountid = pick_account(args, install)
    cloud = load_cloud(install)

    manifests: dict[str, discovery.AppManifest] = {}
    try:
        if not install.libraries:
            install.libraries = discovery.load_libraries(install)
        manifests = discovery.load_app_manifests(install.libraries)
    except Exception:
        manifests = {}

    manifest = manifests.get(str(args.appid))
    game_dir = manifest.install_dir if manifest else None

    resolution = target_mod.resolve_targets(
        install,
        str(args.appid),
        accountid,
        cloud=cloud,
        game_install_dir=game_dir,
    )
    return install, accountid, cloud, manifests, resolution


# ---------------------------------------------------------------- 渲染


def _fmt_ts(value: float | int | None) -> str:
    if not value:
        return "-"
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def render_status(
    install: discovery.SteamInstall,
    accountid: int | None,
    resolution,
    name: str,
) -> None:
    print(f"Steam 根目录      : {install.root}")
    print(f"账号 (accountid)  : {accountid if accountid is not None else '(未确定)'}")
    print(f"游戏              : {name} ({resolution.appid})")
    print(f"云通道(历史统计)  : {resolution.cloud_state}", end="")
    if resolution.toggle_effective:
        print("  (全文件记录: 曾观察到开关生效)")
    else:
        print()
    if resolution.cloud_disabled:
        print(f"  ⚠ {resolution.disabled_hint}")
    print("  ⓘ 以上是**全文件历史统计**, 不代表本会话已点亮; 本会话结论见下方 M3 段")
    print(f"云端 ChangeNumber : {resolution.change_number}")
    print()

    if not resolution.targets:
        print("落点解析: 失败")
        for n in resolution.notes:
            print(f"  - {n}")
        return

    print(f"落点解析: 命中 {len(resolution.targets)} 个候选")
    primary = resolution.primary
    for t in resolution.targets:
        mark = "★" if (primary is not None and t is primary) else " "
        print(f"  {mark} [{t.confidence}] {t.abs_path or '(无法展开)'}")
        print(
            f"      云端 sha={t.remote_sha or '-'} size={_fmt_size(t.remote_size)} "
            f"remotetime={_fmt_ts(t.remotetime)}"
        )
        print(
            f"      本地 {'存在' if t.exists else '不存在'} "
            f"size={_fmt_size(t.size) if t.exists else '-'} mtime={_fmt_ts(t.mtime) if t.exists else '-'}"
        )
        for e in t.evidence:
            print(f"      证据: {e}")
    if resolution.notes:
        print("备注:")
        for n in resolution.notes:
            print(f"  - {n}")


def render_plan(plan: plan_mod.TransferPlan) -> None:
    print(f"游戏          : {plan.name} ({plan.appid})")
    print(f"源            : {plan.source.path}")
    print(
        f"  sha1={plan.source.sha1 or '(不可读)'} size={_fmt_size(plan.source.size)} "
        f"mtime={_fmt_ts(plan.source.mtime)}"
    )
    if plan.target is not None:
        print(f"目标          : {plan.target.abs_path}")
        print(f"  落点置信度  : {plan.target.confidence}")
        print(
            f"  目标现状    : {'存在' if plan.target.exists else '不存在'} "
            f"sha={plan_mod.sha1_of(plan.target.abs_path) if plan.target.exists else '-'}"
        )
    print(f"判定          : {plan.verdict}")
    print()

    if plan.guards:
        blocks = plan.blockers
        warns = plan.warnings
        if blocks:
            print("阻断项 (必须解决):")
            for g in blocks:
                print(f"  ✗ [{g.code}] {g.message}")
                if g.remedy:
                    print(f"      处理: {g.remedy}")
        if warns:
            print("警告:")
            for g in warns:
                print(f"  ! [{g.code}] {g.message}")
                if g.remedy:
                    print(f"      建议: {g.remedy}")
        print()

    if plan.companions:
        print("目标目录伴生文件 (不走云, 不会被自动同步):")
        for c in plan.companions:
            flag = "云同步" if c.cloud_synced else "本地only"
            stale = "  ⚠ 比源档旧" if c.stale else ""
            print(f"  - {c.name}  [{flag}] {_fmt_size(c.size)} {_fmt_ts(c.mtime)}{stale}")
        print()

    if plan.steps:
        print("将执行的动作:")
        for i, s in enumerate(plan.steps, 1):
            touch = "写盘" if s.touches_disk else "只读"
            print(f"  {i}. ({touch}) {s.kind}: {s.detail}")
        print()

    if plan.notes:
        print("备注:")
        for n in plan.notes:
            print(f"  - {n}")
        print()


# ---------------------------------------------------------------- 命令


def render_channel(st: cloudchannel.ChannelState) -> None:
    print(f"游戏 (appid)      : {st.appid}")
    label = {True: "已点亮", False: "未点亮", None: "无法判断"}[st.lit]
    print(f"本会话云通道      : {label}")
    if st.session_marker_found:
        print(f"  会话起始        : {st.session_started_at} (之后 {st.session_line_count} 行)")
    else:
        print("  会话起始        : (日志里没有 `Client version:` 行, 退回全文件判定)")
    print(f"  启动评估已完成  : {'是' if st.eval_completed else '否'}")
    print(f"  本会话活动记录  : {len(st.active_lines)} 条")
    if st.console_ack_line:
        print(f"  控制台点亮回执  : 有 -> {st.console_ack_line}")
    print(f"  判定依据        : {st.reason}")
    if st.hint:
        print(f"  处理            : {st.hint}")


def _enable_channel(
    install: discovery.SteamInstall, appid: str, *, verbose: bool = True
) -> cloudchannel.ConsoleAck:
    """发一次 `testappcloudpaths <appid>` 点亮本会话云通道, 并等回执.

    只做"发送 + 等回执"一件事 —— 是否算点亮由调用方用 `_detect_channel` 重判
    (回执只证明**送达**, `cloud_log` 里的 Auto-Cloud 活动才证明**生效**).
    """
    send = make_sender(open_console=True)
    ok = send(cloudchannel.command_enable(appid))
    time.sleep(3.0)
    ack = cloudchannel.console_ack_from_file(
        "testappcloudpaths", appid, console_log_path(install)
    )
    if verbose:
        print(f"  送达           : {'是' if ok else '否'}")
        print(f"  控制台回执      : {'已接受' if ack.found else '未观察到'}"
              f"{' (本会话)' if ack.in_session else ''}")
        if ack.last_line:
            print(f"  回执行          : {ack.last_line}")
    return ack


def _manual_enable(
    install: discovery.SteamInstall,
    appid: str,
    *,
    wait_s: float,
    poll: float,
) -> cloudchannel.ConsoleAck | None:
    """**人工点亮**: 把要敲的那一行打出来, 然后只盯 `console_log` 等回执.

    为什么不自动发键: 往 Steam 控制台发键靠"点输入行"的坐标配方, 跟窗口尺寸/DPI
    绑得很死 —— 2026-10-13 实测在别人的机器上 4 次重试全部打空. 人就在键盘前时,
    让他在控制台敲一行花 10 秒, 比自动点击稳得多, 而且**不会误发到别的应用**.

    返回 `None` = 等待期内没等到回执 (调用方决定是报错还是重试).
    """
    cl_path = console_log_path(install)
    cl_watermark = len(crossmachine.read_tail_lines(cl_path))
    cmd = cloudchannel.command_enable(appid)
    print()
    print("=" * 68)
    print("  需要你手动敲一行 (打开这个游戏的云通道):")
    print()
    print(f"      {cmd}")
    print()
    print("  怎么开控制台: 按 Win+R -> 输入 steam://open/console -> 回车")
    print("               (或 Steam 左上角 `Steam` 菜单 -> `控制台`)")
    print("  ⚠ 控制台打开了 != 输入行有焦点: 先用鼠标点一下最底下那行输入框.")
    print("  敲对了**没有任何成功提示**, 这是正常的 —— 我只认控制台回执行.")
    print("  (下一行 `cloud_sync_down ...` 我稍后会再打给你.)")
    print()
    print(f"  我在这里等你, 最多 {int(wait_s)} 秒 ...")
    print("=" * 68)
    print()
    ack: cloudchannel.ConsoleAck | None = None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(max(1.0, poll))
        fresh = crossmachine.read_tail_lines(cl_path)[cl_watermark:]
        hit = cloudchannel.console_ack("testappcloudpaths", appid, fresh)
        if hit.found:
            ack = hit
            print(f"  ✓ 收到点亮回执: {hit.last_line}")
            break
    return ack


def cmd_channel(args: argparse.Namespace) -> int:
    install, _accountid, _cloud, _manifests, _resolution = _resolve(args)
    st = _detect_channel(install, args.appid)
    render_channel(st)
    print()

    if not args.enable:
        print("(只读诊断; 加 --enable 点亮本会话的云通道)")
        if args.json:
            _dump_json(args.json, {"channel": st.to_dict()})
        return 0

    # ---- 点亮 ----
    from src.transport.guard import steam_running

    running = steam_running()
    if running is False:
        print("✗ Steam 未运行 —— 控制台命令需要在 Steam 运行时发送.")
        print("  先启动 Steam 并登录, 再重试.")
        return 3
    if running is None:
        print("! 无法确认 Steam 是否在运行, 仍尝试发送.")

    print(f"发送控制台命令: {cloudchannel.command_enable(args.appid)}")
    ack = _enable_channel(install, args.appid)

    if ack.found:
        st2 = _detect_channel(install, args.appid)
        print(f"  重判            : {'已点亮' if st2.lit else ('未点亮' if st2.lit is False else '未知')}")
        print("  提示            : 点亮不会立刻产生日志 —— 跑一次 `sync` 或进出游戏即可验证")
    if args.json:
        _dump_json(args.json, {"channel": st.to_dict(), "ack": ack.to_dict()})
    return 0 if ack.found else 1


def cmd_daily(args: argparse.Namespace) -> int:
    """[v1.0] **日常使用**: 点亮本会话云通道 -> (可选) 启动游戏.

    这就是"装完一直生效"的**等价做法**: 双击一次 `RUN-SYNC.bat`, 本会话内通道是亮的,
    之后你正常玩, **游戏退出时 Steam 自己把存档同步上去**. 不用记任何命令.

    为什么不能真做到"装完一直生效": Steam 对 Auto-Cloud 是两层开关 ——
    ① 账号级 (`localconfig.vdf` 的 `apps/<appid>/cloud`) 是**持久**的;
    ② 游戏级那一层**没有本地持久化落点** (2026-10-13 查证: `appmanifest_<appid>.acf`
       里一个 cloud 字段都没有), 它是 Steam 客户端的**会话级**覆盖, 重启 Steam 即失效.
    所以"等效持久"只能靠"每次开游戏前点亮一次", 本命令把这一步压成一次双击.

    **顺序硬约束**: 点亮必须在**同一次 Steam 会话**内、且在**游戏启动之前**完成;
    中间夹一次 Steam 重启 = 白点亮. 因此本命令先点亮、后 `--launch`.
    """
    install = pick_install(args)
    appid = str(args.appid)
    out: dict[str, Any] = {"appid": appid, "launched": False, "channel": None, "ack": None}

    print("── 日常同步 (点亮本会话云通道) ──")
    print(f"  appid            : {appid}")
    print(f"  Steam 根         : {_redact(str(install.root))}")
    print()

    # ---- 游戏必须关着 ----
    game = _game_running()
    if game:
        print("✗ 游戏正在运行 —— 请先**完全退出游戏**, 再重跑本脚本.")
        print("  原因: 点亮必须在游戏启动**之前**完成; 游戏自己也在跑云同步, 会抢同一个文件.")
        return 3
    print(f"游戏未运行: {'是' if game is False else '无法确认 (继续)'}")

    # ---- Steam 必须在跑 (控制台命令需要有活着的 Steam) ----
    from src.transport.guard import steam_running

    running = steam_running()
    if running is False and args.start_steam:
        print("Steam 没在跑, 正在拉起 ...")
        launch.start_steam(install.root)
        launch.wait_for_process("steam.exe", timeout=float(args.steam_wait))
        time.sleep(12.0)
        running = steam_running()
    if running is False:
        print("✗ Steam 没在运行 —— 控制台命令需要 Steam 活着.")
        print("  先启动 Steam 并登录, 再重跑本脚本; 或加 --start-steam 让它自己拉起.")
        return 3
    if running is None:
        print("! 无法确认 Steam 是否在运行, 仍继续.")
    print()

    # ---- 点亮 (幂等: 已经亮着就跳过) ----
    st = _detect_channel(install, appid)
    render_channel(st)
    ack = None
    if st.lit is True:
        print("  本会话已经是亮的, 跳过点亮.")
    else:
        wait_s = float(getattr(args, "manual_wait", 0.0) or 0.0) or 180.0
        if getattr(args, "manual", False):
            ack = _manual_enable(install, appid, wait_s=wait_s, poll=float(args.poll))
        else:
            print(f"发送控制台命令: {cloudchannel.command_enable(appid)}")
            ack = _enable_channel(install, appid)
            if not ack.found:
                # 自动键入失败 ⇒ 转人工接管 (与 xfer-verify 那两步同一套约定)
                ack = _manual_enable(install, appid, wait_s=wait_s, poll=float(args.poll))
        if ack is None or not ack.found:
            print()
            print("✗ 等不到点亮回执 —— 那一行可能没敲进 Steam 控制台.")
            print(f"  可以自己再敲一次: {cloudchannel.command_enable(appid)}")
            print("  敲完重跑本脚本.")
            return 4
        st = _detect_channel(install, appid)
        label = "已点亮" if st.lit else "未观察到活动 (继续) —— 点亮不会立刻产生日志"
        print(f"  重判: {label}")
    print()

    # ---- 可选: 立刻启动游戏 ----
    if args.launch:
        print(f"启动游戏 (steam://rungameid/{appid}) ...")
        try:
            # 测试钩子 `SCB_LAUNCH_FAKE=<文件>`: 隔离测试里**不能真的去启动游戏**
            # (会牵动真 Steam / 真游戏), 只把 URI 记进文件. 它只替换"启动"这个动作,
            # 不绕过任何守卫 —— 与 `SCB_CONSOLE_FAKE` 同一套约定.
            _launch_fake = os.environ.get("SCB_LAUNCH_FAKE", "").strip()
            if _launch_fake:
                _lp = Path(_launch_fake)
                _lp.parent.mkdir(parents=True, exist_ok=True)
                with open(_lp, "a", encoding="utf-8", newline="\n") as _fh:
                    _fh.write(f"steam://rungameid/{appid}\n")
                print("  (测试钩子) 已记录启动 URI, 未真正启动游戏")
            else:
                os.startfile(f"steam://rungameid/{appid}")  # noqa: S606
            out["launched"] = True
            print("  已交给 Steam. 玩完**正常退出游戏**, Steam 会把存档同步到云端.")
        except Exception as exc:  # pragma: no cover - 依赖真实 shell
            print(f"  ! 启动失败: {exc}")
            print("  自己在 Steam 库里点开始游戏即可 —— 通道已经是亮的.")
    else:
        print("通道已亮. 现在去启动游戏即可.")
        print("(下次可以加 --launch 让本脚本替你启动.)")

    print()
    print("⚠ **退出游戏前不要再重启 Steam** —— 重启会把这次点亮的通道清掉; 真发生了就")
    print("  重新双击一次本脚本 (点亮是幂等的, 重复点没有副作用).")

    out["channel"] = st.to_dict()
    out["ack"] = ack.to_dict() if ack is not None else None
    if args.json:
        _dump_json(args.json, {"daily": out})
    return 0


def _target_rel(resolution) -> str:
    primary = resolution.primary
    if primary is None:
        return ""
    return verify.norm_rel(str(primary.rel_path))


def _sync_flow(
    *,
    install: discovery.SteamInstall,
    accountid: int | None,
    appid: str,
    resolution,
    allow_start_steam: bool,
    allow_enable: bool,
    require_lit: bool,
    max_attempts: int,
    settle: float,
    steam_wait: float = 150.0,
    steam_settle: float = 18.0,
    expected_sha1: str = "",
    header: bool = True,
) -> tuple[int, trigger.TriggerOutcome]:
    """`[M3] trigger-sync` 的统一实现: 启动 Steam(可选) -> 点亮(可选) -> 触发+校验+重试.

    `apply --then-sync` 与 `sync` 共用它, 保证两条入口的行为与退出码完全一致.
    返回 `(退出码, TriggerOutcome)`.
    """
    primary = resolution.primary
    rel = _target_rel(resolution)
    if header:
        print(f"游戏 (appid)      : {appid}")
        print(f"落点              : {primary.abs_path if primary else '(无落点)'}")
        print(f"云相对路径        : {rel}")
        if expected_sha1:
            print(f"期望本地 sha1     : {expected_sha1}")
        print()

    send = make_sender(open_console=True)
    probe = remote_probe(install, accountid, appid)
    log = cloud_log_path(install)

    def _check() -> verify.UploadCheck:
        sha, rt = probe(rel)
        return verify.check_upload_from_file(
            appid, rel, log, remote_sha=sha, remote_remotetime=rt
        )

    out = trigger.trigger_sync(
        appid,
        rel,
        target_path=(primary.abs_path if primary is not None else None),
        expected_sha1=expected_sha1,
        start_steam_fn=(
            (lambda: launch.start_steam(install.root)) if allow_start_steam else None
        ),
        detect_channel_fn=lambda: _detect_channel(install, appid),
        enable_fn=(
            (lambda: _enable_channel(install, appid, verbose=False).found)
            if allow_enable
            else None
        ),
        send=send,
        check=_check,
        allow_start_steam=allow_start_steam,
        require_lit=require_lit,
        max_attempts=max_attempts,
        settle_seconds=settle,
        steam_wait_seconds=steam_wait,
        steam_settle_seconds=steam_settle,
    )

    for s in out.steps:
        print(f"  - {s}")
    print()

    if out.succeeded:
        print("✓ 上传已确认成功")
        rc = 0
    else:
        print(f"✗ {out.error or '未达预期'}")
        rc = {
            trigger.ERROR_STEAM_NOT_STARTED: 3,
            trigger.ERROR_STEAM_UNEXPECTED: 3,
            trigger.ERROR_CHANNEL_UNLIT: 4,
            trigger.ERROR_CLOUD_OVERWROTE: 6,
            trigger.ERROR_SYNC_FAILED: 1,
        }.get(out.error_code, 1)
        if out.error_code == trigger.ERROR_CHANNEL_UNLIT and not allow_enable:
            print(f"  先执行: bridge.py channel --appid {appid} --enable")
            print("  (或给 sync 加 --enable / --force)")
        if out.error_code == trigger.ERROR_CLOUD_OVERWROTE:
            print("  处理: 用 `bridge.py rollback --snapshot <本次快照> --yes` 放回你的档,")
            print("        并确认目标机 Steam 已退出后再重试 (别让云端把本地盖回去).")
        if out.sync is not None and out.sync.final is not None and not out.succeeded:
            for e in out.sync.final.evidence[-4:]:
                print(f"    {e}")

    if out.error_code == trigger.ERROR_STEAM_UNEXPECTED and not allow_start_steam:
        # 把"没获准启动 Steam"翻译成人话 (退出码仍是 3)
        print("  (Steam 未在运行; 加 --start-steam 可让本次自动拉起)")

    return rc, out


def cmd_sync(args: argparse.Namespace) -> int:
    install, accountid, _cloud, _manifests, resolution = _resolve(args)
    primary = resolution.primary
    if primary is None or primary.abs_path is None:
        print("✗ 没有可用的落点, 无法同步.")
        return 2

    st = _detect_channel(install, args.appid)
    render_channel(st)
    print()

    rc, out = _sync_flow(
        install=install,
        accountid=accountid,
        appid=str(args.appid),
        resolution=resolution,
        allow_start_steam=bool(args.start_steam),
        allow_enable=bool(args.enable),
        require_lit=not bool(args.force),
        max_attempts=args.attempts,
        settle=float(args.settle),
        steam_wait=float(args.steam_wait),
    )
    if args.json:
        _dump_json(args.json, {"sync": out.to_dict()})
    return rc


def cmd_rollback(args: argparse.Namespace) -> int:
    try:
        snap = snapshot.load_snapshot(args.snapshot)
    except (OSError, ValueError) as exc:
        print(f"✗ 读快照失败: {exc}")
        return 2

    print(f"快照              : {args.snapshot}")
    print(f"游戏 (appid)      : {snap.appid}")
    print(f"目标              : {snap.target_path}")
    print(f"快照时间          : {_fmt_ts(snap.taken_at)}")
    print(f"快照时目标存在    : {'是' if snap.exists else '否'}")
    if snap.exists:
        print(f"  期望 sha1       : {snap.sha1}  size={_fmt_size(snap.size)}")
        print(f"  备份副本        : {snap.copy_path or '(无 —— 无法回滚内容)'}")
    print(f"  云端登记        : sha={snap.remote_sha or '-'} remotetime={snap.remotetime} CN={snap.change_number}")
    print()

    res = snapshot.restore_snapshot(
        snap,
        apply=bool(args.yes),
        journal_path=Path(args.journal) if args.journal else None,
    )
    print("执行结果:")
    for a in res.actions:
        print(f"  - {a}")
    if res.error:
        print(f"  ✗ {res.error}")
    if res.removed and args.yes:
        print()
        print("⚠ 已走删除路径 —— 紧接着请执行 `sync` 让云端也删掉,")
        print("  且**中间不要启动游戏**, 否则云端会把文件灌回来.")
    if args.json:
        _dump_json(args.json, {"rollback": res.to_dict()})
    # 干跑只要"没有错误"就算成功 (与 `plan` 的约定一致)
    if res.error:
        return 1
    return 0 if (res.restored or res.dry_run) else 1


def cmd_status(args: argparse.Namespace) -> int:
    install, accountid, _cloud, manifests, resolution = _resolve(args)
    manifest = manifests.get(str(args.appid))
    name = manifest.name if manifest else str(args.appid)
    render_status(install, accountid, resolution, name)

    # M3: 加一段**会话感知**的云通道结论 (全文件统计会被旧会话骗过)
    st = _detect_channel(install, args.appid)
    print()
    print("── 本会话云通道 (M3) ──")
    render_channel(st)

    if args.json:
        _dump_json(args.json, {"status": resolution.to_dict(), "channel": st.to_dict()})
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    install, accountid, cloud, manifests, resolution = _resolve(args)
    entry = cloud.entries.get(str(args.appid))
    patterns = entry.rule_patterns if entry else []
    manifest = manifests.get(str(args.appid))
    channel = _detect_channel(install, args.appid)

    plan = plan_mod.build_plan(
        source=Path(args.source),
        resolution=resolution,
        appid=str(args.appid),
        name=manifest.name if manifest else "",
        accountid=accountid,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        cloud_patterns=patterns,
        channel=channel,
    )
    render_plan(plan)
    if args.json:
        _dump_json(args.json, {"plan": plan.to_dict(), "channel": channel.to_dict()})
    return 2 if plan.blocked else 0


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        print("apply 会真正写入存档, 必须显式加 --yes 确认.")
        print("先跑 `plan` 看完整计划 (默认 dry-run, 不会写任何文件).")
        return 3

    if getattr(args, "then_sync", False):
        print("[trigger-sync] 已启用 —— 写完会: 启动 Steam -> 点亮本会话通道 -> 触发上传并校验")
        print("              前提: 此刻 Steam **没在跑**(apply 的守卫要求, 避免与云端仲裁抢文件)")
        if getattr(args, "no_start_steam", False):
            print("              --no-start-steam: Steam 没在跑就直接报错, 不自动拉起")
        print()

    install, accountid, cloud, manifests, resolution = _resolve(args)
    entry = cloud.entries.get(str(args.appid))
    patterns = entry.rule_patterns if entry else []
    manifest = manifests.get(str(args.appid))
    channel = _detect_channel(install, args.appid)
    if not args.backup_dir:
        print("警告: 未指定 --backup-dir, 覆盖前不会备份现有存档.")
        print("       建议加 `--backup-dir <目录>`.")

    plan = plan_mod.build_plan(
        source=Path(args.source),
        resolution=resolution,
        appid=str(args.appid),
        name=manifest.name if manifest else "",
        accountid=accountid,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        cloud_patterns=patterns,
        channel=channel,
    )
    render_plan(plan)

    # ---- M3: 写盘前拍快照 (回滚的唯一依据) ----
    snap_path: Path | None = None
    if not plan.blocked and resolution.primary is not None:
        copies = Path(args.snapshot_dir) if args.snapshot_dir else (
            Path(args.backup_dir) if args.backup_dir else None
        )
        snap_dir = copies or Path.cwd() / "scb-snapshots"
        snap = snapshot.take_snapshot(
            resolution.primary,
            appid=str(args.appid),
            change_number=resolution.change_number,
            channel_lit=channel.lit,
            copy_dir=copies,
            copy=True,
        )
        snap_path = snapshot.save_snapshot(
            snap,
            snap_dir / f"{args.appid}__{Path(plan.target.abs_path).name}__{snap.taken_at}.snapshot.json"
            if plan.target is not None and plan.target.abs_path
            else snap_dir / f"{args.appid}__{snap.taken_at}.snapshot.json",
        )
        print(f"快照已保存: {snap_path}")
        if snap.exists and not snap.has_copy:
            print("  ! 快照时目标存在但没有留副本 —— 回滚将无法恢复内容")

    result = autocloud.apply_plan(
        plan,
        apply=True,
        journal_path=Path(args.journal) if args.journal else None,
    )
    print("执行结果:")
    for a in result.actions:
        print(f"  - {a}")
    if result.error:
        print(f"  ✗ {result.error}")

    # ---- [M3] trigger-sync: 写完就把它推上云 (可选) ----
    sync_rc: int | None = None
    sync_out: trigger.TriggerOutcome | None = None
    if getattr(args, "then_sync", False) and not plan.blocked and not result.error:
        print()
        print("── trigger-sync (写完即触发云上行) ──")
        expected = result.target_sha1_after or plan.source.sha1
        sync_rc, sync_out = _sync_flow(
            install=install,
            accountid=accountid,
            appid=str(args.appid),
            resolution=resolution,
            allow_start_steam=not bool(getattr(args, "no_start_steam", False)),
            allow_enable=True,
            require_lit=True,
            max_attempts=int(getattr(args, "sync_attempts", 3)),
            settle=float(getattr(args, "sync_settle", 20.0)),
            steam_wait=float(getattr(args, "steam_wait", 150.0)),
            expected_sha1=expected,
            header=False,
        )

    if not plan.blocked and not result.error and not getattr(args, "then_sync", False):
        print()
        if channel.lit is not True:
            print("⚠ 本会话云通道未点亮 —— 快照已留, 但文件不会上云.")
            print(f"  下一步: bridge.py channel --appid {args.appid} --enable")
            print(f"           bridge.py sync    --appid {args.appid}")
        else:
            print(f"下一步: bridge.py sync --appid {args.appid}   (触发上传并校验)")
        print(f"或者一条命令走完: bridge.py apply ... --then-sync --yes")
    if args.json:
        _dump_json(
            args.json,
            {
                "plan": plan.to_dict(),
                "apply": result.to_dict(),
                "snapshot": str(snap_path) if snap_path else None,
                "trigger_sync": sync_out.to_dict() if sync_out is not None else None,
            },
        )
    if result.error or plan.blocked:
        return 1
    if sync_rc is not None:
        return sync_rc
    return 0 if (result.verified or not result.applied) else 1


# ---------------------------------------------------------------- 跨机往返 (M6)


def _apply_like_ns(args: argparse.Namespace) -> argparse.Namespace:
    """把 `xfer-push` 的参数装成 `apply --then-sync` 用的 Namespace.

    **刻意复用 `cmd_apply`** —— 跨机测试的"写入 + 上传"必须走与生产完全相同的路径
    (守卫 / 备份 / 快照 / 原子写 / 刷 mtime / 启动 Steam / 校验未被云端覆盖 / 点亮 / 上传校验),
    否则验证的就不是真正交付的那条链.
    """
    return argparse.Namespace(
        steam_root=getattr(args, "steam_root", ""),
        account=getattr(args, "account", ""),
        appid=str(args.appid),
        source=str(args.source),
        backup_dir=str(getattr(args, "backup_dir", "") or ""),
        snapshot_dir=str(getattr(args, "snapshot_dir", "") or ""),
        journal=str(getattr(args, "journal", "") or ""),
        then_sync=True,
        no_start_steam=bool(getattr(args, "no_start_steam", False)),
        sync_attempts=int(getattr(args, "sync_attempts", 3)),
        sync_settle=float(getattr(args, "sync_settle", 20.0)),
        steam_wait=float(getattr(args, "steam_wait", 150.0)),
        yes=True,
        json="",
    )


def _remote_entry_full(install, accountid, appid: str, rel: str) -> dict[str, Any]:
    """读 `remotecache.vdf` 里**这一条**的完整登记 (`cn` / `remotetime` / `sha` / `size`).

    判据只用 `remotetime > 0` (= 云端确实有副本), **不看** ChangeNumber ——
    本地 CN 会滞后/被重置, 读成"云端空"是踩过的坑.
    """
    miss: dict[str, Any] = {
        "change_number": 0, "remotetime": 0, "sha": "", "size": 0, "found": False,
    }
    if accountid is None:
        return miss
    rc = install.userdata / str(accountid) / str(appid) / "remotecache.vdf"
    if not rc.is_file():
        return miss
    try:
        cache = remotecache.parse_remotecache(rc, appid=str(appid))
    except Exception:
        return miss
    want = verify.norm_rel(rel)
    for e in cache.entries:
        if verify.norm_rel(e.name) == want:
            return {
                "change_number": int(cache.change_number),
                "remotetime": int(e.remotetime or 0),
                "sha": str(e.sha or ""),
                "size": int(e.size or 0),
                "found": True,
            }
    miss["change_number"] = int(cache.change_number)
    return miss


def _remote_entry(install, accountid, appid: str, rel: str):
    """读 `remotecache.vdf`: 返回 `(root ChangeNumber, remotetime, sha)`."""
    e = _remote_entry_full(install, accountid, appid, rel)
    return e["change_number"], e["remotetime"], e["sha"]


def _file_state(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"exists": False, "size": 0, "sha1": None, "mtime": 0}
    if not path.is_file():
        return {"exists": False, "size": 0, "sha1": None, "mtime": 0}
    return {
        "exists": True,
        "size": int(st.st_size),
        "sha1": crossmachine.sha1_of_file(path),
        "mtime": int(st.st_mtime),
    }


def cmd_xfer_push(args: argparse.Namespace) -> int:
    """阶段 1 (在本机跑): 写档 + 推上云 + 产出 `expected.json`.

    `--from-cloud` 走**零写入**分支 (见 `_xfer_declare_from_cloud`): 不写档、
    不上传、不碰 Steam, 直接按云端**当前已登记**的副本出契约.
    """
    if getattr(args, "from_cloud", False):
        return _xfer_declare_from_cloud(args)
    if not getattr(args, "source", ""):
        print("✗ 缺少 --source (或改用 `--from-cloud` 走零写入模式).")
        return 3
    if not args.yes:
        print("xfer-push 会真正写入存档并推上云, 必须显式加 --yes 确认.")
        print("先跑 `plan --appid <id> --source <文件>` 看计划 (dry-run).")
        return 3

    print("── 跨机往返 · 阶段 1/2 (push) ──")
    print(f"源文件            : {args.source}")
    print(f"标注 (label)      : {args.label or '(未命名)'}")
    print()

    rc = cmd_apply(_apply_like_ns(args))
    if rc != 0:
        print()
        print(f"✗ push 未成功 (rc={rc}) —— 没有产出 expected.json.")
        print("  云端此时可能已不是期望内容; 建议先跑 `xfer-push --source <本机真实档>` 复位.")
        return rc

    # ---- 复核云端登记, 再写契约 ----
    install, accountid, _cloud, _manifests, resolution = _resolve(args)
    primary = resolution.primary
    if primary is None or not primary.abs_path:
        print("✗ 推完之后反而解不出落点, 不写 expected.json.")
        return 1
    rel = verify.norm_rel(str(primary.rel_path))
    sha = crossmachine.sha1_of_file(args.source)
    cn, rt, rsha = _remote_entry(install, accountid, str(args.appid), rel)
    size = Path(args.source).stat().st_size

    acc = accounts.active_account(Path(install.root)) if install else None
    exp = crossmachine.Expected(
        appid=str(args.appid),
        rel_path=rel,
        sha1=sha,
        size=int(size),
        accountid=int(accountid or 0),
        account_hex_dir=accounts.accountid_to_hex_dir(int(accountid)) if accountid else "",
        account_name=acc.account_name if acc else "",
        persona_name=acc.persona_name if acc else "",
        cloud_change_number=cn,
        remote_remotetime=rt,
        remote_sha=rsha,
        tool_version=VERSION,
        label=str(args.label or ""),
        note=str(getattr(args, "note", "") or ""),
    )
    out = Path(args.out) if args.out else Path.cwd() / "xfer-out"
    path = exp.save(out / "expected.json")

    print()
    print("── 契约已生成 ──")
    print(f"  expected.json   : {path}")
    print(f"  云相对路径      : {rel}")
    print(f"  期望 sha1       : {sha}")
    print(f"  云端 ChangeNumber: {cn}    remotetime: {rt}")
    print(f"  云端登记 sha    : {rsha[:40] or '(读不到)'}")
    if rt <= 0:
        print("  ✗ 云端 remotedtime 为 0 —— 这次**没有真的上云**, 朋友机下不到东西.")
        print("    先查 `cloud_log.txt` 里有没有 `Upload OK for file`, 再重试.")
        return 1
    if rsha and rsha.lower() != sha.lower():
        print("  ! 云端登记 sha 与源不一致 —— 云端可能还没刷新, 或上传到了别的内容.")
        print("    建议重跑一次 `xfer-push` 再确认; 不一致时不要拿去测.")
    print()
    print("下一步:")
    print("  1) 把 expected.json 连同发行包一起发给朋友;")
    print("  2) 朋友在 **已登录这个账号** 的机器上双击 RUN-CROSS-VERIFY.bat;")
    print(f"  3) 测完用 `xfer-push --source <你的真实档> --label restore` 把云端复位.")
    return 0


def _xfer_declare_from_cloud(args: argparse.Namespace) -> int:
    """零写入出契约: 按云端**已登记**的副本写 `expected.json`.

    为什么需要这条路径: 完整往返 (写档 + 上传) 会让云端在这段时间里**不是你的真实档**,
    测完必须复位 —— 对"只想验跨机下行"来说是**多余的真实进度风险**.
    这里一个字节都不写、不启动/不重启 Steam, Steam 开着也能跑.
    代价: 只验下行半环 (上行半环已在本机 M3 真机验过).
    """
    print("── 跨机往返 · 阶段 1/2 (push · 零写入 --from-cloud) ──")
    print("  不写存档 / 不上传 / 不启动或重启 Steam —— 只读云端登记.")
    print()

    install, accountid, _cloud, _manifests, resolution = _resolve(args)
    primary = resolution.primary
    if primary is None or not primary.abs_path:
        print("✗ 解不出落点 (云证据不足) —— 先让云通道点亮起来再试.")
        return 1
    rel = verify.norm_rel(str(primary.rel_path))
    ent = _remote_entry_full(install, accountid, str(args.appid), rel)
    if int(ent["remotetime"]) <= 0 or not ent["sha"]:
        print(f"✗ 云端没有这个文件的副本 (remotetime={ent['remotetime']}) ——")
        print("  零写入模式只能声明**云端已有的东西**; 要么先真的推一次 (去掉 --from-cloud),")
        print("  要么查 `cloud_log.txt` 里这个 appid 到底有没有上过云.")
        return 1

    sha = str(ent["sha"]).lower()
    size = int(ent["size"] or 0)
    local = Path(str(primary.abs_path))
    if not size and local.is_file():
        size = int(local.stat().st_size)

    acc = accounts.active_account(Path(install.root)) if install else None
    exp = crossmachine.Expected(
        appid=str(args.appid),
        rel_path=rel,
        sha1=sha,
        size=size,
        accountid=int(accountid or 0),
        account_hex_dir=accounts.accountid_to_hex_dir(int(accountid)) if accountid else "",
        account_name=acc.account_name if acc else "",
        persona_name=acc.persona_name if acc else "",
        cloud_change_number=int(ent["change_number"]),
        remote_remotetime=int(ent["remotetime"]),
        remote_sha=sha,
        tool_version=VERSION,
        label=str(args.label or "from-cloud"),
        note=str(getattr(args, "note", "") or ""),
    )
    out = Path(args.out) if args.out else Path.cwd() / "xfer-out"
    path = exp.save(out / "expected.json")

    print("── 契约已生成 (零写入) ──")
    print(f"  expected.json     : {path}")
    print(f"  云相对路径        : {rel}")
    print(f"  期望 sha1         : {sha}")
    print(f"  云端 ChangeNumber : {ent['change_number']}    remotetime: {ent['remotetime']}")
    if local.is_file():
        lsha = crossmachine.sha1_of_file(local)
        if lsha and lsha.lower() != sha:
            print("  ! 本机这份文件与云端登记不一致 (不算失败) —— 下发的会是**云端**那份.")
    print()
    print("下一步:")
    print("  1) 把 expected.json 发给朋友, 放在 RUN-CROSS-VERIFY.bat 同一层;")
    print("  2) 朋友在 **已登录这个账号** 的机器上双击 RUN-CROSS-VERIFY.bat;")
    print("  3) 零写入模式**不需要复位云端** —— 云端从头到尾没被动过.")
    return 0


def _redact(text: str) -> str:
    """报告要回传, 抹掉对方机器的 Windows 用户名."""
    out = str(text)
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        val = os.environ.get(key, "").strip()
        if val:
            out = out.replace(val, f"%{key}%")
            out = out.replace(val.replace("\\", "/"), f"%{key}%")
    return out


def _entry_label() -> str:
    """报告里的 `tool.entry` —— 这句话到底是 exe 还是源码跑出来的.

    参考 `tools/machine_probe.py:_entry_label`. ⚠ `sys._MEIPASS` 在 onefile / onedir
    **都会**被设置, 所以不能只看 `hasattr` —— 必须看解包目录的形态:
    落在 `%TEMP%` 下 = onefile, 在 exe 同级 = onedir.

    (此前这里硬编码着 `"run_sandbox_probe/unspecified"` —— 那是开发时的占位串,
    写进回传报告里会让人以为跑的是沙箱, 已改.)
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        try:
            return f"source:{Path(__file__).name}"
        except NameError:  # pragma: no cover
            return "source"
    try:
        temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
        if temp and os.path.abspath(meipass).lower().startswith(
            os.path.abspath(temp).lower()
        ):
            return "exe-onefile"
    except Exception:  # pragma: no cover
        pass
    return "exe-onedir"


def _steam_shutdown(install, timeout: float = 45.0) -> bool:
    """让 Steam 退出 (为 --autoreset 在删档前用). 返回是否确认已退出."""
    try:
        exe = launch.steam_exe_path(install.root)
        subprocess.Popen([str(exe), "-shutdown"])  # noqa: S603
    except Exception:
        return False
    end = time.time() + timeout
    while time.time() < end:
        if not processes.find_pids("steam.exe"):
            return True
        time.sleep(1.5)
    return not processes.find_pids("steam.exe")


def _game_running(image: str = "DarkSoulsIII.exe") -> bool | None:
    """游戏是否在运行. `True`/`False`/`None`(判断不出).

    和 `guard.steam_running()` 同一套约定: **判断不出时返回 `None`**, 由调用方决定
    是提示还是放行 —— 不要默默当成"没在跑".
    测试钩子 `SCB_GAME_RUNNING` (`0`/`1`): 隔离环境里没法保证真机游戏关着, 只能钉死.
    """
    raw = os.environ.get("SCB_GAME_RUNNING")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no", "off")
    if sys.platform != "win32":
        return None
    try:
        return bool(processes.find_pids(image))
    except Exception:
        return None


def _account_probe(install, expected_aid: int) -> dict[str, Any]:
    """三个独立信号一起看 —— 单看注册表会被 `ActiveUser=0` 骗."""
    reg = accounts.get_active_accountid()
    act = None
    try:
        act = accounts.active_account(Path(install.root))
    except Exception:
        act = None
    most = act.account_id if act else None
    ud = install.userdata / str(expected_aid)
    ud_exists = ud.is_dir()
    # 判"账号不对"必须**先要正面证据指向别处**, 且本机没有目标账号的直接痕迹.
    # 只凭注册表就报冲突是不行的: `ActiveUser` 常为 0, 而"记住登录"的机器上
    # 它又可能停留在上一次登录的账号 —— 那会把一次正常测试误拦成跨账号.
    ok = (reg == expected_aid) or (most == expected_aid) or ud_exists
    conflict = (not ok) and ((reg is not None) or (most is not None))
    return {
        "registry_active_user": reg,
        "loginusers_most_recent": most,
        "loginusers_account_name": act.account_name if act else "",
        "loginusers_persona_name": act.persona_name if act else "",
        "expected_userdata_dir": str(ud),
        "expected_userdata_dir_exists": ud_exists,
        "verdict_ok": ok,
        "verdict_conflict": conflict,
    }


def _manual_hint(appid: str, wait_s: float) -> None:
    """**人工接管**说明: 把关卡要敲的**两行**打出来, 判定与报告逻辑一个字都不改.

    为什么是"两行"而不是"一行": 2026-10-13 两轮真机都栽在同一处 —— "本会话已点亮"
    的判据有可能**假阳性** (日志轮转后新档没有 `Client version:` 行, 只能退回全文件
    判定, 上一会话的活动就被当成本会话的). 一旦它判成"已点亮", 上面的点亮闸门 P5
    整个被跳过; 若这里还只让人敲 `cloud_sync_down`, Steam 会以 `AutoCloud is
    disabled` 直接拒掉 —— 整轮白跑. 所以人工接管必须**自带点亮步骤**:
    人就在键盘前, 多敲一行花 10 秒, 换掉一整轮 5 分钟; 且 `testappcloudpaths`
    是幂等的, 重复敲没有副作用.

    为什么不用自动发键: 往 Steam 控制台发键靠"点输入行"的坐标配方, 跟窗口尺寸/DPI
    绑得很死 —— 2026-10-13 实测在别人的机器上 4 次重试全部打空 (墨迹量一次没上去,
    工具因此**拒绝回车**, 没有误发).
    """
    print()
    print("  ── 需要你手动敲两行 (本脚本不碰 Steam 窗口, 只负责盯日志) ──")
    print("   1) 先打开 Steam 控制台: 按 Win+R -> 输入 steam://open/console -> 回车")
    print("      (也可以: Steam 左上角 `Steam` 菜单 -> `控制台` / Console)")
    print("      ⚠ 控制台打开了 != 输入行有焦点: 先用鼠标点一下最底下那行输入框.")
    print("   2) 第一行 —— 打开这个游戏的云通道 (**别跳过**; 不确定就再敲一遍):")
    print(f"        {cloudchannel.command_enable(appid)}")
    print("      敲对了**没有任何成功提示**, 这是正常的. 想确认就再敲一次")
    print(f"      find testappcloudpaths: 看到 \"{appid}\" = 已生效; \"0\" = 没生效.")
    print("   3) 第二行 —— 让 Steam 从云端下载:")
    print(f"        {cloudchannel.command_sync_down(appid)}")
    print("   4) 敲完**别动 Steam**, 回到这个黑窗口等着: 本脚本自己盯日志,")
    print(f"      最长再等 {int(wait_s)} 秒. 看到下行就出报告, **不用重跑本脚本**.")
    print()


def cmd_xfer_verify(args: argparse.Namespace) -> int:
    """阶段 2 (在朋友机跑): 点亮 -> `cloud_sync_down` -> 比对字节 -> 出报告.

    **不启动游戏** (所以不会碰到存档内容本身), 也不需要读存档语义.
    """
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd() / "reports-cross"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        exp = crossmachine.Expected.load(args.expected)
    except ValueError as exc:
        print(f"✗ 读不到可用的 expected.json: {exc}")
        print("  需要本机 push 阶段生成的那个文件, 与脚本放在同一个文件夹里.")
        return 3

    print("── 跨机往返 · 阶段 2/2 (verify) ──")
    print(f"  期望 appid        : {exp.appid}")
    print(f"  期望云相对路径    : {exp.rel_norm}")
    print(f"  期望 sha1         : {exp.sha1}")
    print(f"  期望大小          : {exp.size:,} B" if exp.size else "  期望大小          : (未记录)")
    print(f"  应登录的账号      : {exp.accountid}"
          f"{'  (' + exp.account_name + ')' if exp.account_name else ''}")
    print(f"  本机 push 时云端 CN: {exp.cloud_change_number}")
    if exp.label:
        print(f"  标注              : {exp.label}")
    print()

    install = pick_install(args)

    # ---- P1: 账号必须一致 ----
    ap = _account_probe(install, int(exp.accountid))
    print("账号核对:")
    print(f"  注册表 ActiveUser      : {ap['registry_active_user']}")
    print(f"  loginusers 最近登录    : {ap['loginusers_most_recent']}")
    print(f"  userdata\\{exp.accountid}\\ 存在: {ap['expected_userdata_dir_exists']}")
    if not ap["verdict_ok"] or ap["verdict_conflict"]:
        print()
        print("✗ **当前 Steam 登录的不是这个账号**, 无法继续 (跨账号导入是本项目红线).")
        print(f"  请先: 退出 Steam -> 用账号 {exp.accountid}"
              f"{' (' + exp.account_name + ')' if exp.account_name else ''} 登录 -> 再重跑本脚本.")
        if ap["loginusers_account_name"]:
            print(f"  (现在这台机器上记住的最近账号是: {ap['loginusers_account_name']})")
        return 3
    print("  => OK")
    print()

    # ---- P2: 目标路径 ----
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        print("✗ 读不到 %APPDATA% —— 这套流程要在 Windows 上跑.")
        return 3
    try:
        target = crossmachine.locate_target(appdata, exp.rel_norm)
    except ValueError as exc:
        print(f"✗ 契约里的云相对路径不可用: {exc}")
        return 3
    print(f"目标文件: {_redact(str(target))}")

    # ---- P3: Steam 在跑 ----
    from src.transport.guard import steam_running

    running = steam_running()
    if running is False:
        if args.start_steam:
            print("Steam 没在跑, 正在拉起 ...")
            launch.start_steam(install.root)
            launch.wait_for_process("steam.exe", timeout=float(args.steam_wait))
            time.sleep(12.0)
            running = steam_running()
        if running is False:
            print("✗ Steam 没在运行 —— 控制台命令需要 Steam 活着.")
            print("  请启动 Steam 并登录该账号, 再重跑本脚本.")
            return 3

    # ---- P3.5: 游戏必须关着 ----
    # 游戏自己也会跑 Auto-Cloud, 两条流程会抢同一个文件; 而且游戏一启动就会把
    # 存档目录整个重扫一遍. 所以这一步是**硬门槛**, 不靠说明书提醒用户.
    game = _game_running()
    if game:
        print()
        print("✗ DARK SOULS III 正在运行 —— 请先**完全退出游戏**再重跑本脚本.")
        print("  原因: 游戏自己也在跑云同步, 会和本流程抢同一个存档文件.")
        print("  检查: 任务管理器 (Ctrl+Shift+Esc) 里搜 `DarkSouls`, 一条都不该有.")
        return 3
    print(f"游戏未运行: {'是' if game is False else '无法确认 (继续)'}")
    print()

    before = _file_state(target)
    print(f"  现状: {'存在' if before['exists'] else '不存在'}"
          + (f", {before['size']:,} B, sha1={before['sha1'][:16]}…" if before["exists"] else ""))

    # ---- P4: 干净起点 ----
    if before["exists"]:
        same = (before["sha1"] or "").lower() == exp.sha1.lower()
        print()
        print("! 该机上已经有这个文件了 —— 本次**无法证明**它是这次从云端下发的" if same
              else "! 该机上已经有一个**内容不同**的同名文件 —— 下行会被跳过")
        if not args.autoreset:
            print(f"  请: 完全退出 Steam -> 删除 {_redact(str(target))} -> 重跑本脚本.")
            print("  (或加 --autoreset --yes: 脚本会先备份到报告目录再删除, 然后自己重启 Steam)")
            return 3
        if not args.yes:
            print("  --autoreset 需要同时加 --yes 才执行删除.")
            return 3
        print("  --autoreset: 备份 + 删除旧文件 (先关 Steam, 删完再起来) ...")
        if not _steam_shutdown(install):
            print("  ✗ Steam 不肯退出, 为安全起见中止 (绝不在 Steam 运行时改被监视文件).")
            return 3
        prior_dir = out_dir / "prior"
        prior_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, prior_dir / (target.name + ".prior"))
        target.unlink()
        print(f"  已备份到 {_redact(str(prior_dir))}, 旧文件已移除.")
        launch.start_steam(install.root)
        launch.wait_for_process("steam.exe", timeout=float(args.steam_wait))
        time.sleep(12.0)
        before = _file_state(target)

    # ---- P5: 点亮 ----
    # 取两处水位线: 之后只认"新增行", 免得把上一会话的残留当成本会话证据.
    clog = cloud_log_path(install)
    watermark = len(crossmachine.read_tail_lines(clog))
    cl_path = console_log_path(install)
    cl_watermark = len(crossmachine.read_tail_lines(cl_path))

    st = _detect_channel(install, str(exp.appid))
    render_channel(st)
    ack = None
    if st.lit is not True:
        # ⚠ 这一步**绝不能省**. `testappcloudpaths` 是**易失**的 (重启 Steam 即失效),
        # 而 DS3 的游戏级开关在朋友机上长期是关着的. 2026-10-13 那轮就是判据假阳性
        # (把 `Starting sync (…,Sync Disabled,)` 当成"活动") 把这里跳过了, 于是
        # `cloud_sync_down` 被 Steam 以 `AutoCloud is disabled` 直接拒掉, 整轮白跑.
        print("点亮本会话云通道 ...")
        ack = _enable_channel(install, str(exp.appid))
        if not ack.found:
            # 自动键入失败 (窗口尺寸/DPI 不同) ⇒ **转人工接管**, 与下面下行那步同一套
            # 约定: 人敲, 我自己等回执行. **不要求重跑** —— 早先那句"再重跑本脚本"
            # 会让对方白跑一趟, 而且他刚敲完就重跑反而更难.
            wait_s = float(getattr(args, "manual_wait", 0.0) or 0.0) or 180.0
            ack = _manual_enable(
                install, str(exp.appid), wait_s=wait_s, poll=float(args.poll)
            )
            if ack is None or not ack.found:
                print()
                print("✗ 等不到点亮回执行 —— 那一行可能没敲进控制台.")
                print(f"  可以在 Steam 控制台再敲一次: testappcloudpaths {exp.appid}")
                print("  敲完重跑本脚本.")
                return 4
        st = _detect_channel(install, str(exp.appid))
        print(f"  重判: {'已点亮' if st.lit else '未观察到活动 (继续)'}")
    print()

    # ---- 触发下行 ----
    send = make_sender(open_console=True)
    attempts = max(1, int(args.attempts))
    # `--manual`: 完全不碰 GUI, 直接走"你来敲 + 我自己等". 自动键入在有些机器上
    # (窗口尺寸/DPI 不同) 会 4 次全打空 ⇒ 与其白等那几轮点击, 不如让人一步到位.
    manual = bool(getattr(args, "manual", False))
    manual_wait = float(getattr(args, "manual_wait", 0.0) or 0.0)
    if manual:
        attempts = 1
    code, message, ev = "pending", "未开始", crossmachine.DownEvidence(appid=str(exp.appid))
    for attempt in range(1, attempts + 1):
        cmd_txt = cloudchannel.command_sync_down(str(exp.appid))
        if manual:
            print("[1/1] 手动模式 (本脚本不碰 GUI, 只负责监测):")
            _manual_hint(str(exp.appid), manual_wait)
        else:
            print(f"[{attempt}/{attempts}] 发送控制台命令: {cmd_txt}")
            ok = send(cmd_txt)
            print(f"  送达: {'是' if ok else '否'}")
            if not ok:
                # 自动失败 ⇒ **转人工**, 不再反复点 GUI (再点只会把输入焦点抢走,
                # 反而会打断正在敲字的人), 等待窗口也随之加长.
                manual = True
                _manual_hint(str(exp.appid), manual_wait)
        deadline = time.time() + (manual_wait if manual else float(args.wait))
        while time.time() < deadline:
            time.sleep(max(1.0, float(args.poll)))
            lines = crossmachine.read_tail_lines(clog)[watermark:]
            ev = crossmachine.parse_down_evidence(lines, str(exp.appid))
            code, message = crossmachine.decide(
                exp.rel_norm, exp.sha1, _file_state(target)["sha1"], ev
            )
            if code != "pending":
                break
        print(f"  判定: {code} — {message}")
        if code in ("pass", "byte-mismatch", "nothing-to-do"):
            break
        if manual:  # 人工接管只有一轮: 人没敲就没敲, 再循环也没有意义
            break
        if attempt < attempts:
            print("  还没看到下行证据, 重试一次 ...")

    after = _file_state(target)
    print()
    print("── 结论 ──")
    if code == "pending":
        code = "timeout"
        message = (
            "人工接管等待期内仍未观察到下行证据 (超时, 那一行可能没敲进控制台)"
            if manual else f"{attempts} 次触发后仍未观察到下行证据 (超时)"
        )
    ok_pass = code == "pass"
    print(("✓ 通过: " if ok_pass else "✗ 未通过: ") + message)
    print(f"  本机文件 : {'存在' if after['exists'] else '不存在'}"
          + (f", {after['size']:,} B, sha1={after['sha1'][:16]}…" if after["exists"] else ""))
    print()

    # ---- 报告 ----
    try:
        from src.version import VERSION as _V
    except Exception:  # pragma: no cover
        _V = ""
    lines_tail = crossmachine.read_tail_lines(clog)[watermark:]
    tail = [ln for ln in lines_tail if str(exp.appid) in ln][-40:]
    # 本会话内该 appid 的**全部**行 (诊断用): 判据当时看到了什么, 决定了它为什么
    # 说"已点亮/未点亮" —— 回传包里必须有, 否则下一轮还是只能靠复现猜 (见下面 bundle).
    _all_lines = crossmachine.read_tail_lines(clog)
    _sess_from = st.session_start_line + 1 if st.session_start_line >= 0 else 0
    session_appid_lines = [x for x in _all_lines[_sess_from:] if str(exp.appid) in x][-120:]
    report = {
        "schema": "scb-xfer-report/1",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": {"version": _V, "entry": _entry_label()},
        "expected": exp.to_dict(),
        "account_check": {k: v for k, v in ap.items()},
        "target": {
            "path": _redact(str(target)),
            "before": {k: (_redact(v) if isinstance(v, str) else v) for k, v in before.items()},
            "after": {k: (_redact(v) if isinstance(v, str) else v) for k, v in after.items()},
        },
        "channel": st.to_dict(),
        "console_ack": ack.to_dict() if ack is not None else None,
        "down": {"attempts": attempts, "mode": "manual" if manual else "auto",
                 "evidence": ev.to_dict()},
        "verdict": {"code": code, "message": message, "passed": ok_pass},
        "cloud_log_tail": [_redact(x) for x in tail],
        "cloud_log_session_appid": [_redact(x) for x in session_appid_lines],
        "note": "本报告不含存档内容; 只含哈希/大小/路径(已抹去用户名)与 Steam 日志行.",
    }
    jpath = out_dir / "cross_report.json"
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md = [
        "# 跨机往返验证报告 (阶段 2/verify)",
        "",
        f"- 生成时间: {report['generated_at']}",
        f"- 工具版本: {_V}",
        f"- 期望 appid: `{exp.appid}`   云相对路径: `{exp.rel_norm}`",
        f"- 期望 sha1: `{exp.sha1}`  (大小 {exp.size:,} B)" if exp.size else f"- 期望 sha1: `{exp.sha1}`",
        f"- 应登录账号: `{exp.accountid}`" + (f" ({exp.account_name})" if exp.account_name else ""),
        f"- 目标文件: `{_redact(str(target))}`",
        f"- 触发方式: `{'人工接管 (自动键入失败后由人敲)' if manual else '自动键入'}`",
        "",
        "## 结论",
        "",
        f"**{'通过' if ok_pass else '未通过'}** — {message}",
        "",
        f"- 下行前: {'存在' if before['exists'] else '不存在'}"
        + (f", sha1=`{str(before['sha1'])[:40]}`" if before["exists"] else ""),
        f"- 下行后: {'存在' if after['exists'] else '不存在'}"
        + (f", sha1=`{str(after['sha1'])[:40]}`" if after["exists"] else ""),
        f"- 云端是否下发: `{ev.downloaded(exp.rel_norm)}`  "
        f"(`Download OK for file` 命中 {len(ev.download_ok)} 次)",
        f"- Steam 认为无事可做: `{ev.already_synced_cn}`",
        f"- 本会话通道: `{'已点亮' if st.lit else ('未点亮' if st.lit is False else '无法判断')}`",
        "",
        "## 下行证据 (cloud_log 新增行, 已抹去用户名)",
        "",
        "```",
        *[x for x in tail if "download" in x.lower()][:20],
        "```",
        "",
        "## 边界",
        "",
        "- 本流程**没有启动任何游戏**, 只用了 Steam 控制台的 `cloud_sync_down`。",
        "- 报告不含存档内容, 只有哈希/大小/路径 (Windows 用户名已抹成 `%USERPROFILE%`)。",
    ]
    mpath = out_dir / "cross_report.md"
    mpath.write_text("\n".join(md) + "\n", encoding="utf-8")

    import zipfile  # 局部导入: 只有出包时才需要

    zpath = out_dir / "cross_bundle.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(jpath, jpath.name)
        zf.write(mpath, mpath.name)
        # ⚠ 刻意**不把 prior/ 打进包里** —— 那里是对方机器上原文件的字节备份,
        # 打进去就等于把存档内容外传了, 与"报告不含存档内容"的说法冲突.
        # 它只留在这台机器上, 供本地复核用.
        evf = out_dir / "cloud_log_newlines.txt"
        evf.write_text("\n".join(_redact(x) for x in lines_tail) + "\n", encoding="utf-8")
        zf.write(evf, evf.name)
        # ② 本 Steam 会话里该 appid 的**全部**行 (不限于本次新增).
        # 为什么必须带: 2026-10-13 那轮回传包里只有 6 行"新增行", 看不到"判据当时
        # 为什么说已点亮" —— 只能靠本机拿基线日志去复现才找出真因. 有这一份,
        # 下一轮就能直接对着现场读.
        sessf = out_dir / "cloud_log_session_appid.txt"
        sessf.write_text(
            "\n".join(_redact(x) for x in session_appid_lines) + "\n", encoding="utf-8"
        )
        zf.write(sessf, sessf.name)

    print(f"报告已写出: {out_dir}")
    print(f"  回传这个: {zpath}")
    print("  (报告不含存档内容, 可安全发送)")
    print()
    print("⚠ 在收到本机确认之前, **请不要启动 DS3** —— 本机要把云端复位成你自己的存档.")

    if args.json:
        _dump_json(args.json, report)
    return 0 if ok_pass else 1


# ---------------------------------------------------------------- 魂二 (没有 Steam 云)


def _ds2_account_arg(args: argparse.Namespace) -> int | None:
    raw = str(getattr(args, "account", "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"--account 必须是数字 (accountid): {raw!r}")


def _render_ds2_location(loc: ds2.SaveLocation) -> None:
    print(f"── 魂二存档 ({ds2.PROFILE.name}, appid {ds2.PROFILE.appid}) ──")
    # 这一行每次都要打: 用户的默认预期是"魂二也有云", 不写清楚就会一直找开关.
    print("  Steam 云 : 不支持 —— 游戏本身没有该功能 (不是配置问题, 也没有开关可开)")
    if loc.dir is None:
        print("  存档目录 : (未定位)")
    else:
        print(f"  存档目录 : {loc.dir}")
        print(f"  账号     : accountid={loc.accountid}  (SteamID64={loc.steamid64})")
        if loc.created:
            print("  ⚠ 该目录是按规则生成的, 不是磁盘上实测到的 —— 请确认账号无误")
        if loc.files:
            total = sum(f.size for f in loc.files)
            print(f"  文件     : {len(loc.files)} 个, 共 {total:,} 字节")
            for f in loc.files:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.mtime))
                print(f"      {f.rel:<24} {f.size:>10,} B  {ts}  sha256:{f.sha256[:12]}")
        else:
            print("  文件     : (无)")
    for e in loc.evidence:
        print(f"  · {e}")
    for n in loc.notes:
        print(f"  ! {n}")


def cmd_save_status(args: argparse.Namespace) -> int:
    """看魂二存档在哪、有哪些文件. 只读."""
    loc = ds2.locate(
        args.appid,
        accountid=_ds2_account_arg(args),
        appdata_dir=getattr(args, "appdata", "") or None,
        include_all=bool(getattr(args, "all", False)),
    )
    _render_ds2_location(loc)
    if getattr(args, "json", ""):
        _dump_json(args.json, {"location": loc.to_dict()})
    return 0 if loc.resolved else 2


def cmd_save_out(args: argparse.Namespace) -> int:
    """把魂二存档导出成一个可跨机搬走的包 (`--to` 指向同步盘/U盘目录)."""
    loc = ds2.locate(
        args.appid,
        accountid=_ds2_account_arg(args),
        appdata_dir=getattr(args, "appdata", "") or None,
        include_all=bool(getattr(args, "all", False)),
    )
    if loc.dir is None:
        _render_ds2_location(loc)
        print("\n[ds2] 定位不到魂二存档目录, 无法导出")
        return 2

    res = ds2.export_pack(
        loc,
        args.to,
        include_all=bool(getattr(args, "all", False)),
        label=str(getattr(args, "label", "") or ""),
    )
    if not res.ok:
        print(f"[ds2] 导出失败: {res.error}")
        return 1

    print("── 导出完成 ──")
    print(f"  包目录 : {res.pack_dir}")
    print(f"  清单   : {res.manifest_path}")
    print(f"  内容   : {res.file_count} 个文件, 共 {res.total_size:,} 字节")
    print()
    print("  下一步: 把这个文件夹整个放到能跨机同步的地方 (网盘同步目录 / U 盘),")
    print("          然后在另一台机器上执行  save-in --from <这个文件夹>")
    if getattr(args, "json", ""):
        _dump_json(args.json, {"location": loc.to_dict(), "export": res.to_dict()})
    return 0


def cmd_save_in(args: argparse.Namespace) -> int:
    """把包里的魂二存档导回本机. 默认只做检查 (dry-run), `--yes` 才真写."""
    pack = Path(args.from_dir)
    if not pack.is_dir():
        print(f"[ds2] 目录不存在: {pack}")
        return 2

    ok, problems = ds2.verify_pack(pack)
    if not ok:
        print("[ds2] 包校验未通过, 拒绝导入:")
        for p in problems:
            print(f"      {p}")
        return 2

    manifest = ds2.read_pack(pack)
    loc = ds2.locate(
        args.appid,
        accountid=_ds2_account_arg(args),
        appdata_dir=getattr(args, "appdata", "") or None,
        create=True,          # 目标机器可能还没玩过魂二, 允许按规则建目录
        with_files=False,
    )
    if loc.dir is None:
        _render_ds2_location(loc)
        print("\n[ds2] 定位不到本机的目标存档目录, 无法导入")
        return 2

    print("── 将要导入 ──")
    print(f"  来源包 : {pack}")
    print(f"  导出时间: {manifest.get('exported_at')}  (来自 {manifest.get('host')})")
    print(f"  内容   : {manifest.get('file_count')} 个文件, {manifest.get('total_size'):,} 字节")
    print(f"  目标   : {loc.dir}")
    print(f"  账号   : 包={manifest.get('accountid')}  本机={loc.accountid}")

    dry = not bool(getattr(args, "yes", False)) or bool(getattr(args, "dry_run", False))
    res = ds2.import_pack(
        pack,
        loc,
        backup_root=getattr(args, "backup_dir", "") or None,
        dry_run=dry,
    )

    if not res.ok:
        print()
        print(f"[ds2] 未导入: {res.error}")
        for b in res.blockers:
            print(f"      - {b}")
        # 校验/定位类问题用 2, 其余 (含缺 --yes) 用 3
        return 3 if not res.blockers else (2 if "sha256" in res.error or "跨账号" in res.error else 3)

    if dry:
        print()
        print(f"[ds2] 检查通过, 将写入 {len(res.written)} 个文件 (当前是 dry-run, 未写盘)")
        print("      确认无误后加 --yes 真正导入 (导入前会自动备份现有存档)")
    else:
        print()
        if res.backup_dir:
            print(f"[ds2] 已备份现有存档 -> {res.backup_dir}")
        print(f"[ds2] 已写入 {len(res.written)} 个文件:")
        for w in res.written:
            print(f"      {w}")
        print()
        print("      现在可以启动游戏了. 魂二没有云, 下次换机还是走这个包.")
    for n in res.notes:
        print(f"  ! {n}")
    if getattr(args, "json", ""):
        _dump_json(args.json, {"import": res.to_dict()})
    return 0


def _dump_json(path: str, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"\n已写出 JSON: {p}")


# ---------------------------------------------------------------- 入口


def cmd_wizard(args: argparse.Namespace) -> int:
    """交互式向导 —— 给不想记命令行的人走一遍完整流程.

    每一步都落到一次真实的 CLI 调用 (`main([...])`), 所以向导里能过的东西
    手打命令也一定能过; 反过来也一样. 逻辑在 `src/transport/wizard.py`,
    这里只提供"键盘 / 屏幕 / 执行"三个适配器.
    """

    def _prompt(text: str) -> str:
        try:
            return input(text)
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit("已中断 —— 什么都没有改动.") from None

    default_appid = str(getattr(args, "appid", "") or "374320")
    out = wizard_mod.run(
        prompt=_prompt,
        echo=lambda msg="": print(msg, flush=True),
        invoke=lambda argv: main(list(argv)),
        default_appid=default_appid,
        default_backup_dir=str(Path.cwd() / "backup"),
    )
    if args.json:
        _dump_json(
            args.json,
            {
                "wizard": {
                    "rc": out.rc,
                    "applied": out.applied,
                    "steps": [s.name for s in out.steps],
                }
            },
        )
    return out.rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge",
        description="SteamCloudBridge 搬运器 (M2: dry-run 优先; 默认不改动任何文件)",
    )
    p.add_argument("--steam-root", default="", help="完全覆盖式指定 Steam 根目录")
    p.add_argument("--account", default="", help="覆盖式指定 accountid (沙箱/多账户机器)")
    p.add_argument(
        "--version",
        action="version",
        version=f"SteamCloudBridgeBridge {VERSION}",
        help="打印版本号 (唯一来源: src/version.py)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="只读诊断: 落点 + 云通道状态")
    s.add_argument("--appid", required=True)
    s.add_argument("--json", default="")
    s.set_defaults(func=cmd_status)

    pl = sub.add_parser("plan", help="生成搬运计划 (dry-run, 默认不写盘)")
    pl.add_argument("--appid", required=True)
    pl.add_argument("--source", required=True, help="源存档文件路径")
    pl.add_argument("--backup-dir", default="", help="备份目录 (apply 时用)")
    pl.add_argument("--json", default="")
    pl.set_defaults(func=cmd_plan)

    ap = sub.add_parser("apply", help="真正执行搬运 (必须 --yes)")
    ap.add_argument("--appid", required=True)
    ap.add_argument("--source", required=True, help="源存档文件路径")
    ap.add_argument("--backup-dir", default="", help="备份目录 (强烈建议指定)")
    ap.add_argument("--snapshot-dir", default="", help="快照保存目录 (默认同 backup-dir)")
    ap.add_argument("--journal", default="", help="审计日志 (JSON Lines)")
    ap.add_argument("--then-sync", action="store_true",
                    help="[M3] 写完即触发云上行 (会自动启动 Steam 并点亮本会话通道)")
    ap.add_argument("--no-start-steam", action="store_true",
                    help="配合 --then-sync: Steam 没在跑就报错, 不自动拉起")
    ap.add_argument("--sync-attempts", type=int, default=3, help="trigger-sync 最多重试次数")
    ap.add_argument("--sync-settle", type=float, default=20.0, help="trigger-sync 每次触发后等待秒数")
    ap.add_argument("--steam-wait", type=float, default=150.0, help="等 Steam 起来的超时秒数")
    ap.add_argument("--yes", action="store_true", help="确认执行")
    ap.add_argument("--json", default="")
    ap.set_defaults(func=cmd_apply)

    ch = sub.add_parser("channel", help="[M3] 会话级云通道诊断 / 点亮")
    ch.add_argument("--appid", required=True)
    ch.add_argument("--enable", action="store_true", help="点亮本会话的云通道")
    ch.add_argument("--json", default="")
    ch.set_defaults(func=cmd_channel)

    sy = sub.add_parser("sync", help="[M3] 触发上行同步 + 校验 + 重试 (不必启动游戏)")
    sy.add_argument("--appid", required=True)
    sy.add_argument("--attempts", type=int, default=3, help="最多重试次数 (默认 3)")
    sy.add_argument("--settle", type=float, default=20.0, help="每次触发后等待秒数")
    sy.add_argument("--enable", action="store_true",
                    help="通道未点亮时先发 testappcloudpaths 点亮它")
    sy.add_argument("--start-steam", action="store_true",
                    help="Steam 没在跑时自动拉起 (拉起后通道是会话级的, 需配 --enable)")
    sy.add_argument("--steam-wait", type=float, default=150.0, help="等 Steam 起来的超时秒数")
    sy.add_argument("--force", action="store_true", help="跳过云通道未点亮的检查")
    sy.add_argument("--json", default="")
    sy.set_defaults(func=cmd_sync)

    rb = sub.add_parser("rollback", help="[M3] 从快照回滚 (默认 dry-run)")
    rb.add_argument("--snapshot", required=True, help="快照 JSON 路径")
    rb.add_argument("--journal", default="")
    rb.add_argument("--yes", action="store_true", help="确认执行")
    rb.add_argument("--json", default="")
    rb.set_defaults(func=cmd_rollback)

    xp = sub.add_parser(
        "xfer-push",
        help="[M6] 跨机往返 阶段1/2: 写档 + 推上云 + 产出 expected.json (在本机跑)",
    )
    xp.add_argument("--appid", required=True)
    xp.add_argument("--source", default="",
                    help="源存档文件; 内容必须与云端已有副本**不同**, 否则空跑 "
                         "(加了 --from-cloud 就不需要它)")
    xp.add_argument("--from-cloud", action="store_true",
                    help="零写入: 不写档/不上传/不碰 Steam, 直接按云端**已登记**的副本 "
                         "出 expected.json (只验跨机下行; Steam 开着也能跑)")
    xp.add_argument("--out", default="", help="expected.json 输出目录 (默认 .\\xfer-out)")
    xp.add_argument("--backup-dir", default="", help="备份目录 (强烈建议指定)")
    xp.add_argument("--snapshot-dir", default="", help="快照保存目录 (默认同 backup-dir)")
    xp.add_argument("--journal", default="", help="审计日志 (JSON Lines)")
    xp.add_argument("--label", default="", help="本次用途标注 (如 test / restore)")
    xp.add_argument("--note", default="", help="写进 expected.json 的备注")
    xp.add_argument("--no-start-steam", action="store_true",
                    help="Steam 没在跑就报错, 不自动拉起")
    xp.add_argument("--sync-attempts", type=int, default=3)
    xp.add_argument("--sync-settle", type=float, default=20.0)
    xp.add_argument("--steam-wait", type=float, default=150.0)
    xp.add_argument("--yes", action="store_true", help="确认执行")
    xp.set_defaults(func=cmd_xfer_push)

    xv = sub.add_parser(
        "xfer-verify",
        help="[M6] 跨机往返 阶段2/2: 触发下行 + 比对字节 (在对方机器跑, 不必启动游戏)",
    )
    xv.add_argument("--expected", required=True, help="本机 push 阶段产出的 expected.json")
    xv.add_argument("--out-dir", default="", help="报告输出目录 (默认 .\\reports-cross)")
    xv.add_argument("--attempts", type=int, default=3, help="最多触发几次 (默认 3)")
    xv.add_argument("--wait", type=float, default=90.0, help="每次触发后等多久 (秒)")
    xv.add_argument("--poll", type=float, default=3.0, help="轮询间隔 (秒)")
    xv.add_argument("--manual", action="store_true",
                    help="完全不碰 GUI: 打印要敲的那一行让你自己敲, 工具只负责监测下行")
    xv.add_argument("--manual-wait", type=float, default=180.0,
                    help="转人工后最多等多久 (秒, 默认 180)")
    xv.add_argument("--start-steam", action="store_true", help="Steam 没在跑时自动拉起")
    xv.add_argument("--steam-wait", type=float, default=150.0)
    xv.add_argument("--autoreset", action="store_true",
                    help="目标已存在时: 备份 + 删除后重测 (必须同时加 --yes)")
    xv.add_argument("--yes", action="store_true", help="配合 --autoreset 使用")
    xv.add_argument("--json", default="")
    xv.set_defaults(func=cmd_xfer_verify)

    wz = sub.add_parser("wizard", help="交互式向导 (给不想记命令行的人)")
    wz.add_argument("--appid", default="", help="默认 appid (回车即用)")
    wz.add_argument("--json", default="")
    wz.set_defaults(func=cmd_wizard)

    # --- 魂二: 没有 Steam 云, 用"指定目录"当传输层 (同步盘 / U 盘) ---

    def _add_ds2_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--appid", default=ds2.PROFILE.appid, help=f"默认 {ds2.PROFILE.appid} (魂二原罪)"
        )
        sp.add_argument(
            "--account", default="", help="显式指定 accountid (默认从磁盘上的账号目录反查)"
        )
        sp.add_argument("--appdata", default="", help="覆盖 APPDATA (沙箱/测试用)")

    ss = sub.add_parser("save-status", help="[魂二] 看存档在哪、有哪些文件 (只读)")
    _add_ds2_args(ss)
    ss.add_argument("--all", action="store_true", help="连 *.bak 之类也一并列出")
    ss.add_argument("--json", default="")
    ss.set_defaults(func=cmd_save_status)

    so = sub.add_parser(
        "save-out", help="[魂二] 导出存档包 —— 放进同步盘 / U 盘就是你的\"云\""
    )
    _add_ds2_args(so)
    so.add_argument("--to", required=True, help="包输出目录 (建议指向网盘同步目录或 U 盘)")
    so.add_argument("--all", action="store_true", help="连 *.bak 之类也一起带走")
    so.add_argument("--label", default="", help="给这次导出起个名字 (写进清单)")
    so.add_argument("--json", default="")
    so.set_defaults(func=cmd_save_out)

    si = sub.add_parser("save-in", help="[魂二] 从存档包导入 (导入前强制备份)")
    _add_ds2_args(si)
    si.add_argument("--from", dest="from_dir", required=True, help="包目录 (含 savepack.json)")
    si.add_argument("--backup-dir", default="", help="备份目录 (默认在存档同级 save-backup)")
    si.add_argument("--dry-run", action="store_true", help="只检查, 不写盘")
    si.add_argument("--yes", action="store_true", help="确认导入 (不加则只做检查)")
    si.add_argument("--json", default="")
    si.set_defaults(func=cmd_save_in)

    dl = sub.add_parser(
        "daily", help="[v1.0] 日常使用: 点亮本会话云通道 (+ 可选启动游戏)"
    )
    dl.add_argument("--appid", required=True)
    dl.add_argument("--manual", action="store_true",
                    help="完全不碰 GUI: 把要敲的那一行打给你, 自己盯日志等回执")
    dl.add_argument("--manual-wait", type=float, default=180.0,
                    help="人工接管时最多等多少秒 (默认 180)")
    dl.add_argument("--launch", action="store_true",
                    help="点亮后立刻让 Steam 启动这个游戏")
    dl.add_argument("--start-steam", action="store_true",
                    help="Steam 没在跑就自己拉起 (默认报错)")
    dl.add_argument("--steam-wait", type=float, default=150.0,
                    help="等 Steam 起来的超时秒数")
    dl.add_argument("--poll", type=float, default=2.0, help="日志轮询间隔秒数")
    dl.add_argument("--json", default="")
    dl.set_defaults(func=cmd_daily)

    return p


def main(argv: list[str] | None = None) -> int:
    harden_console()  # 必须在任何 print 之前: 一个 ™ 就能让进程崩掉
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
