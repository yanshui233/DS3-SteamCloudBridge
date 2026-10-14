"""魂二 (DARK SOULS II: Scholar of the First Sin, 335300) 存档搬运.

## 为什么单独一个模块

魂二**不支持 Steam 云**. 本项目实测四条相互独立的证据, 并与官方来源一致:

1. `localconfig.vdf` 里没有 `cloud/last_sync_state`, 也没有 `autocloud/*`
   (本机 110 个游戏有这两个字段, 魂二在没有的那 37 个里);
2. `userdata/<accountid>/335300/` 目录**不存在** —— 按本项目判据, 这就是"从未同步过";
3. 存档根没有 `steam_autocloud.vdf`;
4. `cloud_log.txt` 里魂二是唯一打
   `Not running AutoCloud. No config data found` 的 appid.

第 4 条要与魂三的 `AutoCloud is disabled. Run "testappcloudpaths ..."` 分清 ——
后者是"**有**配置、只是本会话开关被关掉", 前者是"**根本没有**配置". 一个是开关问题,
一个是这个功能压根不存在. 外部来源同结论 (Steam 商店页功能列表无 Steam Cloud;
PCGamingWiki 的 *Save game cloud syncing* 表里该栏为空).

所以它走不了 `target.py` / `cloudchannel.py` 那条 Auto-Cloud 路径: 没有 cloud_log
落点可读、没有 remotecache 可解析、也没有 `testappcloudpaths` 可以点亮.

## 那用什么当传输层

**调用方指定的任意目录** —— 放同步盘 (OneDrive / 坚果云 / Syncthing) 里就自动跨机,
放 U 盘就是手动插拔, **同一套代码**. 本模块只负责"把存档安全地搬进/搬出那个目录",
跨机传输本身交给用户选的那个目录.

## 落点

    %APPDATA%\\DarkSoulsII\\<SteamID64 十六进制>\\DS2SOFS0000.sl2

目录名规则与魂三**同构** (`01100001` + 8 位十六进制 accountid, 本机实测命中),
但仍然**不凭空拼**: 先枚举真实子目录, 用"能解析成 accountid" + "与本机账号一致"
双条件选中. 只有在确实没玩过 (目录不存在) 且调用方显式 `create=True` 时, 才按规则
生成目录名 —— 并把它写进证据链, 让报告里能看出这是"按规则建的"而非"磁盘上找到的".

## 红线 (与主程序一致)

* 只搬**同账号自己**的存档; 清单里的 accountid 与目标不一致 -> 拒绝;
* **游戏进程运行中**禁止改档 (魂二没有云, 不存在云端仲裁, 但游戏自己会覆盖);
* 导入**前强制备份**, 备份失败即中止;
* 写入走"临时文件 + `os.replace`", 并**显式 `os.utime` 刷 mtime**;
* 导入**只覆盖清单里的文件**, 不删除目标目录里多出来的东西 (删文件不是回滚手段).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..steam import accounts

__all__ = [
    "PACK_SCHEMA",
    "MANIFEST_NAME",
    "NOTE_NAME",
    "PROFILE",
    "GameProfile",
    "SaveFile",
    "SaveLocation",
    "ExportResult",
    "ImportResult",
    "game_running",
    "locate",
    "export_pack",
    "read_pack",
    "verify_pack",
    "import_pack",
    "sha256_of",
    "format_blob",
]

PACK_SCHEMA = "scb-savepack/1"
MANIFEST_NAME = "savepack.json"
NOTE_NAME = "先读我.txt"

#: 存档目录里这些不该进包 —— `.co2.bak` 是联机 mod 自己的轮转备份, 十个起步、
#: 每个都是一份完整存档大小, 搬来搬去只占地方, 且永远不该被"还原"回去.
DEFAULT_EXCLUDE = ("*.bak",)


@dataclass(frozen=True)
class GameProfile:
    """一个游戏在"存档搬运"这件事上的静态描述."""

    appid: str
    name: str
    save_subdir: str
    """`%APPDATA%` 下的子目录名 (大小写按磁盘实际)."""
    core_globs: tuple[str, ...]
    """核心存档 —— 导出时的默认集合."""
    exe_names: tuple[str, ...]
    """游戏进程名, 用于"运行中禁止改档"判断."""
    steam_cloud: bool
    """Steam 是否提供云存档. 魂二为 `False`, 这不是配置问题而是游戏没有该功能."""
    notes: tuple[str, ...] = ()


PROFILE = GameProfile(
    appid="335300",
    name="DARK SOULS II: Scholar of the First Sin",
    save_subdir="DarkSoulsII",
    # `.sl2` 是原版存档; `.co2` 是 SeamlessCoop (联机 mod) 的存档. 实测两者可能**不同步**
    # (用过 mod 之后 `.co2` 会比 `.sl2` 新), 所以默认两个都带 —— 只搬 `.sl2` 会搬走一份
    # 过期进度. 其余文件 (含 `.ds3os`) 用 `include_all=True` 一并带走.
    core_globs=("*.sl2", "*.co2"),
    exe_names=("DarkSoulsII.exe",),
    steam_cloud=False,
    notes=(
        "魂二没有 Steam 云存档, 跨机搬运靠你指定的目录 (同步盘 / U 盘)",
        "存档与账号绑定: 只搬自己账号的档",
    ),
)


# ---------------------------------------------------------------- 基础工具


def sha256_of(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """分块算 sha256 —— 存档 8 MB 起步, 一次读完没必要."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def format_blob(text: str | bytes, *, encoding: str = "utf-8") -> str:
    """把文本编码成 base64 —— 只用于清单里的不可见字符安全传输."""
    import base64

    raw = text.encode(encoding) if isinstance(text, str) else text
    return base64.b64encode(raw).decode("ascii")


_ENV_GAME_RUNNING = "SCB_GAME_RUNNING"


def game_running(exe_names: tuple[str, ...] | list[str] = PROFILE.exe_names) -> bool | None:
    """游戏进程是否在运行. 返回 `True` / `False` / `None`(判不出来).

    判不出来时返回 `None` 而不是 `False` —— 调用方应当把它当"不确定"来处理,
    而不是默默放行 (与 `guard.steam_running` 同一哲学).

    自动化测试钩子: `SCB_GAME_RUNNING` (`0`/`1`) 可显式钉死结果.
    """
    raw = os.environ.get(_ENV_GAME_RUNNING)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no", "off")

    if os.name != "nt":
        return None
    exe = shutil.which("tasklist")
    if not exe:
        return None
    try:
        for name in exe_names:
            proc = subprocess.run(
                [exe, "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                timeout=10,
            )
            text = proc.stdout.decode("gbk", "replace").lower()
            if name.lower() in text:
                return True
        return False
    except Exception:
        return None


# ---------------------------------------------------------------- 落点


@dataclass
class SaveFile:
    """存档目录里的一个文件."""

    rel: str
    size: int
    sha256: str
    mtime: float

    def to_dict(self) -> dict[str, Any]:
        return {"rel": self.rel, "size": self.size, "sha256": self.sha256, "mtime": self.mtime}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SaveFile":
        return cls(
            rel=str(data.get("rel", "")),
            size=int(data.get("size", 0)),
            sha256=str(data.get("sha256", "")),
            mtime=float(data.get("mtime", 0.0) or 0.0),
        )


@dataclass
class SaveLocation:
    """本机某账号的魂二存档目录解析结果."""

    appid: str = PROFILE.appid
    dir: Path | None = None
    accountid: int | None = None
    files: list[SaveFile] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created: bool = False
    """本次的目录是**按规则推导**出来的 (磁盘上原本没有这个目录), 不是磁盘实测到的.

    ⚠ 这里只推导路径, **不落盘** —— `locate()` 永远是只读的; 真正的 `mkdir`
    发生在 `import_pack()` 写入那一步 (目标目录不存在时由它创建).
    """

    @property
    def resolved(self) -> bool:
        return self.dir is not None

    @property
    def exists(self) -> bool:
        return self.dir is not None and self.dir.is_dir()

    @property
    def steamid64(self) -> int | None:
        return accounts.accountid_to_steamid64(self.accountid) if self.accountid else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "dir": str(self.dir) if self.dir else None,
            "accountid": self.accountid,
            "steamid64": self.steamid64,
            "resolved": self.resolved,
            "exists": self.exists,
            "created": self.created,
            "files": [f.to_dict() for f in self.files],
            "evidence": list(self.evidence),
            "notes": list(self.notes),
        }


def _appdata_root(appdata_dir: str | Path | None = None) -> Path | None:
    """本机 `%APPDATA%`. 读环境变量而不是 `expanduser` —— 沙箱重定向才生效."""
    if appdata_dir is not None:
        return Path(appdata_dir)
    raw = os.environ.get("APPDATA")
    if not raw:
        raw = os.path.expanduser("~")
    return Path(raw) if raw else None


def _scan_account_dirs(base: Path) -> list[tuple[Path, int]]:
    """枚举存档根下"名字能解析成 accountid"的子目录. 只读."""
    out: list[tuple[Path, int]] = []
    if not base.is_dir():
        return out
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for d in entries:
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        aid = accounts.hex_dir_to_accountid(d.name)
        if aid is not None:
            out.append((d, aid))
    return out


def _list_saves(save_dir: Path, *, include_all: bool = False) -> list[SaveFile]:
    """列出存档目录里的文件并算哈希. 只读. 默认排除 `DEFAULT_EXCLUDE`."""
    import fnmatch

    files: list[SaveFile] = []
    if not save_dir.is_dir():
        return files
    try:
        entries = sorted(save_dir.iterdir())
    except OSError:
        return files
    for p in entries:
        try:
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        if not include_all and any(fnmatch.fnmatch(p.name, pat) for pat in DEFAULT_EXCLUDE):
            continue
        files.append(SaveFile(rel=p.name, size=st.st_size, sha256=sha256_of(p), mtime=st.st_mtime))
    return files


def locate(
    appid: str = PROFILE.appid,
    *,
    accountid: int | None = None,
    appdata_dir: str | Path | None = None,
    create: bool = False,
    with_files: bool = True,
    include_all: bool = False,
) -> SaveLocation:
    """定位本机的魂二存档目录. 只读 (除非 `create=True` 且目录确实不存在).

    判定顺序 —— **先用磁盘证据, 再用规则**:

    1. 枚举 `<APPDATA>/DarkSoulsII/` 下能解析成 accountid 的子目录;
    2. 恰好一个 -> 用它 (磁盘上真实存在, 可信度最高);
    3. 多个 -> 用调用方给的 `accountid` 或本机当前账号匹配;
    4. 一个都没有 -> 只有 `create=True` 且账号已知时, 才按已验证的规则生成目录名
       (此时 `created=True`, 并在证据里写明"按规则生成, 非磁盘实测").
    """
    loc = SaveLocation(appid=str(appid))
    root = _appdata_root(appdata_dir)
    if root is None:
        loc.notes.append("读不到 %APPDATA%, 无法定位存档目录")
        return loc

    base = root / PROFILE.save_subdir
    loc.evidence.append(f"存档根: {base}")

    candidates = _scan_account_dirs(base)
    if candidates:
        loc.evidence.append(
            "磁盘上找到的账号目录: "
            + ", ".join(f"{d.name} (accountid={aid})" for d, aid in candidates)
        )

    wanted = accountid
    if wanted is None and len(candidates) != 1:
        # 只有磁盘证据**不足以判定**时才去问本机账号: 一个目录都没有 (新机器导入,
        # 主路径), 或者有多个需要挑一个. 恰好一个候选时它**本身就是答案** ——
        # 多读一次注册表不但白费, 还会在证据里留一行"取本机当前账号",
        # 让人误以为磁盘证据不足.
        active = accounts.get_active_accountid()
        if active is not None:
            wanted = active
            loc.evidence.append(f"磁盘上的账号目录不足以判定, 取本机当前账号 accountid={active}")

    picked: tuple[Path, int] | None = None
    if len(candidates) == 1:
        picked = candidates[0]
        # 只有一个候选时它就是答案, 但**调用方可能明确指定了另一个账号** ——
        # 那种冲突不能吞掉, 否则用户以为按自己说的走了.
        if accountid is not None and int(accountid) != picked[1]:
            loc.notes.append(
                f"你指定的是 accountid={accountid}, 但磁盘上只有一个账号目录 "
                f"({picked[0].name} -> {picked[1]}) —— 按磁盘证据优先选用它"
            )
    elif candidates and wanted is not None:
        for d, aid in candidates:
            if aid == int(wanted):
                picked = (d, aid)
                break
        if picked is None:
            loc.notes.append(
                f"磁盘上的账号目录没有一个属于 accountid={wanted}; "
                f"找到的是 {[aid for _, aid in candidates]} —— 拒绝猜哪个是你"
            )
    elif wanted is not None and create:
        target = base / accounts.accountid_to_hex_dir(int(wanted))
        loc.dir = target
        loc.accountid = int(wanted)
        loc.created = True
        loc.evidence.append(
            f"磁盘上没有账号目录, 按已验证规则(SteamID64 十六进制)生成: {target.name}"
        )
        loc.notes.append("该目录本次是新按规则确定的, 不是磁盘上实测到的 —— 请确认账号无误")
        return loc

    if picked is None:
        if not candidates:
            loc.notes.append(
                f"{base} 下没有任何账号目录 —— 本机没玩过魂二, 或存档在别处"
            )
        else:
            # 多个候选却定不下是哪个: 不猜, 但要说清楚"为什么没定下来",
            # 否则用户只看到"未定位", 不知道下一步该做什么.
            loc.notes.append(
                f"存档根下有 {len(candidates)} 个账号目录 "
                f"(accountid={[aid for _, aid in candidates]}), 但无法确定哪个是本机账号"
                f" —— 请用 --account <accountid> 指定"
            )
        return loc

    save_dir, aid = picked
    loc.dir = save_dir
    loc.accountid = aid
    loc.evidence.append(f"选中 {save_dir.name} -> accountid={aid}")

    if with_files:
        loc.files = _list_saves(save_dir, include_all=include_all)
        loc.evidence.append(f"目录内 {len(loc.files)} 个文件计入 (include_all={include_all})")

    # 核心存档缺失是很强的信号: 目录在但里面没档, 多半是装过没玩过 / 刚被清过.
    core_hit = [f for f in loc.files if f.rel.lower().endswith((".sl2", ".co2"))]
    if with_files and not core_hit:
        loc.notes.append("账号目录存在, 但里面没有 .sl2 / .co2 存档 —— 导出会得到空包")

    return loc


# ---------------------------------------------------------------- 导出


@dataclass
class ExportResult:
    ok: bool = False
    pack_dir: str = ""
    manifest_path: str = ""
    file_count: int = 0
    total_size: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pack_dir": self.pack_dir,
            "manifest_path": self.manifest_path,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "error": self.error,
            "notes": list(self.notes),
        }


def _note_text(loc: SaveLocation, manifest: dict[str, Any]) -> str:
    lines = [
        f"魂二存档包 —— {PROFILE.name}",
        "",
        f"导出时间 : {manifest['exported_at']}",
        f"来源机器 : {manifest['host']}",
        f"账号     : accountid={manifest['accountid']}  (SteamID64={manifest['steamid64']})",
        f"文件     : {manifest['file_count']} 个, 共 {manifest['total_size']:,} 字节",
        "",
        "里面是什么:",
        "  savepack.json   清单 (每个文件的 sha256, 导入时逐字节核对; 别改它)",
        "  files/          存档本体",
        "",
        "怎么用:",
        "  1. 把这个文件夹整个放到一个能跨机同步的地方 (网盘同步目录 / U 盘)",
        "  2. 在另一台机器上用工具执行: 从文件夹导入",
        "  3. 导入前会自动备份那台机器上的现有存档",
        "",
        "注意:",
        "  * 魂二没有 Steam 云存档, 这个包就是你的\"云\"",
        "  * 只适用于同一 Steam 账号; 换账号导入会被工具拒绝",
        "  * 导入时请务必先退出游戏 (工具也会检查)",
    ]
    if loc.notes:
        lines += ["", "本次导出备注:"] + [f"  - {n}" for n in loc.notes]
    return "\n".join(lines) + "\n"


def export_pack(
    loc: SaveLocation,
    dest: str | Path,
    *,
    include_all: bool = False,
    label: str = "",
    rehash: bool = True,
) -> ExportResult:
    """把定位到的存档导出成一个带清单的包 (`dest` 目录).

    包结构::

        <dest>/
            savepack.json    清单 (最后写 —— 它的存在即"导出完整"的标志)
            files/           存档本体
            先读我.txt

    清单**最后落盘**, 读取端也只认"清单 + 逐文件哈希全对". 导出中途失败会留下一个
    没有清单的半成品, 读取端会明确报错, 而不是把半包当整包用.

    `dest` 可以是同步盘目录或 U 盘 —— 本函数不关心它连着哪儿.
    """
    res = ExportResult()
    if loc.dir is None or not loc.dir.is_dir():
        res.error = "存档目录未定位 (先跑 locate / status)"
        return res

    files = loc.files
    if rehash or not files:
        files = _list_saves(loc.dir, include_all=include_all)
    if not files:
        res.error = f"存档目录里没有可导出的文件: {loc.dir}"
        return res

    dest = Path(dest)
    files_dir = dest / "files"
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.error = f"无法创建包目录 {files_dir}: {exc}"
        return res

    copied: list[dict[str, Any]] = []
    total = 0
    for f in files:
        src = loc.dir / f.rel
        if not src.is_file():
            res.error = f"导出途中源文件消失: {src}"
            return res
        try:
            shutil.copyfile(src, files_dir / f.rel)
            # 复制后按源文件的 mtime 对齐 —— 不用 copy2/copystat (它们会连属性一起带,
            # 与"显式控制时间戳"的项目纪律冲突, 且跨盘/跨文件系统行为不一致).
            os.utime(files_dir / f.rel, (f.mtime, f.mtime))
        except OSError as exc:
            res.error = f"复制失败 {src}: {exc}"
            return res
        copied.append(f.to_dict())
        total += f.size

    manifest: dict[str, Any] = {
        "schema": PACK_SCHEMA,
        "appid": PROFILE.appid,
        "game": PROFILE.name,
        "steam_cloud": PROFILE.steam_cloud,
        "transport": "folder",
        "accountid": loc.accountid,
        "steamid64": loc.steamid64,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": socket.gethostname(),
        "source_dir": str(loc.dir),
        "include_all": bool(include_all),
        "label": label,
        "file_count": len(copied),
        "total_size": total,
        "files": copied,
    }

    # 清单最后写: 半成品没有清单, 读取端就不会误认.
    try:
        (dest / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (dest / NOTE_NAME).write_text(_note_text(loc, manifest), encoding="utf-8")
    except OSError as exc:
        res.error = f"写清单失败: {exc}"
        return res

    res.ok = True
    res.pack_dir = str(dest)
    res.manifest_path = str(dest / MANIFEST_NAME)
    res.file_count = len(copied)
    res.total_size = total
    res.notes = list(loc.notes)
    return res


# ---------------------------------------------------------------- 读取 / 校验


def read_pack(pack_dir: str | Path) -> dict[str, Any]:
    """读包清单. 清单缺失或 schema 不认就抛 `ValueError` (调用方转成友好提示)."""
    pack_dir = Path(pack_dir)
    path = pack_dir / MANIFEST_NAME
    if not path.is_file():
        # 兼容"直接把 files/ 里的东西拷过来"的用法: 明确说清缺什么.
        raise ValueError(
            f"这不是一个完整的存档包: 找不到 {MANIFEST_NAME} ({pack_dir}). "
            f"多半是导出没跑完, 或把 files/ 里的文件单独拷了出来."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"清单读不出来 ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"清单格式不对 ({path}): 顶层不是对象")
    schema = str(data.get("schema", ""))
    if not schema.startswith("scb-savepack/"):
        raise ValueError(f"清单 schema 不认识: {schema!r} (期望 scb-savepack/…)")
    if str(data.get("appid", "")) != PROFILE.appid:
        raise ValueError(
            f"这个包是 appid={data.get('appid')!r} 的, 不是魂二 ({PROFILE.appid})"
        )
    return data


def verify_pack(pack_dir: str | Path) -> tuple[bool, list[str]]:
    """逐文件核对包内的 sha256. 返回 `(是否全对, 问题列表)`."""
    pack_dir = Path(pack_dir)
    try:
        manifest = read_pack(pack_dir)
    except ValueError as exc:
        return False, [str(exc)]

    problems: list[str] = []
    files_dir = pack_dir / "files"
    entries = manifest.get("files") or []
    if not entries:
        problems.append("清单里没有任何文件 —— 空包")
    for raw in entries:
        f = SaveFile.from_dict(raw if isinstance(raw, dict) else {})
        p = files_dir / f.rel
        if not p.is_file():
            problems.append(f"缺少文件: {f.rel}")
            continue
        actual_size = p.stat().st_size
        if f.size and actual_size != f.size:
            problems.append(f"大小不符: {f.rel} (清单 {f.size}, 实际 {actual_size})")
        if f.sha256:
            actual = sha256_of(p)
            if actual != f.sha256:
                problems.append(f"内容不符(sha256): {f.rel}")
    return (not problems), problems


# ---------------------------------------------------------------- 导入


@dataclass
class ImportResult:
    ok: bool = False
    dry_run: bool = True
    target_dir: str = ""
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    backup_dir: str = ""
    error: str = ""
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "target_dir": self.target_dir,
            "written": list(self.written),
            "skipped": list(self.skipped),
            "backup_dir": self.backup_dir,
            "error": self.error,
            "blockers": list(self.blockers),
            "notes": list(self.notes),
        }


def _backup_dir_now(target: Path, backup_root: str | Path) -> Path:
    """把目标目录**现状**整体复制一份到 `<backup_root>/<时间戳>/`. 返回副本路径."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(backup_root) / stamp
    n = 1
    while dest.exists():
        dest = Path(backup_root) / f"{stamp}-{n}"
        n += 1
    dest.mkdir(parents=True, exist_ok=False)
    if target.is_dir():
        for p in sorted(target.iterdir()):
            try:
                if p.is_file():
                    shutil.copyfile(p, dest / p.name)
            except OSError as exc:
                raise OSError(f"备份 {p} 失败: {exc}") from exc
    return dest


def _write_atomic(src: Path, dst: Path, mtime: float) -> None:
    """临时文件 + `os.replace`, 再显式刷 mtime."""
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".scbtmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    if mtime:
        os.utime(dst, (mtime, mtime))


def import_pack(
    pack_dir: str | Path,
    target: SaveLocation,
    *,
    backup_root: str | Path | None = None,
    dry_run: bool = False,
    when: str | None = None,
) -> ImportResult:
    """把包里的存档导入到 `target` 指向的本机存档目录.

    顺序固定, 任一步不过就停:

    1. 读清单 + 逐文件 sha256 核对 (不通过直接拒绝, 一个字都不写);
    2. **账号核对** —— 清单里的 accountid 必须与目标目录反查出来的一致;
    3. **游戏进程检查** —— 运行中禁止改档 (`None` = 判不出来, 记一条 note 但不拦);
    4. **强制备份**目标目录现状, 备份失败即中止;
    5. 逐文件写入 (临时文件 + `os.replace` + 刷 mtime), 写完复校 sha256.

    `dry_run=True` 时只走到第 4 步之前, 打印将要做什么.
    """
    res = ImportResult(dry_run=dry_run)
    pack_dir = Path(pack_dir)

    # --- 1) 清单 + 哈希 ---
    ok, problems = verify_pack(pack_dir)
    if not ok:
        res.error = "包校验未通过, 拒绝导入"
        res.blockers = problems
        return res

    manifest = read_pack(pack_dir)
    entries = [SaveFile.from_dict(e) for e in manifest.get("files") or []]

    # --- 2) 账号 ---
    pack_account = manifest.get("accountid")
    if pack_account is None:
        res.notes.append("包清单里没有 accountid (老版本导出的包?) —— 无法核对账号归属")
    if target.dir is None:
        res.error = "目标存档目录未定位"
        return res
    if target.accountid is not None and pack_account is not None:
        if int(pack_account) != int(target.accountid):
            res.error = (
                f"跨账号: 包属于 accountid={pack_account}, 本机目标目录属于 "
                f"accountid={target.accountid} —— 拒绝导入"
            )
            res.blockers.append("CROSS_ACCOUNT")
            return res

    # --- 3) 游戏进程 ---
    running = game_running()
    if running is True:
        res.error = f"{PROFILE.exe_names[0]} 正在运行 —— 退出游戏后再导入"
        res.blockers.append("GAME_RUNNING")
        return res
    if running is None:
        res.notes.append("无法判断游戏是否在运行 (tasklist 不可用) —— 请自行确认已退出游戏")

    res.target_dir = str(target.dir)
    if dry_run:
        res.ok = True
        res.written = [f.rel for f in entries]
        res.notes.append("dry-run: 未写任何文件")
        return res

    # --- 4) 强制备份 ---
    if target.dir.is_dir() and any(p.is_file() for p in target.dir.iterdir()):
        root = backup_root or (target.dir.parent / "save-backup")
        try:
            dest = _backup_dir_now(target.dir, root)
        except OSError as exc:
            res.error = f"备份失败, 已中止 (一个字都没写): {exc}"
            res.blockers.append("BACKUP_FAILED")
            return res
        res.backup_dir = str(dest)
    else:
        res.notes.append("目标目录本来就没有文件, 无需备份")

    # --- 5) 写入 ---
    try:
        target.dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.error = f"无法创建目标目录 {target.dir}: {exc}"
        return res

    files_dir = pack_dir / "files"
    for f in entries:
        src = files_dir / f.rel
        dst = target.dir / f.rel
        # 只往存档目录里写"裸文件名", 防目录穿越 (清单是外部输入, 不能信)
        if Path(f.rel).name != f.rel or "/" in f.rel or "\\" in f.rel:
            res.blockers.append(f"清单里的文件名含路径分隔符, 拒绝: {f.rel!r}")
            res.error = "清单里有可疑文件名, 已中止"
            return res
        try:
            _write_atomic(src, dst, f.mtime)
        except OSError as exc:
            res.error = f"写入失败 {dst}: {exc}"
            return res
        if sha256_of(dst) != f.sha256:
            res.error = f"写入后校验不符: {dst} —— 目标盘可能有问题"
            return res
        res.written.append(f.rel)

    res.ok = True
    return res
