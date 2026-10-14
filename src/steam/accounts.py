"""Steam 账号发现与 SteamID 换算.

云存路径的账号标识有两套写法, 必须同时支持 (计划 v3 证据 4):

  * `userdata\\<accountid>\\`            -> accountid, **十进制**
  * 游戏存档目录名 (DS3)                 -> SteamID64 的 **十六进制**, 16 位小写
  * 游戏存档目录名 (艾尔登法环)           -> SteamID64 的 **十进制**

换算关系:
    SteamID64 = 76561197960265728 + accountid
    accountid = SteamID64 - 76561197960265728
    hex 目录名 = f"{SteamID64:016x}"

数据来源:
  * 当前账号: 注册表 HKCU\\Software\\Valve\\Steam\\ActiveProcess\\ActiveUser (DWORD)
  * 全部账号: <root>\\config\\loginusers.vdf
  * 兜底: `userdata\\<accountid>\\<appid>\\remotecache.vdf` 唯一命中 (注册表为 0 时)

⚠ **`ActiveUser` 常常是 0** (未勾选"记住登录" / 无人在登录页 / 只读注册表):
实测本机 Steam 正常运行、账号明明在线, 该值仍为 0 —— 只看注册表会把
"当前账号"判成未知, 进而读不到 `remotecache.vdf` (云端副本/ChangeNumber 全丢,
并让跨账号红线静默失效). 故必须带本地兜底.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import vdf

__all__ = [
    "STEAM_ID64_BASE",
    "steamid64_to_accountid",
    "accountid_to_steamid64",
    "accountid_to_hex_dir",
    "steamid64_to_hex_dir",
    "hex_dir_to_accountid",
    "accountid_from_save_dir",
    "find_accountid_for_app",
    "Account",
    "get_active_accountid",
    "load_login_users",
]

# Steam 个人账户 (Individual) 的 Universe/Type 前缀常量
STEAM_ID64_BASE = 76561197960265728


def steamid64_to_accountid(steamid64: int) -> int:
    return int(steamid64) - STEAM_ID64_BASE


def accountid_to_steamid64(accountid: int) -> int:
    return int(accountid) + STEAM_ID64_BASE


def accountid_to_hex_dir(accountid: int) -> str:
    """DS3 风格的存档目录名: SteamID64 的 16 位小写十六进制."""
    return f"{accountid_to_steamid64(accountid):016x}"


def steamid64_to_hex_dir(steamid64: int) -> str:
    return f"{int(steamid64):016x}"


def hex_dir_to_accountid(name: str) -> int | None:
    """DS3 风格十六进制目录名 -> accountid (反查). 非法/越界返回 None.

    `01100001499602d2` -> `0x01100001499602d2` -> SteamID64 -> accountid.
    """
    s = str(name).strip().lower()
    if not s or len(s) > 16 or any(c not in "0123456789abcdef" for c in s):
        return None
    try:
        steamid64 = int(s, 16)
    except ValueError:
        return None
    aid = steamid64_to_accountid(steamid64)
    return aid if aid > 0 else None


def accountid_from_save_dir(path: Path | str) -> int | None:
    """从存档路径里找出 16 位 hex 目录段并反查 accountid.

    `%APPDATA%\\DarkSoulsIII\\01100001499602d2\\DS30000.sl2` -> `01100001499602d2`.
    **只认恰好 16 位的纯十六进制目录段**, 避免把随机目录名当成账号.
    """
    for part in reversed(Path(path).parts):
        s = part.strip().lower()
        if len(s) == 16 and all(c in "0123456789abcdef" for c in s):
            aid = hex_dir_to_accountid(s)
            if aid is not None:
                return aid
    return None


def find_accountid_for_app(steam_root: Path | str, appid: str | int) -> int | None:
    """注册表拿不到当前账号时的本地兜底: 按 `userdata/<id>/<appid>/remotecache.vdf` 反查.

    同一台机器上常被多个账号共用 (实测本机 `userdata\\` 下 150+ 个目录),
    所以**只在唯一命中时**返回; 多命中一律放弃 —— 宁可不判, 不许猜错.
    """
    userdata = Path(steam_root) / "userdata"
    if not userdata.is_dir():
        return None
    try:
        entries = list(userdata.iterdir())
    except OSError:
        return None
    hits: list[int] = []
    for d in entries:
        if not d.name.isdigit():
            continue
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        if (d / str(appid) / "remotecache.vdf").is_file():
            aid = int(d.name)
            if aid not in hits:
                hits.append(aid)
    return hits[0] if len(hits) == 1 else None


@dataclass
class Account:
    """一个 Steam 账号."""

    steamid64: int
    account_name: str = ""
    persona_name: str = ""
    most_recent: bool = False
    timestamp: int = 0
    source: str = "loginusers.vdf"

    @property
    def account_id(self) -> int:
        return steamid64_to_accountid(self.steamid64)

    @property
    def account_id_hex_dir(self) -> str:
        """DS3 / FromSoftware 系使用的十六进制目录名."""
        return steamid64_to_hex_dir(self.steamid64)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steamid64": str(self.steamid64),
            "account_id": self.account_id,
            "account_id_hex_dir": self.account_id_hex_dir,
            "account_name": self.account_name,
            "persona_name": self.persona_name,
            "most_recent": self.most_recent,
            "timestamp": self.timestamp,
        }


_ENV_ACTIVE_ACCOUNT = "SCB_ACTIVE_ACCOUNT"


def get_active_accountid() -> int | None:
    """读取"当前登录账号"的 accountid (注册表 DWORD). 失败返回 None.

    注意: ActiveUser 为 0 表示无人登录 / 未记住登录.

    自动化测试钩子 `SCB_ACTIVE_ACCOUNT` (`<非负整数>`): 注册表是**真机**的,
    隔离环境（假 steam 根）没法重定向 —— 真机上只要有人登录过 Steam, 这个值就
    非 0, 会把假环境的账号顶掉（假环境造的是虚构账号, 真机账号自然找不到对应的
    `userdata/<id>/<appid>/remotecache.vdf` ⇒ 云端副本/CN 全丢, 断言全错).
    故测试必须能把这个信号钉死: `0` 强制判为"未知", 正整数强制判为"该账号".
    """
    raw = os.environ.get(_ENV_ACTIVE_ACCOUNT, "").strip()
    if raw:
        try:
            pinned = int(raw)
        except ValueError:
            raise ValueError(
                f"{_ENV_ACTIVE_ACCOUNT} 必须是非负整数, 收到 {raw!r}"
            )
        return pinned if pinned > 0 else None

    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "ActiveUser")
    except OSError:
        return None
    try:
        val = int(value)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def load_login_users(steam_root: Path) -> list[Account]:
    """解析 config\\loginusers.vdf, 返回本机登录过的全部账号."""
    path = Path(steam_root) / "config" / "loginusers.vdf"
    if not path.is_file():
        return []
    try:
        data = vdf.parse_file(path)
    except Exception:
        return []
    block = data.get("users") or data.get("Users") or {}
    out: list[Account] = []
    if not isinstance(block, dict):
        return out
    for sid, entry in block.items():
        if not isinstance(entry, dict):
            continue
        try:
            sid_int = int(str(sid))
        except (TypeError, ValueError):
            continue
        try:
            ts = int(str(entry.get("Timestamp", "0")))
        except (TypeError, ValueError):
            ts = 0
        out.append(
            Account(
                steamid64=sid_int,
                account_name=str(entry.get("AccountName", "")),
                persona_name=str(entry.get("PersonaName", "")),
                most_recent=str(entry.get("MostRecent", "0")) == "1",
                timestamp=ts,
            )
        )
    out.sort(key=lambda a: (not a.most_recent, -a.timestamp))
    return out


def active_account(steam_root: Path) -> Account | None:
    """尽力给出"当前账号"的完整记录: 注册表 accountid + loginusers 资料."""
    aid = get_active_accountid()
    users = load_login_users(steam_root)
    if aid is None:
        return users[0] if users else None
    for acc in users:
        if acc.account_id == aid:
            return acc
    # 注册表有、loginusers 没有: 只拿到 accountid
    return Account(steamid64=accountid_to_steamid64(aid), source="registry:ActiveProcess")
