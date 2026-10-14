"""Steam 客户端侧数据发现与解析 (只读).

模块划分:
  discovery    安装位置 / 游戏库 / appmanifest
  accounts     账号发现与 SteamID 换算
  cloud_log    cloud_log.txt 解析 (云文件集真相来源)
  remotecache  remotecache.vdf 解析 + root 枚举标定
  localconfig  localconfig.vdf 云设置
"""

from __future__ import annotations

__all__ = ["discovery", "accounts", "cloud_log", "remotecache", "localconfig"]
