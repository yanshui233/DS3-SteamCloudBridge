#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_bait_check.py —— 4b 前置验证: Steam 云认不认"外部放进存档目录的文件".

为什么需要它
============
M2 搬运器的全部前提是一句话:

    把存档文件放进游戏存档目录后, Steam 的 Auto-Cloud 会把这次本地变化
    当作**真实游戏进度**上传/保留, 而不是用云端旧版本把本地覆盖回去.

这个前提到目前为止**没有任何一台机器上验证过**. 如果它不成立, 搬运器写完
也是白写 —— 文件放进去, 下一次同步就被云端回滚.

怎么验证 (不用第二台机器, 也不用动真实存档)
============================================
在存档目录里放一个**无害诱饵**:

  * 不是存档本体: 内容是自定义魔数, 游戏读不动, 也不会去读它;
  * 但与真存档同目录、同后缀, 因此**命中同一套 Auto-Cloud 规则**
    (FromSoftware 系均为 `*.sl2`).

然后启动一次 Steam 再关掉, 看诱饵有没有被云端登记:

  (a) `userdata/<sid>/<appid>/remotecache.vdf` 里是否出现诱饵文件名;
  (b) `logs/cloud_log.txt` 里是否出现诱饵的 upload / queue 记录.

任一命中  => Steam **接受**外部文件, M2 的搬运路线成立.
两处都没有 => 需要重新设计 (报告里会给下一步).

最后 `cleanup` 删掉诱饵, 再同步一次把云端那条也撤掉.

四个子命令幂等, `plan` 全程只读.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"
BAIT_PREFIX = "scb-bait-"
BAIT_MAGIC = b"SCBBAIT1"
DEFAULT_EXT = ".sl2"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _harden_console() -> None:
    """管道重定向下 Windows 控制台会退回 GBK, 中文/箭头会炸. 统一 replace."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def list_dir(d: Path, limit: int = 200) -> list[dict]:
    out: list[dict] = []
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append(
            {
                "name": p.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "mtime_iso": datetime.fromtimestamp(st.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
        if len(out) >= limit:
            break
    return out


def find_userdata_app(steam_root: Path | None, appid: str) -> Path | None:
    """在 `userdata/<任何账号>/<appid>/` 里找第一个存在的."""
    if steam_root is None:
        return None
    ud = steam_root / "userdata"
    if not ud.is_dir():
        return None
    for acc in sorted(ud.iterdir()):
        if not acc.is_dir():
            continue
        app = acc / appid
        if app.is_dir():
            return app
    return None


def cloud_log_paths(steam_root: Path | None) -> list[Path]:
    if steam_root is None:
        return []
    logs = steam_root / "logs"
    return [
        p
        for p in (logs / "cloud_log.txt", logs / "cloud_log.previous.txt")
        if p.is_file()
    ]


def state_file(args: argparse.Namespace) -> Path:
    if args.state_file:
        return Path(args.state_file).expanduser()
    return Path.cwd() / "reports" / f"bait-{args.appid}.json"


def make_bait_name(ext: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{BAIT_PREFIX}{ts}-{secrets.token_hex(2)}{ext}"


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def gather(args: argparse.Namespace) -> dict:
    save_dir = Path(args.save_dir).expanduser()
    steam_root = Path(args.steam_root).expanduser() if args.steam_root else None
    ud_app = (
        Path(args.userdata_app).expanduser()
        if args.userdata_app
        else find_userdata_app(steam_root, args.appid)
    )

    info: dict = {
        "tool": {"name": "cloud_bait_check", "version": VERSION},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "appid": args.appid,
        "save_dir": str(save_dir),
        "save_dir_exists": save_dir.is_dir(),
        "steam_root": str(steam_root) if steam_root else None,
        "userdata_app": str(ud_app) if ud_app else None,
        "remotecache": None,
        "cloud_log": None,
        "files": list_dir(save_dir),
        "bait_present": [],
    }

    if ud_app:
        rc = ud_app / "remotecache.vdf"
        info["remotecache"] = {
            "path": str(rc),
            "exists": rc.is_file(),
            "sha256": sha256_file(rc) if rc.is_file() else None,
            "size": rc.stat().st_size if rc.is_file() else None,
            "text": read_text(rc) if rc.is_file() else "",
        }

    logs = cloud_log_paths(steam_root)
    if logs:
        merged = "\n".join(read_text(p) for p in logs)
        info["cloud_log"] = {
            "paths": [str(p) for p in logs],
            "text": merged,
        }

    info["bait_present"] = [
        f["name"] for f in info["files"] if f["name"].startswith(BAIT_PREFIX)
    ]
    return info


def _mentions(info: dict, needle: str) -> dict:
    rc = info.get("remotecache") or {}
    log = info.get("cloud_log") or {}
    log_text = log.get("text") or ""
    hits: list[str] = []
    for line in log_text.splitlines():
        if needle in line:
            hits.append(line.strip()[:300])
    return {
        "in_remotecache": bool(rc.get("text") and needle in rc["text"]),
        "in_cloud_log": bool(hits),
        "log_lines": hits[:40],
    }


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    info = gather(args)
    print(f"cloud_bait_check v{VERSION} — plan (只读)")
    print(f"  检测时间     : {info['generated_at']}")
    print(f"  目标 appid   : {info['appid']}")
    print(f"  存档目录     : {info['save_dir']}")
    print(f"  目录存在     : {info['save_dir_exists']}")
    print(f"  userdata/app : {info['userdata_app']}")
    rc = info.get("remotecache")
    if rc:
        print(f"  remotecache  : {rc['path']}")
        print(f"                 exists={rc['exists']} size={rc['size']} sha256={(rc['sha256'] or '')[:16]}")
    else:
        print("  remotecache  : 未找到 (需要 --steam-root 或 --userdata-app)")
    log = info.get("cloud_log")
    if log:
        print(f"  cloud_log    : {len(log['paths'])} 个文件, {len((log['text'] or '').splitlines())} 行")
    else:
        print("  cloud_log    : 未找到")

    print(f"\n  目录内现有文件 ({len(info['files'])}):")
    for f in info["files"][:40]:
        print(f"    - {f['name']}  {f['size']} B  {f['mtime_iso']}")
    if info["bait_present"]:
        print(f"\n  [注意] 目录里已有诱饵残留: {info['bait_present']} —— 先跑 cleanup")

    print("\n  drop 将执行:")
    print(f"    1. 写入诱饵文件 {BAIT_PREFIX}<时间戳>-<随机>{args.ext} ({len(BAIT_MAGIC)}B 魔数头)")
    print(f"    2. 把当前状态快照存到 {state_file(args)}")
    print("  不影响任何已有文件. 之后请: 启动 Steam -> 等待 1 分钟 -> 关闭 Steam -> 跑 inspect")
    return 0


def cmd_drop(args: argparse.Namespace) -> int:
    save_dir = Path(args.save_dir).expanduser()
    if not save_dir.is_dir():
        print(f"[失败] 存档目录不存在: {save_dir}", file=sys.stderr)
        return 2

    pre = gather(args)
    if pre["bait_present"]:
        print(f"[失败] 已有诱饵残留: {pre['bait_present']} —— 先跑 cleanup", file=sys.stderr)
        return 2

    name = args.bait_name or make_bait_name(args.ext)
    target = save_dir / name
    if target.exists():
        print(f"[失败] 目标已存在: {target}", file=sys.stderr)
        return 2

    payload = (
        BAIT_MAGIC
        + b"\n"
        + f"steamcloudbridge bait probe {datetime.now().isoformat()}\n".encode()
        + secrets.token_bytes(32)
    )
    target.write_bytes(payload)

    snap = {
        "tool": {"name": "cloud_bait_check", "version": VERSION},
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "appid": args.appid,
        "save_dir": str(save_dir),
        "bait_name": name,
        "bait_sha256": sha256_file(target),
        "bait_size": target.stat().st_size,
        "before": {
            "files": pre["files"],
            "remotecache_sha256": (pre.get("remotecache") or {}).get("sha256"),
            "remotecache_text": (pre.get("remotecache") or {}).get("text", ""),
        },
    }
    sf = state_file(args)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[完成] 诱饵已写入: {target}")
    print(f"       大小 {snap['bait_size']} B, sha256 {snap['bait_sha256'][:16]}…")
    print(f"       基线快照: {sf}")
    print("\n下一步:")
    print("  1. 启动 Steam, 等 1 分钟 (无需启动游戏), 然后完全退出 Steam")
    print("  2. 运行: cloud_bait_check.py ... --mode inspect")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    sf = state_file(args)
    if not sf.is_file():
        print(f"[失败] 找不到快照 {sf} —— 先跑 drop", file=sys.stderr)
        return 2
    snap = json.loads(sf.read_text(encoding="utf-8"))
    name = snap["bait_name"]

    now = gather(args)
    m = _mentions(now, name)
    rc_changed = (now.get("remotecache") or {}).get("sha256") != snap["before"].get(
        "remotecache_sha256"
    )
    still_there = name in [f["name"] for f in now["files"]]

    if m["in_remotecache"] and m["in_cloud_log"]:
        verdict = "ACCEPTED"
    elif m["in_remotecache"]:
        verdict = "LIKELY_ACCEPTED"
    elif m["in_cloud_log"]:
        verdict = "PARTIAL"
    else:
        verdict = "NOT_SEEN"

    print(f"cloud_bait_check v{VERSION} — inspect")
    print(f"  诱饵           : {name}")
    print(f"  仍在本目录     : {still_there}")
    print(f"  remotecache 变动: {rc_changed}")
    print(f"  进入 remotecache: {m['in_remotecache']}")
    print(f"  进入 cloud_log  : {m['in_cloud_log']}")
    print(f"\n  >>> 判定: {verdict}")
    if m["log_lines"]:
        print("\n  cloud_log 命中行:")
        for line in m["log_lines"]:
            print(f"    {line}")
    if verdict == "NOT_SEEN":
        print("\n  提示: 确认 Steam 真的重启过; 有些游戏需要**启动游戏**才会触发 Auto-Cloud.")

    report = {
        "tool": {"name": "cloud_bait_check", "version": VERSION},
        "generated_at": now["generated_at"],
        "appid": args.appid,
        "bait_name": name,
        "verdict": verdict,
        "still_present": still_there,
        "remotecache_changed": rc_changed,
        "remotecache_mentions_bait": m["in_remotecache"],
        "cloud_log_mentions_bait": m["in_cloud_log"],
        "cloud_log_lines": m["log_lines"],
        "files_after": now["files"],
    }
    out = sf.with_name(sf.stem + "-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告: {out}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    sf = state_file(args)
    names: list[str] = []
    if sf.is_file():
        snap = json.loads(sf.read_text(encoding="utf-8"))
        names.append(snap["bait_name"])
    save_dir = Path(args.save_dir).expanduser()
    for f in list_dir(save_dir):
        if f["name"].startswith(BAIT_PREFIX) and f["name"] not in names:
            names.append(f["name"])

    removed = []
    for n in names:
        p = save_dir / n
        if p.is_file():
            p.unlink()
            removed.append(n)

    print(f"cloud_bait_check v{VERSION} — cleanup")
    print(f"  已删除诱饵: {removed or '(无)'}")
    print("\n下一步: 再启动一次 Steam 并退出, 让云端把已删除的那条也同步掉.")
    print("  快照与报告留在:")
    print(f"    {sf}")
    print(f"    {sf.with_name(sf.stem + '-report.json')}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloud_bait_check",
        description="4b 前置验证: Steam 云认不认外部放进存档目录的文件 (诱饵法, 不动真实存档)",
    )
    p.add_argument("--mode", required=True, choices=["plan", "drop", "inspect", "cleanup"])
    p.add_argument("--save-dir", required=True, help="游戏存档目录 (绝对路径)")
    p.add_argument("--appid", required=True, help="目标 appid, 例 1245620")
    p.add_argument("--steam-root", help="Steam 根目录 (用于自动定位 userdata/logs)")
    p.add_argument("--userdata-app", help="直接指定 userdata/<sid>/<appid> 目录")
    p.add_argument("--state-file", help="快照文件路径 (默认 ./reports/bait-<appid>.json)")
    p.add_argument("--bait-name", help="自定义诱饵文件名 (默认自动生成)")
    p.add_argument("--ext", default=DEFAULT_EXT, help=f"诱饵后缀 (默认 {DEFAULT_EXT})")
    p.add_argument("--version", action="version", version=f"cloud_bait_check {VERSION}")
    return p


def main(argv: list[str] | None = None) -> int:
    _harden_console()
    args = build_parser().parse_args(argv)
    return {
        "plan": cmd_plan,
        "drop": cmd_drop,
        "inspect": cmd_inspect,
        "cleanup": cmd_cleanup,
    }[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
