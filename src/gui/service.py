"""图形向导的**后端动作层** —— 所有"干活"的逻辑都在这里, 一行 tkinter 都没有.

这样分层不是为了好看: 界面层只负责"显示 + 点击", 出问题最多是难看;
**凡是有副作用的动作 (复制存档、启动游戏) 都在这里, 可以用普通单元测试覆盖**,
不需要真的开窗口. 现有的判据 (`src/transport/*`) 一律复用, 这里只做编排 ——
任何"重新实现一遍判据"的写法都会让 GUI 与 CLI 的判断悄悄分叉.

与 `tools/bridge.py` 的关系: 它是 CLI 的输出层 (大量 `print`), 装在 `tools/` 下,
单文件构建时**不会**被内嵌. 所以 GUI 不 import 它, 而是直接调它下面那层 `src/*`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..steam import accounts, cloud_log, discovery, launch, processes
from ..transport import cloudchannel, crossmachine, ds2
from ..transport import target as target_mod
from ..transport.guard import steam_running

DEFAULT_APPID = "374320"
GAME_IMAGE = "DarkSoulsIII.exe"
BACKUP_DIR_NAME = "save-backup"
ENABLE_VAR = "testappcloudpaths"  # 点亮通道用的控制台变量名 (与 CLI 同一处判据)

# 状态档: ok 正常 / warn 要注意 / bad 需要处理 / info 纯说明
OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"


# ---------------------------------------------------------------- 数据结构


@dataclass
class Check:
    """一条检查项, 界面直接渲染成一行."""

    label: str
    state: str
    detail: str = ""
    fix: str = ""


@dataclass
class Environment:
    """一次体检的全部结论. 界面只读它, 不再自己算任何东西."""

    appid: str = DEFAULT_APPID
    game_name: str = ""
    error: str = ""

    steam_root: Path | None = None
    accountid: int | None = None
    game_installed: bool = False
    game_dir: Path | None = None

    cloud_root: Path | None = None
    cloud_root_source: str = ""
    save_path: Path | None = None
    save_exists: bool = False
    save_size: int = 0

    cloud_change_number: int = 0
    remote_sha: str = ""
    remote_size: int = 0
    remote_remotetime: int = 0

    steam_running: bool | None = None
    game_running: bool | None = None
    channel_lit: bool | None = None
    channel_reason: str = ""
    channel_hint: str = ""

    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # 供后续动作复用 (不进界面)
    install: Any = None
    channel: Any = None

    @property
    def ready_to_play(self) -> bool:
        """能否直接进入"点亮并启动"这一步."""
        return bool(self.save_path) and self.game_installed and self.game_running is not True

    @property
    def can_backup(self) -> bool:
        return self.cloud_root is not None and self.cloud_root.is_dir()


@dataclass
class BackupFile:
    rel: str
    size: int
    sha256: str


@dataclass
class BackupResult:
    ok: bool = False
    dest: Path | None = None
    files: list[BackupFile] = field(default_factory=list)
    total_bytes: int = 0
    verified: bool = False
    error: str = ""
    created: str = ""


@dataclass
class EnableOutcome:
    ok: bool = False
    ack_line: str = ""
    reason: str = ""
    waited: float = 0.0


# ---------------------------------------------------------------- 小工具


def fmt_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} B"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def console_log_path(install: discovery.SteamInstall) -> Path:
    return install.logs / "console_log.txt"


def cloud_log_path(install: discovery.SteamInstall) -> Path:
    return install.logs / "cloud_log.txt"


def app_data_roaming() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _game_running() -> bool | None:
    try:
        return bool(processes.find_pids(GAME_IMAGE))
    except Exception:
        return None


def cloud_root_of(entry: Any, game_install_dir: Path | None = None) -> Path | None:
    """从 Auto-Cloud 的**规则本身**推出存档根目录 (`root` + `path`).

    这是"证据驱动"的: `Evaluating rule N with root="WinAppDataRoaming"
    path="DarkSoulsIII"` 是 Steam 自己写下的同步范围. 退到"按游戏名猜目录"是
    这个项目明令禁止的写法 —— 猜错就会去操作一个无关目录.
    """
    if entry is None:
        return None
    for rule in getattr(entry, "rules", []) or []:
        if not isinstance(rule, dict):
            continue
        base = discovery.resolve_ac_root_dir(str(rule.get("root") or ""), game_install_dir)
        sub = str(rule.get("path") or "")
        if base is not None and sub:
            return base / sub
    return None


# 各 AC root 环境变量对应的本地基目录 —— 用于"从落点反推根目录"的第二级.
_ROOT_BASES = (
    ("APPDATA", lambda: app_data_roaming()),
    ("USERPROFILE", lambda: Path.home()),
)


def ac_root_from_save_path(save_path: Path | None) -> tuple[Path | None, str]:
    """规则行缺失时, 从**已确认的落点**往上反推 Auto-Cloud 根目录.

    为什么需要这条兜底: `cloud_log.txt` 会按 ~1 MiB 轮转 (实测 9,719 行被切成
    42 行), 轮转后里面**一条规则行都不剩** —— 那时只认规则就会让"备份"整个失效,
    而备份恰恰是用户第一步要用的功能.

    反推的依据仍然是证据, 不是猜名字: 落点是 `resolve_targets` 用云日志 /
    `remotecache` 定出来的绝对路径, 它必然位于某个 AC 根之下. 返回
    `(根目录, 依据说明)`; 推不出来时给 `(落点所在目录, ...)` 这个保守结果.
    """
    if save_path is None:
        return None, ""
    try:
        resolved = Path(save_path).resolve()
    except OSError:
        return None, ""

    for label, get_base in _ROOT_BASES:
        try:
            base = get_base().resolve()
            rel = resolved.relative_to(base)
        except (ValueError, OSError):
            continue
        if not rel.parts:
            continue
        root = base / rel.parts[0]
        if root.is_dir():
            return root, f"按落点反推 · {label} 下第一层"

    parent = resolved.parent
    if parent.is_dir():
        return parent, "仅落点所在目录 · 未能确定云同步范围"
    return None, ""


# ---------------------------------------------------------------- 体检


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def inspect(appid: str = DEFAULT_APPID, *, steam_root: Path | str | None = None) -> Environment:
    """只读体检. **不写任何文件**, 可以在界面上随便刷新.

    `steam_root` 把视野钉在指定的 Steam 安装上. GUI 不传 (自动发现); 测试传它,
    好把整轮体检搬进 `make_fake_steam` 造的假环境里 —— 不去读真机注册表、
    也不碰真存档. 与 CLI 的 `--steam-root` 是同一个语义.
    """
    env = Environment(appid=str(appid))

    installs: list[discovery.SteamInstall] = []
    if steam_root is not None:
        inst = discovery.SteamInstall(root=Path(steam_root), source="--steam-root")
        if not inst.root.is_dir():
            env.error = f"指定的 Steam 目录不存在: {inst.root}"
            return env
        inst.libraries = _safe(lambda: discovery.load_libraries(inst), []) or []
        installs = [inst]
    else:
        installs = _safe(discovery.find_installations, []) or []

    if not installs:
        env.error = "找不到 Steam 安装目录"
        env.checks.append(Check("Steam 安装", BAD, "没有在本机找到 Steam", "先安装并登录 Steam"))
        return env

    install = installs[0]
    env.install = install
    env.steam_root = install.root
    env.checks.append(Check("Steam 安装", OK, str(_short(install.root))))

    # 账号: 注册表 ActiveUser 常为 0, 用 userdata 兜底 (与 CLI 完全同一套)
    accountid = _safe(accounts.get_active_accountid)
    if accountid is None:
        accountid = _safe(lambda: accounts.find_accountid_for_app(install.root, str(appid)))
    env.accountid = accountid

    libraries = _safe(lambda: install.libraries or discovery.load_libraries(install), []) or []
    manifests = _safe(lambda: discovery.load_app_manifests(libraries), {}) or {}
    manifest = manifests.get(str(appid))
    if manifest is not None:
        env.game_installed = True
        env.game_name = manifest.name or ""
        env.game_dir = manifest.install_dir

    cloud = cloud_log.CloudLog()
    cl = cloud_log_path(install)
    if cl.is_file():
        cloud = _safe(lambda: cloud_log.parse_cloud_log(cl), cloud_log.CloudLog()) or cloud

    resolution = _safe(
        lambda: target_mod.resolve_targets(
            install, str(appid), accountid, cloud=cloud, game_install_dir=env.game_dir
        )
    )
    if resolution is not None:
        env.cloud_change_number = int(resolution.change_number or 0)
        primary = resolution.primary
        if primary is not None:
            env.save_path = primary.abs_path
            env.remote_sha = primary.remote_sha or ""
            env.remote_size = int(primary.remote_size or 0)
            env.remote_remotetime = int(primary.remotetime or 0)
            env.save_exists = bool(primary.exists)
            env.save_size = int(primary.size or 0)
        env.notes.extend(resolution.notes or [])
        env.checks.append(
            Check(
                "存档位置",
                OK if primary is not None else WARN,
                str(_short(primary.abs_path)) if primary else "没能确定存档在哪儿",
                "" if primary else "先按第三步点亮一次云通道, 再回来刷新",
            )
        )

    entry = cloud.entries.get(str(appid))
    root = cloud_root_of(entry, env.game_dir)
    if root is not None:
        env.cloud_root, env.cloud_root_source = root, "按云同步规则"
    else:
        # 规则行缺失 (日志轮转 / 这台机器从没跑过自动云) —— 用落点反推
        env.cloud_root, env.cloud_root_source = ac_root_from_save_path(env.save_path)
    if env.cloud_root is not None:
        env.checks.append(
            Check(
                "云同步范围",
                OK,
                f"{_short(env.cloud_root)}  ({env.cloud_root_source})",
            )
        )

    # 本地与云端谁更新 (只报事实, 不做仲裁)
    if env.save_path and env.save_exists:
        detail = f"{fmt_size(env.save_size)}"
        if env.remote_remotetime:
            detail += f" · 云端有一份 {fmt_size(env.remote_size)}"
        env.checks.append(Check("本地存档", OK, detail))
    elif env.save_path:
        env.checks.append(
            Check("本地存档", WARN, "磁盘上没有这个文件", "启动一次游戏就会生成")
        )

    if env.game_installed:
        env.checks.append(
            Check("游戏本体", OK, f"{env.game_name or appid}  ·  {_short(env.game_dir)}")
        )
    else:
        env.checks.append(
            Check("游戏本体", WARN, f"没找到 appid {appid} 的安装清单", "确认游戏装在这台机器上")
        )

    env.steam_running = _safe(steam_running)
    env.checks.append(
        Check(
            "Steam 客户端",
            OK if env.steam_running else WARN,
            "正在运行" if env.steam_running else "没在运行",
            "" if env.steam_running else "点亮云通道需要 Steam 开着, 我可以帮你启动",
        )
    )

    env.game_running = _game_running()
    env.checks.append(
        Check(
            "游戏",
            WARN if env.game_running else OK,
            "正在运行" if env.game_running else "没在运行",
            "先退出游戏再点亮通道" if env.game_running else "",
        )
    )

    channel = _safe(
        lambda: cloudchannel.detect_channel_from_files(
            str(appid),
            cloud_log_path(install),
            console_log_path(install),
            steam_start_epoch=processes.steam_start_epoch(),
        )
    )
    if channel is not None:
        env.channel = channel
        env.channel_lit = channel.lit
        env.channel_reason = channel.reason or ""
        env.channel_hint = channel.hint or ""
        if channel.lit is True:
            env.checks.append(Check("本次云通道", OK, "已经点亮 (本次开 Steam 之后)"))
        elif channel.lit is False:
            env.checks.append(
                Check("本次云通道", WARN, "还没点亮", "进入第三步, 敲一行就亮了")
            )
        else:
            env.checks.append(
                Check("本次云通道", INFO, "暂时判断不了", "按第三步走一遍即可")
            )

    return env


def _short(p: Path | None, keep: int = 3) -> str:
    """把长路径缩成 `…\\a\\b\\c`, 界面上一行放得下."""
    if p is None:
        return "-"
    parts = Path(p).parts
    if len(parts) <= keep + 1:
        return str(p)
    return "…\\" + "\\".join(parts[-keep:])


# ---------------------------------------------------------------- 备份


def default_backup_root() -> Path:
    """备份放哪: 优先程序旁边 (跟着走, 直观), 不行就退到用户目录.

    候选顺序 = 显式指定 (`SCB_EXE_DIR`, 测试用) -> 打包后的 exe 同级 -> 仓库根
    -> 当前工作目录. 逐个试写一个临时文件, **确实能写才用它** —— 程序被放在
    `Program Files` 这类只读位置时, 硬写会直接失败, 而用户只会看到"备份没反应".
    """
    cands: list[Path] = []
    env_dir = os.environ.get("SCB_EXE_DIR", "").strip()
    if env_dir:
        cands.append(Path(env_dir))
    if getattr(sys, "frozen", False):
        try:
            cands.append(Path(sys.executable).resolve().parent)
        except OSError:
            pass
    else:
        try:
            cands.append(Path(__file__).resolve().parents[2])
        except (OSError, IndexError):
            pass
    try:
        cands.append(Path.cwd())
    except OSError:
        pass

    for c in cands:
        try:
            probe = c / ".scb-write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return c / BACKUP_DIR_NAME
        except OSError:
            continue
    fallback = Path.home() / "SteamCloudBridge-Backups"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def backup_saves(env: Environment, dest_root: Path | None = None) -> BackupResult:
    """把整个 Auto-Cloud 根目录完整复制一份, 再逐文件校验 sha256.

    **只复制, 不移动, 不删除**: 源目录一个字节都不动. 校验是必须的 ——
    一份没校验过的备份在需要的时候才被发现是坏的, 那就太晚了.
    """
    res = BackupResult()
    if env.cloud_root is None or not env.cloud_root.is_dir():
        res.error = "还没定位到存档目录, 无法备份"
        return res

    src: Path = env.cloud_root
    root = Path(dest_root) if dest_root else default_backup_root()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = root / f"dir-{stamp}" / src.name
    res.created = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, copy_function=shutil.copy2)
    except OSError as exc:
        res.error = f"复制失败: {exc}"
        return res

    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        d = dest / rel
        try:
            h_src, h_dst = _sha256(p), _sha256(d)
        except OSError as exc:
            res.error = f"校验读取失败: {rel}: {exc}"
            return res
        if h_src != h_dst or p.stat().st_size != d.stat().st_size:
            res.error = f"校验不一致: {rel}"
            return res
        res.files.append(BackupFile(rel=rel, size=p.stat().st_size, sha256=h_src))

    res.total_bytes = sum(f.size for f in res.files)
    res.verified = True
    res.ok = True
    res.dest = dest
    try:
        (dest.parent / "BACKUP-INFO.txt").write_text(_backup_info(env, dest, res), encoding="utf-8")
    except OSError:
        pass  # 说明文件写不出来不影响备份本身
    return res


def _backup_info(env: Environment, dest: Path, res: BackupResult) -> str:
    lines = [
        "SteamCloudBridge 存档备份",
        "=" * 58,
        "",
        f"备份时间  {res.created}",
        f"备份目录  {dest}",
        f"来源目录  {env.cloud_root}",
        f"游戏账号  {env.accountid if env.accountid is not None else '(未知)'}",
        "",
        "内容与校验 (sha256)",
        "-" * 58,
    ]
    for f in res.files:
        lines += [f"{f.rel}", f"    {f.size} 字节", f"    {f.sha256}"]
    lines += [
        "",
        "怎么恢复 (万一存档出问题)",
        "-" * 58,
        "前提: 先彻底退出游戏, 并退出 Steam (右下角托盘也要退干净),",
        "      否则刚放回去的存档会被云端旧档覆盖。",
        "",
        f"  1. 打开 {env.cloud_root}",
        "  2. 把里面的存档文件夹改名留底 (例如加上 .old)",
        f"  3. 从本备份里把内容整个复制回去",
        "  4. 重新启动 Steam 和游戏",
        "",
    ]
    return "\n".join(lines)


def list_backups(root: Path | None = None) -> list[dict[str, Any]]:
    """列出已做过的备份 (新→旧)."""
    root = Path(root) if root else default_backup_root()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(root.glob("dir-*"), reverse=True):
        if not d.is_dir():
            continue
        files = [p for p in d.rglob("*") if p.is_file() and p.name != "BACKUP-INFO.txt"]
        if not files:
            continue
        try:
            total = sum(p.stat().st_size for p in files)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime))
        except OSError:
            continue
        out.append({"dir": d, "when": when, "count": len(files), "bytes": total})
    return out


# ---------------------------------------------------------------- 点亮云通道


def enable_command(appid: str = DEFAULT_APPID) -> str:
    """要用户手敲的那一行. 判据在 `cloudchannel`, 这里只是取出来显示."""
    return cloudchannel.command_enable(str(appid))


def console_tail_depth(install: discovery.SteamInstall) -> int:
    """当前控制台日志的行数 —— 当作水位线, 只看之后新增的行."""
    return len(crossmachine.read_tail_lines(console_log_path(install)))


def watch_enable(
    install: discovery.SteamInstall,
    appid: str = DEFAULT_APPID,
    *,
    wait_s: float = 180.0,
    poll: float = 1.5,
    watermark: int | None = None,
    on_tick: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> EnableOutcome:
    """盯着 `console_log.txt`, 等用户把那行敲进去的回执.

    **为什么让用户手敲而不是自动发键**: 自动发键靠"点输入行"的坐标配方, 跟窗口
    尺寸/DPI 绑死 —— 在别人的机器上实测全部打空, 而且有误发到别的应用的风险.
    人本来就在键盘前, 敲一行十秒钟的事.

    只认控制台的**回执行** (`"testappcloudpaths" = "<appid>"`): 敲对了没有任何
    成功提示, 这是正常的.
    """
    out = EnableOutcome(waited=0.0)
    cl = console_log_path(install)
    wm = console_tail_depth(install) if watermark is None else int(watermark)
    start = time.time()
    deadline = start + max(5.0, float(wait_s))

    while time.time() < deadline:
        if should_stop is not None and should_stop():
            out.reason = "已取消"
            return out
        time.sleep(max(0.5, float(poll)))
        fresh = crossmachine.read_tail_lines(cl)[wm:]
        try:
            hit = cloudchannel.console_ack(ENABLE_VAR, str(appid), fresh)
        except Exception:
            hit = None
        if hit is not None and getattr(hit, "found", False):
            out.ok = True
            out.ack_line = getattr(hit, "last_line", "") or ""
            out.reason = "收到点亮回执"
            out.waited = time.time() - start
            return out
        if on_tick is not None:
            on_tick(time.time() - start)

    out.reason = "等待超时, 没看到回执"
    out.waited = time.time() - start
    return out


# ---------------------------------------------------------------- 动作


def open_console() -> bool:
    """唤起 Steam 控制台 (`steam://open/console`)."""
    try:
        os.startfile("steam://open/console")  # noqa: S606
        return True
    except Exception:
        return False


def launch_game(appid: str = DEFAULT_APPID) -> tuple[bool, str]:
    """让 Steam 把游戏拉起来.

    测试钩子 `SCB_LAUNCH_FAKE=<文件>`: 只把 URI 记进文件, **不真启动游戏**.
    与 CLI 同一套约定 —— 测试里绝不能真的动到用户的游戏.
    """
    uri = f"steam://rungameid/{appid}"
    fake = os.environ.get("SCB_LAUNCH_FAKE", "").strip()
    if fake:
        try:
            p = Path(fake)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(uri + "\n")
            return True, f"(测试钩子) 已记录 {uri}"
        except OSError as exc:
            return False, f"测试钩子写入失败: {exc}"
    try:
        os.startfile(uri)  # noqa: S606
        return True, uri
    except Exception as exc:
        return False, str(exc)


def start_steam(install: discovery.SteamInstall, timeout: float = 60.0) -> bool:
    """把 Steam 拉起来并等它出现 (点亮通道需要它活着)."""
    try:
        launch.start_steam(install.root)
        launch.wait_for_process("steam.exe", timeout=timeout)
        time.sleep(8.0)
        return bool(steam_running())
    except Exception:
        return False


def open_path(path: Path | str | None) -> bool:
    """用资源管理器打开一个目录 (或文件)."""
    if path is None:
        return False
    p = Path(path)
    if not p.exists():
        return False
    try:
        os.startfile(str(p))  # noqa: S606
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 魂二 (没有 Steam 云)

# 魂二**不支持 Steam 云** —— 不是"配置没开", 是游戏本身没有这个功能, 所以没有云
# 通道可以点亮, 也没有 `testappcloudpaths` 可执行. 它走的是 `src/transport/ds2.py`
# 那条"用户指定文件夹"的传输层 (网盘同步目录 / U 盘).
#
# 下面几个只是**薄包装**: 界面层不自己判断任何事, 一律回到 `ds2` 的判据 —— 一旦
# GUI 和 CLI 各写一份判断, 两边迟早给出不同的答案, 那是这个项目最不能接受的事.


def ds2_locate() -> ds2.SaveLocation:
    """定位本机魂二存档目录 (先在磁盘上找, 找不到才按规则生成). 只读."""
    return ds2.locate()


def ds2_locate_for_import() -> ds2.SaveLocation:
    """定位**导入目标**目录 —— 与 `ds2_locate` 的差别只有一处: 允许按规则新建.

    为什么导入单独一个: 换台新机器导入是主路径, 而那台机器多半从没玩过魂二,
    `<APPDATA>/DarkSoulsII/` 下是空的. 这时必须能按本机账号把目录名算出来,
    否则"导入"这条路从第一步就断了. 只读定位 (`ds2_locate`) 保持只读语义不变.
    """
    return ds2.locate(create=True, with_files=False)


def ds2_export(
    loc: ds2.SaveLocation, dest: str | Path, *, label: str = ""
) -> ds2.ExportResult:
    """把魂二存档导出成一个带清单的包."""
    return ds2.export_pack(loc, dest, label=label)


def ds2_verify_pack(pack_dir: str | Path) -> tuple[bool, list[str]]:
    """逐文件核对包内 sha256."""
    return ds2.verify_pack(pack_dir)


def ds2_read_pack(pack_dir: str | Path) -> dict[str, Any]:
    """读包清单 (外部输入, schema / appid 由 ds2 校验)."""
    return ds2.read_pack(pack_dir)


def ds2_import(pack_dir: str | Path, target: ds2.SaveLocation) -> ds2.ImportResult:
    """把包导入本机存档目录.

    内部顺序固定: 校验 -> 账号核对 -> 游戏进程检查 -> 强制备份 -> 原子写入.
    任一步不过就停, 不会写到一半.
    """
    return ds2.import_pack(pack_dir, target)


__all__ = [
    "DEFAULT_APPID",
    "GAME_IMAGE",
    "OK",
    "WARN",
    "BAD",
    "INFO",
    "Check",
    "Environment",
    "BackupFile",
    "BackupResult",
    "EnableOutcome",
    "inspect",
    "backup_saves",
    "list_backups",
    "default_backup_root",
    "cloud_root_of",
    "enable_command",
    "watch_enable",
    "console_tail_depth",
    "open_console",
    "launch_game",
    "start_steam",
    "open_path",
    "fmt_size",
    "ds2_locate",
    "ds2_locate_for_import",
    "ds2_export",
    "ds2_verify_pack",
    "ds2_read_pack",
    "ds2_import",
]
