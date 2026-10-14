#!/usr/bin/env python3
"""在**假 Steam 环境**里跑 `machine_probe`, 并可选按预期断言.

配合 `tools/make_fake_steam.py` 使用: 后者造环境, 本工具负责

1. **环境隔离** —— 把 `USERPROFILE` / `APPDATA` / `LOCALAPPDATA` / `PROGRAMDATA`
   指向假用户档, 并把 `--steam-root` 钉死在假 Steam 根上。这样探测器既不会
   读注册表发现真机 Steam, 也不会去真 `%APPDATA%` 下找存档。
2. **断言** —— `--verify` 时按 `manifest.json` 的 `expected` 校验报告, 让整条
   链路变成一个可进 CI 的端到端用例。

用法
----
    python tools/run_sandbox_probe.py --env cases/sandbox --zip --verify
    python tools/run_sandbox_probe.py --env cases/sandbox --exe dist/SteamCloudBridgeProbe.exe
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_APPID = "550"


def _load_manifest(env_dir: Path) -> dict:
    path = env_dir / "manifest.json"
    if not path.is_file():
        raise SystemExit(
            f"找不到 {path}\n先用 tools/make_fake_steam.py 生成环境: \n"
            f"  python tools/make_fake_steam.py --out {env_dir} --scenario synced"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _decode(raw: bytes) -> str:
    """exe 在管道下输出 cp936, .py 走解释器是 UTF-8 —— 两种都接住."""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _verify(manifest: dict, data: dict) -> int:
    exp = manifest["expected"]
    games = {str(g.get("appid")): g for g in data.get("games", [])}
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # --- 隔离性: 必须是假根, 不能是真机 Steam ---
    primary = str((data.get("steam") or {}).get("primary_root") or "")
    check(
        "--steam-root 生效 (未回落真机)",
        os.path.normcase(primary) == os.path.normcase(manifest["steam_root"]),
        primary,
    )
    scanned_accounts = sorted(
        ((data.get("steam") or {}).get("userdata_apps_by_account") or {}).keys()
    )
    check(
        "扫描账号 = 沙箱账号 (未读真机注册表)",
        scanned_accounts == [str(manifest["account_id"])],
        str(scanned_accounts),
    )

    # --- Auto-Cloud 型主角 (374320) ---
    ds3 = games.get(manifest["appid"])
    check(f"{manifest['appid']} 被探测到", ds3 is not None)
    if ds3 is not None:
        check(
            "机制判定 = auto-cloud",
            ds3.get("mechanism") == exp["appid_mechanism"],
            str(ds3.get("mechanism")),
        )
        resolved = str(ds3.get("save_dir_resolved") or "")
        expected_dir = os.path.join(
            manifest["env"]["APPDATA"], *exp["save_dir_suffix"].split("\\")
        )
        check(
            "存档目录定位到假 %APPDATA%",
            os.path.normcase(resolved) == os.path.normcase(expected_dir),
            resolved or "(未定位)",
        )
        cn = ds3.get("last_upload_change_number")
        if cn is None:
            cn = (ds3.get("change_numbers") or {}).get("global")
        check(
            f"云端变更号 = {exp['change_number']}",
            int(cn or 0) == int(exp["change_number"]),
            str(cn),
        )
        check(
            "上传确认状态一致",
            bool(ds3.get("upload_confirmed")) == bool(exp["upload_verified"]),
            str(ds3.get("upload_confirmed")),
        )
        check(
            "verdict.bridgeable = 可桥接",
            bool((ds3.get("verdict") or {}).get("bridgeable")) == bool(exp["bridgeable"]),
            json.dumps(ds3.get("verdict"), ensure_ascii=False)[:70],
        )
        if exp["cloud_disabled"]:
            check(
                "云被禁用时给出开关提示",
                "testappcloudpaths" in json.dumps(ds3.get("verdict"), ensure_ascii=False),
            )
        # console_log 证据: 没有它就只能说"云被禁用", 无法区分
        # "用户没执行点亮命令" 与 "执行了但被重启清掉" (2026-10-13 实测卡点).
        check(
            "console_log 可用性与场景一致",
            bool((data.get("capabilities") or {}).get("console_log_available"))
            == bool(exp["console_log_present"]),
            str((data.get("capabilities") or {}).get("console_log_available")),
        )
        ack = ds3.get("cloud_toggle_console")
        check("点亮命令回执字段已采集", ack is not None, json.dumps(ack, ensure_ascii=False)[:80])
        if ack is not None:
            check(
                f"点亮命令执行回执 = {exp['toggle_cmd_executed']}",
                bool(ack.get("found")) == bool(exp["toggle_cmd_executed"]),
                str(ack.get("found")),
            )
        if exp["cloud_disabled"]:
            joined = " ".join((ds3.get("verdict") or {}).get("reasons") or [])
            check(
                "禁用且无执行回执时, 判定必须写明命令从未执行过",
                "执行回执" in joined and "没有" in joined,
                joined[:90],
            )
        if exp["expect_ac_based_locating"]:
            check(
                "无 Watching 时靠 ac 镜像 + 账号标识定位 (D2)",
                ds3.get("save_dir_source") != "cloud_log",
                str(ds3.get("save_dir_source")),
            )
        check(
            "存档快照命中真实存档",
            (ds3.get("save_snapshot") or {}).get("file_count") == 1
            and (ds3.get("save_snapshot") or {}).get("extensions") == {".sl2": 1},
            json.dumps(ds3.get("save_snapshot"), ensure_ascii=False)[:90],
        )

    # --- API 型对照 (550): 有 remote\ 实体, 不该被 marker 夺权 ---
    api = games.get(API_APPID)
    if api is not None:
        check(
            f"{API_APPID} 机制 = remote-storage-api",
            api.get("mechanism") == exp["api_appid_mechanism"],
            str(api.get("mechanism")),
        )

    # --- 脱敏: 报告里不该出现真机家目录 ---
    real_home = os.path.expanduser("~")
    share = json.loads(
        (Path(manifest["_env_dir"]) / "reports" / "machine_profile.share.json")
        .read_text(encoding="utf-8")
    )
    leaked = [
        s for s in (real_home, manifest["save_file"]) if s and s in json.dumps(share)
    ]
    check("脱敏档不含真机家目录", not leaked, str(leaked))

    # --- 目录分层: 未脱敏完整档不得躺在根目录 (否则用户极易整包外发) ---
    reports_dir = Path(manifest["_env_dir"]) / "reports"
    check(
        "完整档收在 private/ 下",
        (reports_dir / "private" / "machine_profile.json").is_file(),
    )
    check(
        "根目录没有未脱敏完整档",
        not (reports_dir / "machine_profile.json").is_file(),
    )
    check(
        "根目录有发送说明",
        (reports_dir / "00-发送说明.txt").is_file(),
    )

    # --- 拉起方式必须记进档案 (.py 跑就该是 python-script) ---
    check(
        "档案记录了拉起方式",
        (data.get("tool") or {}).get("entry") == "python-script",
        str((data.get("tool") or {}).get("entry")),
    )
    check(
        "脱敏档也保留拉起方式",
        (share.get("tool") or {}).get("entry") == "python-script",
        str((share.get("tool") or {}).get("entry")),
    )

    # --- 报告头必须与拉起方式**自洽** (.py 跑就不能写"内置 Python") ---
    md_text = (reports_dir / "machine_profile.md").read_text(encoding="utf-8")
    header = next(
        (ln for ln in md_text.splitlines() if ln.startswith("- 系统:")), ""
    )
    check(
        "报告头不把本机解释器说成内置",
        "内置" not in header and "Python `" in header,
        header,
    )
    check(
        "报告头写明运行方式",
        any(ln.startswith("- 运行方式: Python 脚本") for ln in md_text.splitlines()),
    )

    # --- 覆盖率: 必须记录"已安装 appid 全量清单" ---
    # 事故来源: 朋友机回传档里 DS3 (374320) 零痕迹, 据此被读成"没装", 实为已装.
    # 逐游戏详查只覆盖有云证据的候选, 所以必须另记一份已安装清单, 否则
    # "报告里没有" = "机器上没有" 这个错误推理迟早再犯.
    installed = (data.get("steam") or {}).get("app_manifest_appids")
    probed_ids = {str(g.get("appid")) for g in (data.get("games") or [])}
    check(
        "档案记录已安装 appid 全量清单",
        isinstance(installed, list) and bool(installed) and installed == sorted(installed),
        f"{len(installed) if isinstance(installed, list) else installed} 条",
    )
    expect_missing = sorted(a for a in (installed or []) if a not in probed_ids)
    caps = data.get("capabilities") or {}
    check(
        "已安装但未详查的清单自洽",
        caps.get("installed_not_probed_appids") == expect_missing
        and caps.get("installed_not_probed_count") == len(expect_missing),
        f"count={caps.get('installed_not_probed_count')} expect={len(expect_missing)}",
    )
    check(
        "报告写明未详查口径 + 防误读提示",
        "已安装但未详查" in md_text and '不是"没装"' in md_text,
    )

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    print()
    print("沙箱断言:")
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {mark} {name.ljust(width)}{suffix}")
    print()
    print(f"结果: {len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在假 Steam 环境里跑探测器")
    parser.add_argument("--env", required=True, help="make_fake_steam.py 的输出目录")
    parser.add_argument("--exe", default="", help="改用打包好的 exe 跑 (验证发行包)")
    parser.add_argument("--out-dir", default="", help="报告输出目录 (默认 <env>/reports)")
    parser.add_argument("--zip", action="store_true", help="额外打包回传包")
    parser.add_argument("--quiet", action="store_true", help="少打印")
    parser.add_argument("--verify", action="store_true", help="按 manifest 预期断言")
    args = parser.parse_args(argv)

    env_dir = Path(args.env).resolve()
    manifest = _load_manifest(env_dir)
    manifest["_env_dir"] = str(env_dir)
    out_dir = Path(args.out_dir).resolve() if args.out_dir else env_dir / "reports"

    child_env = dict(os.environ)
    child_env.update({k: str(v) for k, v in manifest["env"].items()})

    if args.exe:
        cmd = [str(Path(args.exe).resolve())]
    else:
        cmd = [sys.executable, str(PROJECT_ROOT / "tools" / "machine_probe.py")]
    cmd += [
        "--steam-root", str(manifest["steam_root"]),
        # 必须显式钉住账号: 否则机器注册表里的真机 accountid 会被当成"当前账号",
        # 既污染沙箱报告, 又让假 userdata 目录匹配不上 (D5)。
        "--account", str(manifest["account_id"]),
        "--appid", str(manifest["appid"]),
        "--appid", API_APPID,
        "--out-dir", str(out_dir),
    ]
    if args.zip:
        cmd.append("--zip")
    if args.quiet:
        cmd.append("--quiet")

    proc = subprocess.run(cmd, env=child_env, capture_output=True, timeout=600)
    print(_decode(proc.stdout))
    if proc.stderr:
        print(_decode(proc.stderr), file=sys.stderr)
    if proc.returncode != 0:
        print(f"探测失败: rc={proc.returncode}")
        return proc.returncode

    # 目录分层: 未脱敏完整档在 private/ 下, 根目录只放可外发的东西.
    report = out_dir / "private" / "machine_profile.json"
    if not report.is_file():
        # 兼容旧布局 (根目录直接放完整档)
        legacy = out_dir / "machine_profile.json"
        if legacy.is_file():
            report = legacy
        else:
            print(f"未生成报告: {report}")
            return 3

    if not args.verify:
        print(f"报告: {report}")
        return 0

    data = json.loads(report.read_text(encoding="utf-8"))
    return _verify(manifest, data)


if __name__ == "__main__":
    raise SystemExit(main())
