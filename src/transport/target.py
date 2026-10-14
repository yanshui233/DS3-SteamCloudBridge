"""搬运落点解析: "这台机器上, Steam 到底会同步哪个文件".

## 为什么不能自己拼路径

`docs/plan-v3.md` 证据 4 实测: 存档目录里的**账号段写法因游戏而异** ——

    DS3            %APPDATA%\\DarkSoulsIII\\01100001499602d2\\DS30000.sl2   <- SteamID64 十六进制
    艾尔登法环      %APPDATA%\\EldenRing\\76561199194833618\\ER0000.sl2     <- SteamID64 十进制

所以任何"我用 accountid 拼一个目录名"的做法, 都会在第二个游戏上翻车. 更糟的是
`docs/plan-v3.md` 记录的 D3 事故: 把**游戏安装目录**误当存档目录写入. 因此本模块
只做一件事 —— 从机器自身的证据反推落点, 按可信度排三级:

1. `cloud_log.txt` 的 `Watching file <绝对路径>`
   —— Steam 自己写下的绝对路径, 最硬 (plan-v3 证据 1).
2. `remotecache.vdf` 的条目 (`相对路径` + `root` 枚举)
   —— 用 root 语义展开成绝对路径, 再**用文件存在性校验**; 与 (1) 一致则升级为 `confirmed`.
3. 两者都没有 -> `unresolved`. **拒绝执行**, 而不是猜一个目录名.

## root 枚举

`root` 是枚举值而非路径, 官方未文档化. 本项目沿用 `remotecache.ROOT_HINTS`
的语义表给出候选基目录, 但**结论以磁盘上文件是否存在为准** —— 与
`remotecache.calibrate_roots` 的哲学一致 (不信文档, 靠实机标定).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import vdf
from ..steam import cloud_log, discovery, remotecache

__all__ = [
    "RootBase",
    "SaveTarget",
    "TargetResolution",
    "normalize_rel",
    "root_bases",
    "same_path",
    "save_root_of",
    "autocloud_accountid",
    "resolve_targets",
]

# root 枚举 -> (环境变量名, 相对子路径). 空子路径表示环境变量本身就是基目录.
# 与 remotecache.ROOT_HINTS 对齐, 但这里给出的是**可展开成本机路径**的形式.
_ROOT_ENV: dict[str, tuple[str, str]] = {
    "2": ("USERPROFILE", "Documents"),
    "3": ("LOCALAPPDATA", ""),
    "4": ("APPDATA", ""),
    "5": ("USERPROFILE", "AppData/LocalLow"),
    "9": ("USERPROFILE", "Saved Games"),
    "10": ("PROGRAMDATA", ""),
    "11": ("USERPROFILE", "Documents/Steam Cloud"),
    "12": ("USERPROFILE", "AppData/LocalLow"),
}

_ROOT_LABELS: dict[str, str] = {
    "0": "<Steam>/userdata/<accountid>/<appid>/remote",
    "1": "游戏安装目录",
    "2": "%USERPROFILE%/Documents",
    "3": "%LOCALAPPDATA%",
    "4": "%APPDATA%",
    "5": "%USERPROFILE%/AppData/LocalLow",
    "9": "%USERPROFILE%/Saved Games",
    "10": "%PROGRAMDATA%",
    "11": "%USERPROFILE%/Documents/Steam Cloud",
    "12": "%USERPROFILE%/AppData/LocalLow",
}

# 存档根向上查找 Auto-Cloud 声明文件的最大层数.
_MARKER_MAX_UP = 6


def normalize_rel(rel: str) -> Path:
    """把 `remotecache` 的相对路径归一成本地相对 `Path`.

    `remotecache.vdf` 的键用 `/` 或 `\\` 分隔 (`DarkSoulsIII/01100001499602d2/DS30000.sl2`),
    Windows 上 FileSystem 两种都认, 但为了比较与拼接统一按 `/` 切开.

    含 `..` 上跳的路径一律拒绝 (防目录穿越).
    """
    cleaned = str(rel).replace("\\", "/")
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError(f"相对路径含上跳, 拒绝: {rel!r}")
    return Path(*parts) if parts else Path()


def same_path(a: str | Path, b: str | Path) -> bool:
    """两个路径是否指向同一位置 (大小写与分隔符不敏感)."""
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b))
        )
    except (TypeError, ValueError):
        return False


@dataclass
class RootBase:
    """一个 `root` 枚举值在本机对应的基目录."""

    enum: str
    label: str
    path: Path | None

    def to_dict(self) -> dict[str, Any]:
        return {"enum": self.enum, "label": self.label, "path": str(self.path) if self.path else None}


def root_bases(
    install: discovery.SteamInstall | None,
    appid: str,
    accountid: int | None,
    game_install_dir: Path | None = None,
) -> dict[str, RootBase]:
    """本机上每个 `root` 枚举值对应的候选基目录.

    `root=0` (云端 remote 目录) 与 `root=1` (游戏安装目录) 是**按 appid 变化**的,
    必须由调用方传入才能解析; 其余靠环境变量展开. 环境变量读 `os.environ` 而不是
    `expanduser`, 这样沙箱重定向 (`make_fake_steam.py` 会改 `APPDATA` 等) 同样生效.
    """
    out: dict[str, RootBase] = {}

    remote: Path | None = None
    if install is not None and accountid is not None:
        remote = install.userdata / str(accountid) / str(appid) / "remote"
    out["0"] = RootBase("0", _ROOT_LABELS["0"], remote)

    out["1"] = RootBase(
        "1", _ROOT_LABELS["1"], Path(game_install_dir) if game_install_dir else None
    )

    for enum, (env_name, sub) in _ROOT_ENV.items():
        raw = os.environ.get(env_name)
        if not raw and env_name == "USERPROFILE":
            raw = os.path.expanduser("~")
        if not raw:
            out[enum] = RootBase(enum, _ROOT_LABELS[enum], None)
            continue
        base = Path(raw)
        out[enum] = RootBase(enum, _ROOT_LABELS[enum], base / sub if sub else base)

    return out


@dataclass
class SaveTarget:
    """一个候选落点 (Steam 可能同步的某一个文件)."""

    appid: str
    rel_path: str
    """`remotecache` 里的相对路径, 或从 Watching file 反推出的相对段."""
    abs_path: Path | None
    root_enum: str
    root_label: str
    confidence: str
    """`confirmed-by-cloud-log` > `confirmed` (磁盘可证) > `inferred` (靠语义展开) > `unresolved`."""
    evidence: list[str] = field(default_factory=list)

    # 云端登记信息 (来自 remotecache; 缺失时为默认值)
    remote_sha: str = ""
    remote_size: int = 0
    remotetime: int = 0
    syncstate: int = 0

    # 本地磁盘实况
    exists: bool = False
    size: int = 0
    mtime: float = 0.0

    @property
    def usable(self) -> bool:
        """能否作为搬运目标 —— 必须解析出了绝对路径, 且不是靠猜."""
        return self.abs_path is not None and self.confidence != "unresolved"

    @property
    def has_remote_copy(self) -> bool:
        """云端是否真有副本 (`remotetime > 0`)."""
        return self.remotetime > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "rel_path": self.rel_path,
            "abs_path": str(self.abs_path) if self.abs_path else None,
            "root_enum": self.root_enum,
            "root_label": self.root_label,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "remote_sha": self.remote_sha,
            "remote_size": self.remote_size,
            "remotetime": self.remotetime,
            "syncstate": self.syncstate,
            "exists": self.exists,
            "size": self.size,
            "mtime": self.mtime,
        }


@dataclass
class TargetResolution:
    """某个 appid 在本机的落点解析结果."""

    appid: str
    accountid: int | None
    targets: list[SaveTarget] = field(default_factory=list)
    watching_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # 云通道状态 (来自 cloud_log)
    cloud_state: str = "unknown"
    cloud_disabled: bool = False
    toggle_effective: bool = False
    change_number: int = 0
    disabled_hint: str = ""

    @property
    def primary(self) -> SaveTarget | None:
        """挑出最可信的落点.

        优先级: 有云日志实锤 > 磁盘可证 > 已存在 > 靠前. 同一档里优先**已存在**的
        文件 —— 因为"已存在"意味着 Steam 确实在那儿同步过.
        """
        usable = [t for t in self.targets if t.usable]
        if not usable:
            return None

        def rank(t: SaveTarget) -> tuple[int, int]:
            conf = {
                "confirmed-by-cloud-log": 0,
                "confirmed": 1,
                "inferred": 2,
            }.get(t.confidence, 3)
            return (conf, 0 if t.exists else 1)

        return sorted(usable, key=rank)[0]

    @property
    def resolved(self) -> bool:
        return self.primary is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "accountid": self.accountid,
            "resolved": self.resolved,
            "primary": self.primary.to_dict() if self.primary else None,
            "targets": [t.to_dict() for t in self.targets],
            "watching_files": list(self.watching_files),
            "cloud_state": self.cloud_state,
            "cloud_disabled": self.cloud_disabled,
            "toggle_effective": self.toggle_effective,
            "change_number": self.change_number,
            "disabled_hint": self.disabled_hint,
            "notes": list(self.notes),
        }


def save_root_of(save_path: str | Path) -> Path | None:
    """从一个存档文件向上找 Auto-Cloud 声明文件 (`steam_autocloud.vdf`) 所在目录.

    该目录就是"存档根". DS3 的形态是 `%APPDATA%\\DarkSoulsIII\\steam_autocloud.vdf`,
    存档本体在其子目录 `<账号段>\\` 下. 找不到返回 `None`.
    """
    cur = Path(save_path).parent
    for _ in range(_MARKER_MAX_UP):
        try:
            if (cur / "steam_autocloud.vdf").is_file():
                return cur
        except OSError:
            return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def autocloud_accountid(save_root: str | Path) -> int | None:
    """读 `steam_autocloud.vdf` 里的 `accountid` (存档归属账号).

    这是跨账号红线检查的依据: 目标存档根属于别的账号 -> 禁止写入.
    读不到时返回 `None` (调用方应视为"未知", 而不是"无账号").
    """
    path = Path(save_root) / "steam_autocloud.vdf"
    if not path.is_file():
        return None
    try:
        data = vdf.parse_file(path)
    except Exception:
        return None
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() == "accountid":
                    try:
                        return int(str(value))
                    except (TypeError, ValueError):
                        return None
                if isinstance(value, dict):
                    stack.append(value)
    return None


def _probe(target: SaveTarget) -> SaveTarget:
    """回填磁盘实况 (存在性 / 大小 / mtime). 只读."""
    p = target.abs_path
    if p is None:
        return target
    try:
        if p.is_file():
            st = p.stat()
            target.exists = True
            target.size = st.st_size
            target.mtime = st.st_mtime
    except OSError:
        target.exists = False
    return target


def resolve_targets(
    install: discovery.SteamInstall,
    appid: str,
    accountid: int | None,
    *,
    cloud: cloud_log.CloudLog | None = None,
    game_install_dir: Path | None = None,
) -> TargetResolution:
    """解析某个 appid 在本机的云存落点.

    只读, 不修改任何文件. `cloud` 可传入已解析的 `CloudLog` 以避免重复读日志.
    """
    appid = str(appid)
    res = TargetResolution(appid=appid, accountid=accountid)

    # ---- 1) 云日志 (权威: 绝对路径 + 云通道状态) ----
    if cloud is None:
        log_path = install.logs / "cloud_log.txt"
        cloud = cloud_log.parse_cloud_log(log_path) if log_path.is_file() else cloud_log.CloudLog()
    entry = cloud.entries.get(appid)
    if entry is not None:
        res.watching_files = list(entry.watching_files)
        res.cloud_state = entry.cloud_state
        res.cloud_disabled = entry.cloud_state == "disabled"
        res.toggle_effective = bool(entry.toggle_effective)
        res.disabled_hint = (
            f'AutoCloud is disabled. Run "testappcloudpaths {appid}" in the console to enable it'
        )
    else:
        res.notes.append(f"cloud_log 里没有 AppID {appid} 的任何记录")

    # ---- 2) remotecache (权威: 云端登记的相对路径 + root + sha) ----
    cache: remotecache.RemoteCache | None = None
    if accountid is None:
        res.notes.append("账号未确定, 无法定位 userdata 下的 remotecache.vdf")
    else:
        rc_path = install.userdata / str(accountid) / appid / "remotecache.vdf"
        if rc_path.is_file():
            cache = remotecache.parse_remotecache(rc_path, appid)
            res.change_number = cache.change_number
        else:
            res.notes.append(f"未找到 remotecache.vdf: {rc_path}")

    bases = root_bases(install, appid, accountid, game_install_dir)

    if cache is not None:
        for e in cache.entries:
            rb = bases.get(e.root)
            evidence = [f"remotecache.vdf: root={e.root}"]
            abs_path: Path | None = None
            confidence = "unresolved"

            if rb is None or rb.path is None:
                evidence.append(f"root {e.root} 无法展开成本机路径")
            else:
                try:
                    abs_path = rb.path / normalize_rel(e.name)
                except ValueError as exc:
                    evidence.append(str(exc))
                    abs_path = None
                else:
                    evidence.append(f"root {e.root} -> {rb.label}")
                    confidence = "inferred"

            target = SaveTarget(
                appid=appid,
                rel_path=str(e.name),
                abs_path=abs_path,
                root_enum=str(e.root),
                root_label=rb.label if rb else "(未知)",
                confidence=confidence,
                evidence=evidence,
                remote_sha=str(e.sha),
                remote_size=int(e.size),
                remotetime=int(e.remotetime),
                syncstate=int(e.syncstate),
            )
            _probe(target)

            if abs_path is not None:
                if target.exists:
                    target.confidence = "confirmed"
                    target.evidence.append("文件在磁盘上存在")
                for w in res.watching_files:
                    if same_path(w, abs_path):
                        target.confidence = "confirmed-by-cloud-log"
                        target.evidence.append("cloud_log 的 Watching file 指向同一路径")
                        break

            res.targets.append(target)

    # ---- 3) 用 cloud_log 的绝对路径补齐解析失败的条目 ----
    # 注意**不要**把 watching_files 照单全收: 那些行会累积历史 —— 实测中已经
    # 清理掉的诱饵 (`scb-bait-*.sl2`) 仍留在日志里, 全收会引入幽灵候选.
    # remotecache 才是"云端已登记文件"的权威快照, 日志只作补充.
    for t in [x for x in res.targets if x.abs_path is None]:
        base = Path(str(t.rel_path).replace("\\", "/")).name.lower()
        for w in res.watching_files:
            if Path(str(w)).name.lower() == base:
                t.abs_path = Path(w)
                t.confidence = "confirmed-by-cloud-log"
                t.root_label = "(来自 cloud_log Watching file)"
                t.evidence.append("remotecache 的 root 无法展开, 改用 cloud_log 的绝对路径")
                _probe(t)
                break

    # ---- 4) 完全没有登记时才整体兜底 ----
    if not res.targets and res.watching_files:
        for w in res.watching_files:
            target = SaveTarget(
                appid=appid,
                rel_path=Path(str(w).replace("\\", "/")).name,
                abs_path=Path(w),
                root_enum="?",
                root_label="(来自 cloud_log Watching file)",
                confidence="confirmed-by-cloud-log",
                evidence=["cloud_log 的 Watching file (绝对路径); remotecache 无登记"],
            )
            _probe(target)
            res.targets.append(target)
        res.notes.append(
            "remotecache 无条目, 落点全部来自 cloud_log 的 Watching 行 "
            "(该行会累积历史, 请注意甄别)"
        )

    # ---- 5) 兜底说明 ----
    if not res.targets:
        res.notes.append(
            "本机没有任何该游戏的云存证据 (既无 remotecache 条目, 也无 Watching file); "
            "拒绝自行拼目录名 —— 请先在该机启动一次游戏让 Steam 生成落点记录"
        )

    return res
