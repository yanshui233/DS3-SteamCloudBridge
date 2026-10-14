"""`userdata\\<accountid>\\config\\localconfig.vdf` 里的云存设置解析.

Steam 为每个 appid 在 localconfig.vdf 里记录了一段 cloud 配置, 路径为:

    UserLocalConfigStore
      Software
        Valve
          Steam
            apps
              <appid>
                cloud
                {
                    "last_sync_state"  "synchronized"
                    ...
                }
              <appid>
                UFS               <- Auto-Cloud 相关
                { ... }

注意 (计划 v3 已更正): `last_sync_state = synchronized` **只是状态字段**,
不代表真的发生过文件传输. 判定"是否真的同步过"要看 `cloud_log.txt` 的 change number.
本模块只做如实提取, 不做结论.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import vdf

__all__ = ["AppCloudSetting", "load_local_config"]


@dataclass
class AppCloudSetting:
    """localconfig.vdf 中单个 appid 的云相关设置."""

    appid: str
    last_sync_state: str = ""
    cloud_keys: dict[str, str] = field(default_factory=dict)
    has_ufs_block: bool = False
    ufs_keys: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "last_sync_state": self.last_sync_state,
            "cloud_keys": self.cloud_keys,
            "has_ufs_block": self.has_ufs_block,
            "ufs_keys": self.ufs_keys,
        }


def _flatten(node: Any, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, out) if isinstance(v, dict) else out.__setitem__(str(k), str(v))
    elif isinstance(node, list):  # pragma: no cover - VDF 不产生 list
        for item in node:
            _flatten(item, out)


def load_local_config(path: str | Path) -> dict[str, AppCloudSetting]:
    """解析 localconfig.vdf, 返回 appid -> AppCloudSetting (只含有云设置的 appid)."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = vdf.parse_file(p)
    except Exception:
        return {}

    store = data.get("UserLocalConfigStore") or data.get("userlocalconfigstore") or {}
    if not isinstance(store, dict):
        return {}
    software = store.get("Software") or store.get("software") or {}
    valve = software.get("Valve") if isinstance(software, dict) else None
    steam = valve.get("Steam") if isinstance(valve, dict) else None
    apps = steam.get("apps") if isinstance(steam, dict) else None
    if not isinstance(apps, dict):
        return {}

    result: dict[str, AppCloudSetting] = {}
    for appid, node in apps.items():
        if not isinstance(node, dict):
            continue
        setting = AppCloudSetting(appid=str(appid))
        touched = False

        cloud = node.get("cloud")
        if isinstance(cloud, dict):
            touched = True
            setting.last_sync_state = str(cloud.get("last_sync_state", ""))
            flat: dict[str, str] = {}
            for k, v in cloud.items():
                if isinstance(v, dict):
                    _flatten(v, flat)
                else:
                    flat[str(k)] = str(v)
            setting.cloud_keys = flat

        ufs = node.get("UFS")
        if isinstance(ufs, dict):
            touched = True
            setting.has_ufs_block = True
            flat_ufs: dict[str, str] = {}
            for k, v in ufs.items():
                if isinstance(v, dict):
                    _flatten(v, flat_ufs)
                else:
                    flat_ufs[str(k)] = str(v)
            setting.ufs_keys = flat_ufs

        if touched:
            result[str(appid)] = setting
    return result
