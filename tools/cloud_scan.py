#!/usr/bin/env python3
"""cloud_scan —— 只读扫描本机 Steam 云存全貌, 输出 Markdown + JSON 报告.

对应计划 v3 的 **M1 / 通用内核**. 本工具:

  * **只读**: 不写、不改、不删任何 Steam / 游戏文件, 不启动游戏.
  * 运行时发现一切: Steam 安装位置、游戏库、账号、云文件集, 全部靠探测而非硬编码.
  * 可移植: 同一份代码换机器直接跑.

用法:

    python tools/cloud_scan.py                       # 全自动: 主安装 + 当前账号
    python tools/cloud_scan.py --all-accounts        # 扫全部账号的 userdata
    python tools/cloud_scan.py --steam-root "D:\\Steam"
    python tools/cloud_scan.py --accountid 1234567890
    python tools/cloud_scan.py --out-dir reports --no-markers
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 让脚本能直接 `python tools/cloud_scan.py` 运行而不必先安装包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import __version__ as TOOL_VERSION  # noqa: E402
from src.steam import accounts as accounts_mod  # noqa: E402
from src.steam import cloud_log as cloud_log_mod  # noqa: E402
from src.steam import discovery  # noqa: E402
from src.steam import localconfig as localconfig_mod  # noqa: E402
from src.steam import remotecache as remotecache_mod  # noqa: E402

# ---------------------------------------------------------------------------
# userdata 扫描
# ---------------------------------------------------------------------------


def scan_userdata_apps(userdata_dir: Path, accountid: str) -> dict[str, dict[str, Any]]:
    """扫描 userdata\\<accountid>\\<appid>\\ 下每个 appid 的云存目录形态.

    判定 (计划 v3 二节):
      * 有 `remote\\` 目录  -> API 型 (ISteamRemoteStorage)
      * 只有 `ac\\` 目录    -> Auto-Cloud (UFS), 且 `ac\\` 下此时多为空壳
    """
    result: dict[str, dict[str, Any]] = {}
    acc_dir = userdata_dir / accountid
    if not acc_dir.is_dir():
        return result

    for app_dir in acc_dir.iterdir():
        if not app_dir.is_dir() or not app_dir.name.isdigit():
            continue
        appid = app_dir.name
        remote_dir = app_dir / "remote"
        ac_dir = app_dir / "ac"
        rc_path = app_dir / "remotecache.vdf"

        info: dict[str, Any] = {
            "appid": appid,
            "path": str(app_dir),
            "has_remote_dir": remote_dir.is_dir(),
            "has_ac_dir": ac_dir.is_dir(),
            "has_remotecache": rc_path.is_file(),
            "remote_files": 0,
            "remote_bytes": 0,
            "ac_top_dirs": [],
            "cache": None,
        }

        if info["has_remote_dir"]:
            count = 0
            total = 0
            try:
                for p in remote_dir.rglob("*"):
                    if p.is_file():
                        count += 1
                        try:
                            total += p.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
            info["remote_files"] = count
            info["remote_bytes"] = total

        if info["has_ac_dir"]:
            try:
                info["ac_top_dirs"] = sorted(
                    d.name for d in ac_dir.iterdir() if d.is_dir()
                )
            except OSError:
                pass

        if info["has_remotecache"]:
            try:
                cache = remotecache_mod.parse_remotecache(rc_path, appid=appid)
                info["cache"] = cache.to_dict()
            except Exception:
                info["cache"] = None

        if info["has_remote_dir"] or info["has_ac_dir"] or info["has_remotecache"]:
            result[appid] = info
    return result


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def collect(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": {
            "name": "SteamCloudBridge / cloud_scan",
            "version": TOOL_VERSION,
            "mode": "read-only",
            "python": sys.version.split()[0],
            "platform": sys.platform,
        },
    }

    # --- 1. Steam 安装发现 ---
    installs = discovery.find_installations()
    if args.steam_root:
        forced = Path(args.steam_root)
        # 显式指定的路径不存在时直接失败, 不静默回退到自动发现 (否则用户会误读结果)
        if not forced.is_dir():
            data["error"] = f"--steam-root 指定的目录不存在: {forced}"
            return data
        installs = [
            i for i in installs if os.path.normcase(str(i.root)) != os.path.normcase(str(forced))
        ]
        installs.insert(0, discovery.SteamInstall(root=forced, source="--steam-root"))
    installs = [i for i in installs if i.exists()]
    if not installs:
        data["error"] = "未发现任何 Steam 安装 (注册表与常见路径均未命中)"
        return data

    main = installs[0]
    if not (main.root / "userdata").is_dir():
        for inst in installs:
            if (inst.root / "userdata").is_dir():
                main = inst
                break

    data["steam_installations"] = [
        {
            "root": str(i.root),
            "source": i.source,
            "has_userdata": (i.root / "userdata").is_dir(),
            "has_logs": (i.root / "logs").is_dir(),
            "has_config": (i.root / "config").is_dir(),
        }
        for i in installs
    ]
    data["active_steam_root"] = str(main.root)

    # --- 2. 游戏库 + 清单 ---
    libraries: list[discovery.Library] = []
    seen_lib: set[str] = set()
    for inst in installs:
        for lib in discovery.load_libraries(inst):
            key = os.path.normcase(str(lib.path))
            if key in seen_lib:
                continue
            seen_lib.add(key)
            libraries.append(lib)

    manifests = discovery.load_app_manifests(libraries)
    data["libraries"] = [
        {
            "path": str(lib.path),
            "label": lib.label,
            "has_steamapps": lib.steamapps.is_dir(),
            "app_count_hint": len(lib.apps),
        }
        for lib in libraries
    ]

    # --- 3. 账号 ---
    login_users = accounts_mod.load_login_users(main.root)
    active = accounts_mod.active_account(main.root)
    data["accounts"] = [a.to_dict() for a in login_users]
    data["active_account"] = active.to_dict() if active else None

    if args.accountid:
        target_accountids = [str(args.accountid)]
    elif args.all_accounts and (main.root / "userdata").is_dir():
        target_accountids = sorted(
            d.name for d in (main.root / "userdata").iterdir() if d.is_dir() and d.name.isdigit()
        )
    elif active is not None:
        target_accountids = [str(active.account_id)]
    else:
        target_accountids = sorted(
            d.name for d in (main.root / "userdata").iterdir() if d.is_dir() and d.name.isdigit()
        )
    data["scanned_accountids"] = target_accountids

    # --- 4. userdata 扫描 ---
    userdata_dir = main.root / "userdata"
    per_account: dict[str, dict[str, Any]] = {}
    calib_records: list[tuple[str, str, dict[str, Path]]] = []
    for aid in target_accountids:
        apps = scan_userdata_apps(userdata_dir, aid)
        per_account[aid] = apps
        for info in apps.values():
            cache = info.get("cache")
            if not cache:
                continue
            extra: dict[str, Path] = {}
            remote_dir = Path(info["path"]) / "remote"
            if remote_dir.is_dir():
                extra["<SteamRemote>"] = remote_dir
            m = manifests.get(info["appid"])
            if m and m.install_dir and m.install_dir.is_dir():
                extra["<GameInstallDir>"] = m.install_dir
            for entry in cache.get("entries", []):
                calib_records.append((str(entry["root"]), str(entry["name"]), extra))
    # 只保留真正有云存数据的账号 (共用机器上 userdata 可能有上百个历史账号目录)
    data["userdata_apps"] = {k: v for k, v in per_account.items() if v}
    data["accounts_scanned_count"] = len(target_accountids)
    data["accounts_with_data"] = sorted(k for k, v in per_account.items() if v)

    # --- 5. localconfig 云设置 ---
    local_cfg: dict[str, Any] = {}
    for aid in target_accountids:
        cfg_path = userdata_dir / aid / "config" / "localconfig.vdf"
        for appid, setting in localconfig_mod.load_local_config(cfg_path).items():
            local_cfg.setdefault(aid, {})[appid] = setting.to_dict()
    data["localconfig_cloud"] = local_cfg

    # --- 6. cloud_log 解析 ---
    log_paths: list[Path] = []
    for inst in installs:
        logs_dir = inst.root / "logs"
        if logs_dir.is_dir():
            log_paths.extend(sorted(logs_dir.glob("cloud_log*.txt")))
    log = cloud_log_mod.parse_cloud_logs(log_paths)
    data["cloud_log"] = log.to_dict()

    # --- 7. Auto-Cloud 声明文件 (辅助证据) ---
    markers: dict[str, list[str]] = {}
    if not args.no_markers:
        for lib in libraries:
            for path in discovery.find_autocloud_markers(lib, max_depth=args.marker_depth):
                appid = _appid_from_path(path, library=lib, manifests=manifests)
                markers.setdefault(appid, []).append(path)
    data["autocloud_markers"] = markers

    # --- 8. root 枚举标定 ---
    bases = remotecache_mod.candidate_bases()
    calibration = remotecache_mod.calibrate_roots(calib_records, bases=bases)
    data["root_calibration"] = {k: v.to_dict() for k, v in sorted(calibration.items())}
    data["root_calibration_bases"] = {k: str(v) for k, v in bases.items()}
    data["root_calibration_note"] = (
        "标定同时使用全局候选基目录与 per-appid 专属基目录 "
        "(<SteamRemote> = userdata\\<id>\\<appid>\\remote, <GameInstallDir> = 游戏安装目录)."
    )

    # --- 9. 合并成 app 视图 ---
    apps_view: list[dict[str, Any]] = []
    appids: set[str] = set(manifests) | set(log.entries)
    for per in per_account.values():
        appids |= set(per)
    appids |= set(markers)

    for aid in target_accountids:
        for appid in sorted(per_account.get(aid, {})):
            appids.add(appid)

    for appid in sorted(appids, key=lambda x: (len(x), x)):
        m = manifests.get(appid)
        entry = log.entries.get(appid)
        # 只用被扫账号里出现的 userdata 数据 (取第一个命中)
        ud: dict[str, Any] | None = None
        ud_account: str | None = None
        for aid in target_accountids:
            if appid in per_account.get(aid, {}):
                ud = per_account[aid][appid]
                ud_account = aid
                break

        cfg: dict[str, Any] | None = None
        for aid, mapping in local_cfg.items():
            if appid in mapping:
                cfg = mapping[appid]
                break

        mechanism = _infer_mechanism(ud, entry, bool(markers.get(appid)))
        apps_view.append(
            {
                "appid": appid,
                "name": (m.name if m else "") or (f"(未安装)" if not m else ""),
                "installed": bool(m),
                "install_dir": str(m.install_dir) if m and m.install_dir else None,
                "size_on_disk": m.size_on_disk if m else 0,
                "library": str(m.library) if m and m.library else None,
                "mechanism": mechanism,
                "cloud_state": entry.cloud_state if entry else "unknown",
                "never_synced": bool(entry.never_synced) if entry else None,
                "no_change_advance": bool(entry.no_change_advance) if entry else None,
                "sync_direction": entry.sync_direction if entry else "unknown",
                "watching_files": entry.watching_files if entry else [],
                "watching_count_reported": entry.watching_count if entry else None,
                "local_change_number": entry.local_cn if entry else None,
                "global_change_number": entry.global_cn if entry else None,
                "incomplete_sync_count": entry.incomplete_sync_count if entry else 0,
                "on_launch_count": entry.on_launch_count if entry else 0,
                "on_exit_count": entry.on_exit_count if entry else 0,
                "userdata_account": ud_account,
                "has_remote_dir": bool(ud and ud["has_remote_dir"]),
                "has_ac_dir": bool(ud and ud["has_ac_dir"]),
                "has_remotecache": bool(ud and ud["has_remotecache"]),
                "remote_files": ud["remote_files"] if ud else 0,
                "remote_bytes": ud["remote_bytes"] if ud else 0,
                "ac_top_dirs": ud["ac_top_dirs"] if ud else [],
                "remotecache_roots": (ud["cache"]["roots"] if ud and ud.get("cache") else {}),
                "remotecache_files": (
                    [e["name"] for e in ud["cache"]["entries"]] if ud and ud.get("cache") else []
                ),
                "autocloud_markers": markers.get(appid, []),
                "ac_root_map": [],
                "candidate_save_dirs": [],
                "candidate_save_dirs_narrowed": False,
                "localconfig": cfg,
            }
        )
    data["apps"] = apps_view

    # --- 9b. Auto-Cloud 根语义解析 + 按账号标识定位存档目录 ---
    # 账号标识**只取当前登录账号**: 用它去反查存档目录才有意义.
    # 若把扫描范围内全部账号 (共用机器上可能有上百个历史账号目录) 的标识混在一起,
    # 候选集会被同基目录下的其他游戏/账号目录淹没.
    ident_source: list[int] = []
    if active is not None:
        ident_source = [active.account_id]
    elif target_accountids:
        try:
            ident_source = [int(target_accountids[0])]
        except (TypeError, ValueError):
            ident_source = []
    ident_strings: list[str] = []
    for aid in ident_source:
        ident_strings.extend(discovery.account_ident_strings(aid))
    ident_strings = sorted(set(ident_strings))
    data["account_ident_strings"] = ident_strings
    current_idents_lower = {s.lower() for s in ident_strings}

    dir_cache: dict[str, list[str]] = {}
    for a in apps_view:
        if not a["ac_top_dirs"]:
            continue
        install_dir = Path(a["install_dir"]) if a["install_dir"] else None
        mapping: list[dict[str, Any]] = []
        raw_candidates: list[str] = []
        for ac_name in a["ac_top_dirs"]:
            base = discovery.resolve_ac_root_dir(ac_name, install_dir)
            mapping.append(
                {
                    "ac_dir": ac_name,
                    "base": str(base) if base else None,
                    "base_exists": bool(base and base.is_dir()),
                    "resolved": base is not None,
                }
            )
            if base and base.is_dir():
                key = os.path.normcase(str(base))
                if key not in dir_cache:
                    dir_cache[key] = [
                        str(p) for p in discovery.find_account_dirs(base, ident_strings)
                    ]
                raw_candidates.extend(dir_cache[key])

        # 用游戏名把宽泛候选集收敛到具体游戏 (不依赖任何硬编码映射)
        game_names = [a["name"] or ""]
        if install_dir is not None:
            game_names.append(install_dir.name)
        scored: list[dict[str, Any]] = []
        for raw in sorted(set(raw_candidates)):
            p = Path(raw)
            level, reason = discovery.match_game_name(p, game_names)
            scored.append(
                {
                    "path": raw,
                    "match_level": level,
                    "match_reason": reason,
                    "matches_current_account": p.name.lower() in current_idents_lower,
                }
            )
        # 收敛优先级: 完全匹配 > 部分匹配 > 全部 (部分匹配容易因罗马数字等后缀误判)
        exact = [c for c in scored if c["match_level"] == "exact"]
        partial = [c for c in scored if c["match_level"] == "partial"]
        if exact:
            pool = exact
        elif partial:
            pool = partial
        else:
            pool = scored
        pool.sort(key=lambda c: (not c["matches_current_account"], c["path"]))
        a["ac_root_map"] = mapping
        a["candidate_save_dirs"] = pool
        a["candidate_save_dirs_narrowed"] = bool(exact or partial)

    # --- 10. 统计与结论 ---
    data["stats"] = _build_stats(apps_view, log)
    data["findings"] = _build_findings(apps_view, log, calibration, manifests)
    data["ds3_case_study"] = _build_ds3_case(apps_view, manifests)
    return data


def _appid_from_path(path: Path, library: discovery.Library, manifests: dict[str, discovery.AppManifest]) -> str:
    """从 steam_autocloud.vdf 的路径反推 appid.

    先按"同库 + installdir 前缀"严格匹配; 失败则退化为"任意库 + installdir 前缀"
    (常见于游戏实际装在别的库、当前库只留了残留目录的情况).
    """
    s = str(path).lower()
    best = ""
    best_len = 0
    for same_library_only in (True, False):
        for appid, m in manifests.items():
            if not m.installdir:
                continue
            if same_library_only:
                if not m.library or os.path.normcase(str(m.library)) != os.path.normcase(str(library.path)):
                    continue
                base = library
            else:
                if m.library is None:
                    continue
                base = discovery.Library(path=m.library)
            prefix = str(base.steamapps / "common" / m.installdir).lower()
            if s.startswith(prefix) and len(prefix) > best_len:
                best, best_len = appid, len(prefix)
        if best:
            return best
    return "unknown"


def _infer_mechanism(ud: dict[str, Any] | None, entry: Any, has_marker: bool) -> str:
    """机制判定: cloud_log 实测 > userdata 目录形态 > 声明文件."""
    if entry is not None and entry.watching_files:
        return "auto-cloud (log-confirmed)"
    if entry is not None and (entry.disabled_count or entry.enabled_count or entry.on_launch_count):
        return "auto-cloud (log-declared)"
    if ud is not None and ud["has_remote_dir"]:
        return "api (ISteamRemoteStorage)"
    if ud is not None and ud["has_ac_dir"]:
        return "auto-cloud (ac-dir)"
    if has_marker:
        return "auto-cloud (marker-only)"
    return "none"


def _build_stats(apps: list[dict[str, Any]], log: Any) -> dict[str, Any]:
    by_mech: dict[str, int] = {}
    for a in apps:
        by_mech[a["mechanism"]] = by_mech.get(a["mechanism"], 0) + 1
    return {
        "app_count": len(apps),
        "installed_count": sum(1 for a in apps if a["installed"]),
        "by_mechanism": dict(sorted(by_mech.items(), key=lambda kv: -kv[1])),
        "log_appid_count": len(log.entries),
        "log_files_read": log.files_read,
        "log_lines_total": log.lines_total,
        "apps_with_watching_files": sum(1 for a in apps if a["watching_files"]),
        "apps_with_remote_dir": sum(1 for a in apps if a["has_remote_dir"]),
        "apps_with_ac_dir": sum(1 for a in apps if a["has_ac_dir"]),
        "disabled_cloud_apps": log.disabled_appids,
        "never_synced_apps": log.never_synced_appids,
        "no_change_advance_apps": log.no_change_advance_appids,
        "incomplete_sync_apps": log.incomplete_appids,
        "testappcloudpaths_seen": log.testcmd_appids,
    }


def _build_findings(
    apps: list[dict[str, Any]],
    log: Any,
    calibration: dict[str, Any],
    manifests: dict[str, discovery.AppManifest],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    disabled = [a for a in apps if a["cloud_state"] == "disabled"]
    for a in disabled:
        findings.append(
            {
                "level": "warn",
                "appid": a["appid"],
                "name": a["name"],
                "title": "Steam 云开关被禁用",
                "detail": (
                    "cloud_log 记录 `AutoCloud is disabled`. "
                    '需要在 Steam 控制台执行 `testappcloudpaths %s` 才能点亮云开关'
                    " (Valve 官方开发者测试命令, 社区复用于零售游戏; 且每次重开 Steam 需重做)."
                    % a["appid"]
                ),
            }
        )

    for a in apps:
        # 强证据: 云被禁用 + 两侧 change number 全 0 (见 cloud_log.never_synced)
        if a["never_synced"]:
            findings.append(
                {
                    "level": "warn",
                    "appid": a["appid"],
                    "name": a["name"],
                    "title": "云被禁用且从未发生任何同步",
                    "detail": (
                        "cloud_log 显示 `AutoCloud is disabled`, 且两侧 change number 全为 0 "
                        "(local=%s, global=%s). 这是**强证据**: 该 appid 在本机从未传输过云文件."
                        % (a["local_change_number"], a["global_change_number"])
                    ),
                }
            )
        # 弱提示: 两侧全 0 但没有禁用记录 (日志可能已轮转, 不能断言)
        elif a["no_change_advance"] and a["installed"]:
            findings.append(
                {
                    "level": "info",
                    "appid": a["appid"],
                    "name": a["name"],
                    "title": "未观察到云同步推进 (弱提示)",
                    "detail": (
                        "cloud_log 里两侧 change number 全为 0, 但没有 `AutoCloud is disabled` 记录. "
                        "可能原因: 日志已轮转 (更早的同步记录被覆盖), 或云端确为空. "
                        "**不能据此断定从未同步** —— 需要看 `%s` 的 watching 记录做交叉验证."
                        % a["appid"]
                    ),
                }
            )

    for a in apps:
        if a["incomplete_sync_count"]:
            findings.append(
                {
                    "level": "warn",
                    "appid": a["appid"],
                    "name": a["name"],
                    "title": "Auto-Cloud 曾静默失败",
                    "detail": (
                        "cloud_log 出现 %d 次 `detected incomplete sync after previous run`. "
                        "Auto-Cloud 会失败而不报错, 跨机器方案必须自带 sha 校验."
                        % a["incomplete_sync_count"]
                    ),
                }
            )

    # root 标定: 未命中任何基目录的枚举值
    for root, cal in sorted(calibration.items()):
        if cal.hits == 0:
            findings.append(
                {
                    "level": "info",
                    "appid": "-",
                    "name": "-",
                    "title": f"root 枚举 {root} 未能标定",
                    "detail": (
                        "该 root 值名下 %d 个相对路径在本机所有候选基目录下都不存在; "
                        "通常是文件只存在于云端, 或路径含变量. 已按推测语义展示, 不参与自动搬运."
                        % cal.total
                    ),
                }
            )

    # 安装目录里有 autocloud 标记但日志里没有 watching 记录
    for a in apps:
        if a["autocloud_markers"] and not a["watching_files"] and a["mechanism"] == "none":
            findings.append(
                {
                    "level": "info",
                    "appid": a["appid"],
                    "name": a["name"],
                    "title": "有 Auto-Cloud 声明文件但无实测记录",
                    "detail": "安装目录存在 steam_autocloud.vdf, 但 cloud_log 无该 appid 记录——可能从未在此机器上游戏过.",
                }
            )

    return findings


def _build_ds3_case(apps: list[dict[str, Any]], manifests: dict[str, discovery.AppManifest]) -> dict[str, Any]:
    """DS3 专项: 支撑 M4-4a (启用云后的真实被监视文件集)."""
    target = "374320"
    hit = next((a for a in apps if a["appid"] == target), None)
    if hit is None:
        return {"appid": target, "present": False}
    out = dict(hit)
    out["present"] = True
    out["m4_4a_answer"] = (
        "已从 cloud_log 取得实测被监视文件集: " + ", ".join(hit["watching_files"])
        if hit["watching_files"]
        else "尚无实测记录 (本机 DS3 的 Auto-Cloud 被禁用, 未产生 Watching 记录) —— M4-4a 仍未闭环"
    )
    out["m4_4b_status"] = "未执行 (需要第二台机器 / 第二块盘的实测)"
    m = manifests.get(target)
    install = Path(m.install_dir) if m and m.install_dir else None
    if install and install.is_dir():
        # 只读探测: mod 加载框架与启动器是否存在 (计划 v3 C 表要求运行时发现).
        # DS3 的模组文件在 `Game\` 子目录下, 而不是安装根目录, 两处都探.
        roots = [install, install / "Game"]
        probes: dict[str, dict[str, Any]] = {}
        launchers: list[str] = []
        for root in roots:
            if not root.is_dir():
                continue
            tag = root.name if root != install else "<root>"
            for key, rel in (
                ("dllMods_dir", "dllMods"),
                ("modengine_ini", "modengine.ini"),
                ("steam_appid_txt", "steam_appid.txt"),
            ):
                candidate = root / rel
                probes[f"{tag}/{key}"] = {"path": str(candidate), "exists": candidate.exists()}
            try:
                launchers.extend(
                    f"{tag}/{p.name}" for p in root.glob("*.exe") if "launcher" in p.name.lower()
                )
            except OSError:
                pass
        out["local_mod_probes"] = probes
        out["local_launchers"] = sorted(launchers)
    return out


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|")


def _render_candidates(cands: list[dict[str, Any]] | None, narrowed: bool = False) -> str:
    """渲染"按账号标识命中的目录"候选: 标注游戏名匹配原因与是否当前账号."""
    if not cands:
        return "—"
    parts: list[str] = []
    for c in cands:
        mark = " ⭐" if c.get("matches_current_account") else ""
        reason = f" <sub>{_md_escape(c['match_reason'])}</sub>" if c.get("match_reason") else ""
        parts.append(f"`{_md_escape(c['path'])}`{mark}{reason}")
    out = "<br>".join(parts)
    if not narrowed:
        out += "<br><sub>(未能与游戏名匹配, 以上为同基目录下全部账号目录, 属宽泛候选)</sub>"
    return out


def render_markdown(data: dict[str, Any]) -> str:
    if data.get("error"):
        return f"# Steam 云存扫描报告\n\n**扫描失败**: {data['error']}\n"

    L: list[str] = []
    stats = data["stats"]
    tool = data["tool"]

    L.append("# Steam 云存全库扫描报告")
    L.append("")
    L.append(f"- 生成时间: `{data['generated_at']}`")
    L.append(f"- 工具: `{tool['name']}` v{tool['version']} (mode: **{tool['mode']}**)")
    L.append(f"- 平台: `{tool['platform']}` / Python {tool['python']}")
    L.append("")
    L.append(
        "> **只读声明**: 本报告由扫描工具生成, 过程中未写入、修改或删除任何 Steam / 游戏文件, "
        "也未启动任何游戏. 扫描结果含本机账号与路径信息, 请勿直接公开."
    )
    L.append("")

    # 环境
    L.append("## 1. 环境")
    L.append("")
    L.append("| Steam 安装 | 来源 | userdata | logs | config |")
    L.append("|---|---|---|---|---|")
    for inst in data["steam_installations"]:
        L.append(
            f"| `{_md_escape(inst['root'])}` | {inst['source']} | "
            f"{'✅' if inst['has_userdata'] else '—'} | {'✅' if inst['has_logs'] else '—'} | "
            f"{'✅' if inst['has_config'] else '—'} |"
        )
    L.append("")
    L.append(f"**主安装**: `{_md_escape(data['active_steam_root'])}`  ")
    scanned = data["scanned_accountids"]
    with_data = data.get("accounts_with_data") or []
    if len(scanned) > 3:
        L.append(
            f"**扫描账号**: 共 {len(scanned)} 个目录, 其中 {len(with_data)} 个有云存数据 "
            f"({', '.join('`%s`' % a for a in with_data[:6]) if with_data else '—'})"
        )
    else:
        L.append(f"**扫描账号**: {', '.join('`%s`' % a for a in scanned) or '—'}")
    L.append("")

    acc = data.get("active_account")
    if acc:
        L.append("| 字段 | 值 |")
        L.append("|---|---|")
        L.append(f"| PersonaName | {_md_escape(acc['persona_name'] or '—')} |")
        L.append(f"| AccountName | {_md_escape(acc['account_name'] or '—')} |")
        L.append(f"| accountid (十进制) | `{acc['account_id']}` |")
        L.append(f"| SteamID64 | `{acc['steamid64']}` |")
        L.append(f"| 十六进制目录名 (DS3 用) | `{acc['account_id_hex_dir']}` |")
        L.append("")

    L.append("**游戏库**")
    L.append("")
    L.append("| 库路径 | 标注 | 有 steamapps |")
    L.append("|---|---|---|")
    for lib in data["libraries"]:
        L.append(
            f"| `{_md_escape(lib['path'])}` | {_md_escape(lib['label'] or '—')} | "
            f"{'✅' if lib['has_steamapps'] else '—'} |"
        )
    L.append("")

    # root 标定
    L.append("## 2. root 枚举标定 (实机探测, 非文档抄写)")
    L.append("")
    L.append(
        "`remotecache.vdf` 里的 `root` 是枚举值而非路径, 官方未文档化. "
        "下表用**真实文件存在性**反推每个取值对应的基目录, 命中率即置信度."
    )
    L.append("")
    L.append("| root | 命中基目录 | 命中/样本 | 置信度 | 推测语义 | 样例 |")
    L.append("|---|---|---|---|---|---|")
    for root, cal in sorted(data["root_calibration"].items(), key=lambda kv: kv[0].zfill(3)):
        sample = cal["samples"][0] if cal["samples"] else "—"
        L.append(
            f"| `{root}` | {_md_escape(cal['base_label'])} | {cal['hits']}/{cal['total']} | "
            f"{cal['confidence']} | {_md_escape(cal['hint'] or '—')} | `{_md_escape(sample)}` |"
        )
    L.append("")
    if data.get("root_calibration_note"):
        L.append(f"> {data['root_calibration_note']}")
        L.append("")
    L.append("候选基目录:")
    L.append("")
    for label, path in data["root_calibration_bases"].items():
        L.append(f"- `{label}` → `{_md_escape(path)}`")
    L.append("")

    # 概览
    L.append("## 3. 概览统计")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append(f"| 涉及 appid 总数 | {stats['app_count']} |")
    L.append(f"| 已安装 | {stats['installed_count']} |")
    L.append(f"| 有实测被监视文件 | {stats['apps_with_watching_files']} |")
    L.append(f"| 有 `remote\\` 目录 (API 型) | {stats['apps_with_remote_dir']} |")
    L.append(f"| 有 `ac\\` 目录 (Auto-Cloud 镜像) | {stats['apps_with_ac_dir']} |")
    L.append(f"| cloud_log 覆盖 appid | {stats['log_appid_count']} |")
    L.append("")
    L.append("**机制分布**")
    L.append("")
    L.append("| 机制 | 数量 |")
    L.append("|---|---|")
    for mech, cnt in stats["by_mechanism"].items():
        L.append(f"| `{mech}` | {cnt} |")
    L.append("")

    # 全库表
    L.append("## 4. 全库云存清单")
    L.append("")
    L.append(
        "`云文件` 列是 `cloud_log.txt` 的**实测**被监视文件数 (最强证据); "
        "`remote` 是 API 型云存目录里的文件数; `ac` 是 Auto-Cloud 镜像目录是否存在."
    )
    L.append("")
    L.append("| AppID | 名称 | 机制 | 云文件 | 云开关 | 同步进度 (local→global) | remote | ac | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for a in data["apps"]:
        cloud_cnt = len(a["watching_files"])
        if a["watching_files"]:
            cloud_cnt_s = f"**{cloud_cnt}**"
        elif a["watching_count_reported"] is not None:
            cloud_cnt_s = str(a["watching_count_reported"])
        else:
            cloud_cnt_s = "—"
        sync = "—"
        if a["local_change_number"] is not None:
            sync = f"{a['local_change_number']} → {a['global_change_number']}"
            if a["never_synced"]:
                sync += " ⚠️"
            elif a["no_change_advance"]:
                sync += " ?"
        notes: list[str] = []
        if a["cloud_state"] == "disabled":
            notes.append("云已禁用")
        if a["never_synced"]:
            notes.append("从未同步")
        elif a["no_change_advance"]:
            notes.append("无变更推进?")
        if a["sync_direction"] == "local-behind":
            notes.append("本地落后")
        elif a["sync_direction"] == "local-ahead":
            notes.append("本地领先")
        if a["incomplete_sync_count"]:
            notes.append(f"失败×{a['incomplete_sync_count']}")
        if not a["installed"]:
            notes.append("未安装")
        L.append(
            f"| `{a['appid']}` | {_md_escape(a['name'] or '—')} | `{a['mechanism']}` | {cloud_cnt_s} | "
            f"{a['cloud_state']} | {sync} | {a['remote_files'] or '—'} | "
            f"{'✅' if a['has_ac_dir'] else '—'} | {_md_escape(', '.join(notes)) or '—'} |"
        )
    L.append("")

    # 实测文件清单
    withfiles = [a for a in data["apps"] if a["watching_files"]]
    L.append("## 5. 实测被监视文件清单 (`cloud_log` 原始证据)")
    L.append("")
    if withfiles:
        L.append("| AppID | 名称 | 被监视文件 |")
        L.append("|---|---|---|")
        for a in withfiles:
            files = "<br>".join(f"`{_md_escape(f)}`" for f in a["watching_files"])
            L.append(f"| `{a['appid']}` | {_md_escape(a['name'] or '—')} | {files} |")
    else:
        L.append("_无: cloud_log 中没有 `Watching file` 记录._")
    L.append("")

    # API 型文件清单
    apis = [a for a in data["apps"] if a["has_remote_dir"] or a["has_remotecache"]]
    if apis:
        L.append("### 5b. API 型 (ISteamRemoteStorage) 云存文件")
        L.append("")
        L.append("| AppID | 名称 | root 分布 | 文件 (前 12) |")
        L.append("|---|---|---|---|")
        for a in apis:
            roots = ", ".join(f"{k}×{v}" for k, v in sorted(a["remotecache_roots"].items()))
            files = a["remotecache_files"][:12]
            files_s = "<br>".join(f"`{_md_escape(f)}`" for f in files) or "—"
            if len(a["remotecache_files"]) > 12:
                files_s += f"<br>… 共 {len(a['remotecache_files'])} 个"
            L.append(
                f"| `{a['appid']}` | {_md_escape(a['name'] or '—')} | {_md_escape(roots) or '—'} | {files_s} |"
            )
        L.append("")

    # Auto-Cloud 根语义汇总
    ac_apps = [a for a in data["apps"] if a.get("ac_top_dirs")]
    if ac_apps:
        L.append("### 5c. Auto-Cloud 根语义解析与存档目录定位")
        L.append("")
        L.append(
            "`ac\\` 的一级目录名可还原成本地基目录 (声明式语义); 再拿账号标识 "
            "(accountid 十进制 / SteamID64 十进制 / 十六进制) 在该基目录下反查, "
            "即可定位真正的存档目录 —— **不需要预先知道游戏把存档放在哪个子目录**."
        )
        L.append("")
        L.append("| AppID | 名称 | ac 目录 | 解析出的基目录 | 按账号标识命中的目录 |")
        L.append("|---|---|---|---|---|")
        for a in ac_apps:
            maps = a.get("ac_root_map") or []
            ac_s = "<br>".join(f"`{m['ac_dir']}`" for m in maps) or "—"
            base_s = (
                "<br>".join(
                    (f"`{_md_escape(m['base'])}`" if m["resolved"] and m["base"] else f"(未识别 `{m['ac_dir']}`)")
                    for m in maps
                )
                or "—"
            )
            cands = a.get("candidate_save_dirs") or []
            cand_s = _render_candidates(cands, bool(a.get("candidate_save_dirs_narrowed")))
            L.append(
                f"| `{a['appid']}` | {_md_escape(a['name'] or '—')} | {ac_s} | {base_s} | {cand_s} |"
            )
        L.append("")

    # DS3 专项
    ds3 = data.get("ds3_case_study") or {}
    L.append("## 6. DS3 专项 (AppID 374320)")
    L.append("")
    if not ds3.get("present"):
        L.append("_未在本机发现 DS3 相关数据._")
    else:
        L.append("| 字段 | 值 |")
        L.append("|---|---|")
        L.append(f"| 安装目录 | `{_md_escape(ds3['install_dir'] or '—')}` |")
        L.append(f"| 机制 | `{ds3['mechanism']}` |")
        L.append(f"| 云开关 | `{ds3['cloud_state']}` |")
        L.append(f"| 被监视文件 | {', '.join('`%s`' % f for f in ds3['watching_files']) or '—'} |")
        L.append(f"| 同步进度 | {ds3['local_change_number']} → {ds3['global_change_number']} |")
        L.append(f"| userdata 目录 | remote={'✅' if ds3['has_remote_dir'] else '—'}, ac={'✅' if ds3['has_ac_dir'] else '—'}, remotecache={'✅' if ds3['has_remotecache'] else '—'} |")
        L.append(f"| ac 镜像一级目录 | {', '.join('`%s`' % d for d in ds3['ac_top_dirs']) or '—'} |")
        acmap = ds3.get("ac_root_map") or []
        if acmap:
            resolved = "<br>".join(
                (f"`{m['ac_dir']}` → `{_md_escape(m['base'])}`" if m["resolved"] and m["base"] else f"`{m['ac_dir']}` → (需额外上下文)")
                for m in acmap
            )
            L.append(f"| ac 根语义解析 | {resolved} |")
        cands = ds3.get("candidate_save_dirs") or []
        L.append(
            f"| 按账号标识定位的存档目录 | "
            f"{_render_candidates(cands, bool(ds3.get('candidate_save_dirs_narrowed')))} |"
        )
        L.append("")
        if ds3["ac_top_dirs"]:
            L.append(
                "> `ac\\` 下的一级目录名是 Auto-Cloud **根语义的可读形式** "
                "(例如 `WinAppDataRoaming` 对应 `%APPDATA%`, 与 `remotecache.vdf` 的 "
                "`root=4` 等价) —— 这是比枚举数字更直观的交叉证据."
            )
            L.append("")
        L.append(f"**M4-4a 结论**: {_md_escape(ds3['m4_4a_answer'])}")
        L.append("")
        L.append(f"**M4-4b 状态**: {_md_escape(ds3['m4_4b_status'])}")
        L.append("")
        probes = ds3.get("local_mod_probes") or {}
        if probes:
            L.append("**本机 mod 加载点探测 (只读, 仅作可选增强)**")
            L.append("")
            L.append("| 探测项 | 路径 | 存在 |")
            L.append("|---|---|---|")
            for k, v in probes.items():
                L.append(f"| `{k}` | `{_md_escape(v['path'])}` | {'✅' if v['exists'] else '—'} |")
            L.append("")
            if ds3.get("local_launchers"):
                L.append(f"检测到启动器: {', '.join('`%s`' % x for x in ds3['local_launchers'])}")
                L.append("")

    # findings
    L.append("## 7. 发现与风险")
    L.append("")
    findings = data.get("findings") or []
    if not findings:
        L.append("_无异常._")
    else:
        order = {"warn": 0, "info": 1}
        for i, f in enumerate(sorted(findings, key=lambda x: order.get(x["level"], 9)), 1):
            icon = "⚠️" if f["level"] == "warn" else "ℹ️"
            L.append(f"**{i}. {icon} {f['title']}** (`{f['appid']}` {f['name']})")
            L.append("")
            L.append(f"   {f['detail']}")
            L.append("")

    L.append("---")
    L.append("")
    L.append(
        "本报告为只读扫描结果. 数据来源: 注册表 / `libraryfolders.vdf` / `appmanifest_*.acf` / "
        "`userdata\\*\\*\\remotecache.vdf` / `logs\\cloud_log*.txt` / `config\\localconfig.vdf`."
    )
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cloud_scan",
        description="只读扫描本机 Steam 云存全貌 (SteamCloudBridge M1 通用内核).",
    )
    ap.add_argument("--steam-root", help="指定 Steam 主安装目录 (默认自动发现)")
    ap.add_argument("--accountid", help="只扫指定 accountid (十进制)")
    ap.add_argument("--all-accounts", action="store_true", help="扫描 userdata 下全部账号")
    ap.add_argument("--out-dir", default=str(PROJECT_ROOT / "reports"), help="报告输出目录")
    ap.add_argument("--no-markers", action="store_true", help="跳过 steam_autocloud.vdf 扫描 (更快)")
    ap.add_argument("--marker-depth", type=int, default=4, help="markers 扫描深度 (默认 4)")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--md", action="store_true", help="只输出 Markdown")
    ap.add_argument("--stdout", action="store_true", help="同时打印 Markdown 到标准输出")
    args = ap.parse_args(argv)

    data = collect(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []

    if not args.json:
        md_path = out_dir / f"steam_cloud_scan_{stamp}.md"
        md_path.write_text(render_markdown(data), encoding="utf-8")
        written.append(md_path)
        latest_md = out_dir / "steam_cloud_scan_latest.md"
        latest_md.write_text(render_markdown(data), encoding="utf-8")
        written.append(latest_md)

    if not args.md and not data.get("error"):
        json_path = out_dir / f"steam_cloud_scan_{stamp}.json"
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(json_path)
        latest_json = out_dir / "steam_cloud_scan_latest.json"
        latest_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(latest_json)

    if data.get("error"):
        print(f"[!] {data['error']}", file=sys.stderr)
        return 2

    for p in written:
        print(f"[+] wrote {p}")

    stats = data["stats"]
    print(
        f"[=] apps={stats['app_count']} installed={stats['installed_count']} "
        f"watching={stats['apps_with_watching_files']} remote={stats['apps_with_remote_dir']} "
        f"ac={stats['apps_with_ac_dir']}"
    )
    if stats["disabled_cloud_apps"]:
        print(f"[!] 云被禁用: {', '.join(stats['disabled_cloud_apps'])}")
    if stats["incomplete_sync_apps"]:
        print(f"[!] 曾同步失败: {', '.join(stats['incomplete_sync_apps'])}")

    if args.stdout:
        print()
        print(render_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
