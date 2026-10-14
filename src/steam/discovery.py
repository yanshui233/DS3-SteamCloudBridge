"""Steam 安装位置 / 游戏库 / 应用清单的运行时发现.

设计原则 (来自计划 v3 第三节 C 表): **任何本机具体值都不允许硬编码**.
所有路径都从注册表或 Steam 自己的配置文件推导, 因此同一份代码可跑在别人电脑上.

数据来源:
  1. 注册表 HKCU\\Software\\Valve\\Steam\\SteamPath         -> 主客户端根目录
  2. 注册表 HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam\\InstallPath (兜底)
  3. <root>\\steamapps\\libraryfolders.vdf                   -> 全部库路径
  4. <lib>\\steamapps\\appmanifest_<appid>.acf               -> 游戏名 / installdir / 体积
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import vdf

__all__ = [
    "SteamInstall",
    "Library",
    "AppManifest",
    "find_installations",
    "load_libraries",
    "load_app_manifests",
    "find_autocloud_markers",
    "resolve_ac_root_dir",
    "find_account_dirs",
    "account_ident_strings",
    "name_key",
    "match_game_name",
]

# 仅作为注册表缺失时的兜底候选; 不是"预期值", 只是探测清单.
_FALLBACK_ROOTS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"C:\Steam",
    r"D:\Steam",
    r"E:\Steam",
)


@dataclass
class Library:
    """一个 Steam 游戏库 (steamapps 所在目录)."""

    path: Path
    label: str = ""
    apps: dict[str, int] = field(default_factory=dict)  # appid -> 占用字节

    @property
    def steamapps(self) -> Path:
        return self.path / "steamapps"

    @property
    def common(self) -> Path:
        return self.steamapps / "common"


@dataclass
class SteamInstall:
    """一个 Steam 客户端安装 (可能有多份, 例如 SteamCMD 或旧客户端)."""

    root: Path
    source: str
    libraries: list[Library] = field(default_factory=list)

    @property
    def steamapps(self) -> Path:
        return self.root / "steamapps"

    @property
    def userdata(self) -> Path:
        return self.root / "userdata"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def config(self) -> Path:
        return self.root / "config"

    def exists(self) -> bool:
        return self.root.is_dir()


@dataclass
class AppManifest:
    """某个游戏的安装清单信息."""

    appid: str
    name: str = ""
    installdir: str = ""
    size_on_disk: int = 0
    last_updated: int = 0
    state_flags: int = 0
    library: Path | None = None
    manifest_path: Path | None = None

    @property
    def install_dir(self) -> Path | None:
        if self.library is None or not self.installdir:
            return None
        return self.library / "steamapps" / "common" / self.installdir

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "name": self.name,
            "installdir": self.installdir,
            "install_dir": str(self.install_dir) if self.install_dir else None,
            "size_on_disk": self.size_on_disk,
            "last_updated": self.last_updated,
            "state_flags": self.state_flags,
            "library": str(self.library) if self.library else None,
        }


def _read_registry_roots() -> list[tuple[Path, str]]:
    """从注册表读取 Steam 根目录候选. 非 Windows 或读取失败时返回空列表."""
    if sys.platform != "win32":
        return []
    out: list[tuple[Path, str]] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []

    probes = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, key_path, value_name in probes:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if value:
            out.append((Path(str(value)), f"registry:{key_path}\\{value_name}"))
    return out


def find_installations() -> list[SteamInstall]:
    """发现本机所有 Steam 安装, 按"主客户端优先"排序.

    主客户端由注册表 SteamPath 判定 (它同时是 userdata / logs 的宿主).
    """
    seen: dict[str, SteamInstall] = {}
    for path, source in _read_registry_roots():
        key = os.path.normcase(str(path))
        if key not in seen and path.is_dir():
            seen[key] = SteamInstall(root=path, source=source)

    for candidate in _FALLBACK_ROOTS:
        p = Path(candidate)
        key = os.path.normcase(str(p))
        if key in seen or not p.is_dir():
            continue
        # 兜底候选必须是真 Steam 目录才收
        if (p / "steamapps").is_dir() or (p / "userdata").is_dir():
            seen[key] = SteamInstall(root=p, source="fallback-probe")

    installs = list(seen.values())
    # 有 userdata 的排前面 (那是能承载云存的"主"安装)
    installs.sort(key=lambda s: (not (s.root / "userdata").is_dir(), str(s.root).lower()))
    return installs


def load_libraries(install: SteamInstall) -> list[Library]:
    """解析 libraryfolders.vdf 得到该安装下的全部库. 找不到时退回自带 steamapps."""
    out: list[Library] = []
    seen: set[str] = set()

    def _add(path: Path, label: str = "", apps: dict[str, int] | None = None) -> None:
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        out.append(Library(path=path, label=label, apps=apps or {}))

    lf = install.steamapps / "libraryfolders.vdf"
    if lf.is_file():
        try:
            data = vdf.parse_file(lf)
        except Exception:
            data = {}
        block = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        if isinstance(block, dict):
            for _, entry in block.items():
                if not isinstance(entry, dict):
                    continue
                raw_path = entry.get("path") or entry.get("Path")
                if not raw_path:
                    continue
                apps: dict[str, int] = {}
                raw_apps = entry.get("apps")
                if isinstance(raw_apps, dict):
                    for aid, size in raw_apps.items():
                        try:
                            apps[str(aid)] = int(str(size))
                        except (TypeError, ValueError):
                            apps[str(aid)] = 0
                _add(Path(str(raw_path)), str(entry.get("label", "")), apps)
        # 旧版格式: "1" "D:\\Steam"
        if not out:
            for k, val in (block.items() if isinstance(block, dict) else []):
                if isinstance(val, str) and k.isdigit():
                    _add(Path(val))

    if not out and (install.steamapps).is_dir():
        _add(install.root)

    install.libraries = out
    return out


def load_app_manifests(libraries: list[Library]) -> dict[str, AppManifest]:
    """扫描全部库的 appmanifest_*.acf, 返回 appid -> AppManifest."""
    result: dict[str, AppManifest] = {}
    for lib in libraries:
        if not lib.steamapps.is_dir():
            continue
        for acf in lib.steamapps.glob("appmanifest_*.acf"):
            stem = acf.stem  # appmanifest_374320
            parts = stem.split("_", 1)
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            appid = parts[1]
            try:
                data = vdf.parse_file(acf)
            except Exception:
                continue
            state = data.get("AppState")
            if not isinstance(state, dict):
                continue
            try:
                size = int(str(state.get("SizeOnDisk", "0")))
            except (TypeError, ValueError):
                size = 0
            try:
                updated = int(str(state.get("LastUpdated", "0")))
            except (TypeError, ValueError):
                updated = 0
            try:
                flags = int(str(state.get("StateFlags", "0")))
            except (TypeError, ValueError):
                flags = 0
            result[appid] = AppManifest(
                appid=appid,
                name=str(state.get("name", "")),
                installdir=str(state.get("installdir", "")),
                size_on_disk=size,
                last_updated=updated,
                state_flags=flags,
                library=lib.path,
                manifest_path=acf,
            )
    return result


def find_autocloud_markers(library: Library, max_depth: int = 4) -> dict[str, Path]:
    """在库内查找 steam_autocloud.vdf (Auto-Cloud 声明文件), 返回 绝对路径 -> 文件.

    注意: 这只是**辅助**证据. 计划 v3 已确认 `cloud_log.txt` 的 `Watching file`
    更权威 (声明文件存在 != 真在同步). 本函数用于交叉验证, 不作为唯一依据.

    用 os.walk 而不是 rglob: 大型库 (多款 30GB+ 游戏) 下 rglob 会遍历数万文件,
    这里按 max_depth 剪枝, 只下钻到存档可能所在的层级.
    """
    out: dict[str, Path] = {}
    base = library.common
    if not base.is_dir():
        return out
    base_depth = len(base.parts)
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []  # 剪枝: 不再下钻
        if "steam_autocloud.vdf" in filenames:
            target = current / "steam_autocloud.vdf"
            out[str(target)] = target
    return out


# ---------------------------------------------------------------------------
# Auto-Cloud 根语义: `ac\` 一级目录名 -> 本地基目录
# ---------------------------------------------------------------------------

# `userdata\<id>\<appid>\ac\` 下的一级目录名是 Auto-Cloud "根" 的可读形式.
# 本表把可读名映射回本地路径. 这比 `remotecache.vdf` 的枚举数字 (root=4)
# 直观得多, 而且**声明式**存在 —— 即使 ac\ 是空壳也能读出根语义.
_AC_ROOT_ENV: dict[str, tuple[str, str]] = {
    # ac 目录名 : (环境变量, 相对该变量的子路径)
    "WinAppDataRoaming": ("APPDATA", ""),
    "WinAppDataLocal": ("LOCALAPPDATA", ""),
    "WinAppDataLocalLow": ("USERPROFILE", r"AppData\LocalLow"),
    "WinMyDocuments": ("USERPROFILE", "Documents"),
    "WinDocuments": ("USERPROFILE", "Documents"),
    "WinSavedGames": ("USERPROFILE", "Saved Games"),
    "WinProgramData": ("PROGRAMDATA", ""),
    "WinSteamUserBaseStorage": ("", ""),
}

# 无法从 ac 目录名直接还原的根 (需要游戏安装目录等额外上下文)
# 值用类型码: "install" = 游戏安装目录, "cloud" = Steam 云端 remote 目录
_AC_ROOT_SPECIAL: dict[str, str] = {
    "WinGameInstall": "install",
    "WinGameInstallDir": "install",
    "GameInstall": "install",
    "GameInstallDir": "install",
    "WinSteamCloud": "cloud",
}


def resolve_ac_root_dir(name: str, game_install_dir: Path | None = None) -> Path | None:
    """把 `ac\\` 一级目录名解析成本地基目录. 无法解析时返回 None."""
    key = str(name)
    special = _AC_ROOT_SPECIAL.get(key)
    if special is None:
        for k, v in _AC_ROOT_SPECIAL.items():
            if k.lower() == key.lower():
                special = v
                break
    if special is not None:
        if special == "install":
            return Path(game_install_dir) if game_install_dir else None
        return None
    hit = _AC_ROOT_ENV.get(key)
    if hit is None:
        # 兜底: 大小写/别名差异
        for k, v in _AC_ROOT_ENV.items():
            if k.lower() == key.lower():
                hit = v
                break
    if hit is None:
        return None
    env_name, sub = hit
    if not env_name:
        return None
    base_raw = os.environ.get(env_name)
    if not base_raw and env_name == "USERPROFILE":
        base_raw = os.path.expanduser("~")
    if not base_raw:
        return None
    base = Path(base_raw)
    return base / sub if sub else base


def find_account_dirs(
    base: Path,
    ident_strings: Iterable[str],
    max_depth: int = 3,
) -> list[Path]:
    """在 base 下查找名字等于任一账号标识的目录.

    用途: 从 `ac\\` 镜像推出的基目录出发, **不是**硬编码游戏目录名, 而是用账号
    标识 (accountid 十进制 / SteamID64 十进制 / SteamID64 十六进制) 去反查
    真正的存档目录. 因为多数 FromSoftware 系游戏的存档目录以账号标识命名:

        %APPDATA%\\DarkSoulsIII\\01100001499602d2\\        <- SteamID64 十六进制
        %APPDATA%\\EldenRing\\76561199194833618\\          <- SteamID64 十进制

    这样即便不知道游戏把存档放在哪个子目录, 也能定位到候选路径.
    """
    wanted = {s.lower() for s in ident_strings if s}
    if not wanted or not base.is_dir():
        return []
    base_depth = len(base.parts)
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(base):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        for d in list(dirnames):
            if d.lower() in wanted:
                found.append(current / d)
    return found


def name_key(text: str) -> str:
    """把名称归一化用于模糊比对: 只保留字母数字并小写.

    `DARK SOULS III` -> `darksoulsiii`
    `DarkSoulsIII`   -> `darksoulsiii`   (相等 -> 命中)
    """
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def match_game_name(
    candidate: Path,
    game_names: Iterable[str],
    min_len: int = 6,
) -> tuple[str, str]:
    """判断候选目录路径里是否有某一层"像"这款游戏的目录名.

    这是把宽泛的候选集收敛到具体游戏的关键启发式: 不依赖任何硬编码映射,
    只用 `appmanifest` 里的 `name` / `installdir` 与路径层名做归一化比对.

    返回 `(level, reason)`, level 取值:

    * `"exact"`   —— 归一化后完全相等. 最可信, 例如 `DarkSoulsIII` 对
      `DARK SOULS III`.
    * `"partial"` —— 前缀关系且长度接近. 可信度低 (罗马数字这类后缀差异
      会造成假阳性, 例如 `DarkSoulsII` 会被 `darksoulsiii` 部分命中),
      因此**仅在没有任何 exact 匹配时才应使用**.
    * `""`        —— 不匹配.
    """
    keys = {name_key(n) for n in game_names if n}
    keys = {k for k in keys if len(k) >= min_len}
    if not keys:
        return "", ""
    for part in candidate.parts:
        p = name_key(part)
        if not p:
            continue
        for k in keys:
            if p == k:
                return "exact", f"目录名与游戏名完全匹配 ({part})"
    for part in candidate.parts:
        p = name_key(part)
        if not p or len(p) < min_len:
            continue
        for k in keys:
            if p.startswith(k) or k.startswith(p):
                ratio = min(len(p), len(k)) / max(len(p), len(k))
                if ratio >= 0.85:
                    return "partial", f"目录名与游戏名部分匹配 ({part})"
    return "", ""


def account_ident_strings(account_id: int) -> list[str]:
    """一个账号的三种目录命名写法."""
    from .accounts import accountid_to_hex_dir, accountid_to_steamid64

    sid = accountid_to_steamid64(account_id)
    return [str(account_id), str(sid), accountid_to_hex_dir(account_id)]
