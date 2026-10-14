"""搬运层 (M2/M3): 把一份存档就位到"Steam 真正会同步的那个落点".

模块职责:

* `target`       —— 落点解析. 只从机器自身证据反推路径, 绝不自拼目录名.
* `guard`        —— 前置守卫. 会破坏存档的操作一律先被拦下.
* `plan`         —— 搬运计划 (纯计算, 可序列化, 默认不落盘).
* `autocloud`    —— 执行器. 备份 -> 原子写入 -> 刷新 mtime -> 回读校验.
* `cloudchannel` —— **云通道守卫 (M3)**. 按 **Steam 会话** 判定该 appid 的云通道是否点亮.
* `snapshot`     —— **快照 / 回滚 (M3)**. 回滚绝不靠删文件 (实测: 删了会被云端灌回来).
* `verify`       —— **上传校验 + 重试 (M3)**. 实测存在 `Upload Timeout` 静默失败.

设计红线 (与 `docs/plan-v3.md` 一致):

1. **只做同账号自己存档的字节级搬运**, 不解析 / 不修改存档内容.
2. **禁止跨账号导入** —— 目标存档根的 `steam_autocloud.vdf` 记录了归属账号,
   与本机当前账号不一致时拒绝执行.
3. **Steam 运行中禁止改写被监视文件** (会与云端仲裁抢文件).
4. 落点不明时**拒绝执行**, 而不是猜.
5. **`testappcloudpaths` 每次重启 Steam 即失效** —— 云通道是**会话级**状态,
   判定必须按会话, 不能按全文件统计 (那会被旧会话的 `Watching file` 行骗过).
"""

from __future__ import annotations

__all__ = [
    "target",
    "guard",
    "plan",
    "autocloud",
    "cloudchannel",
    "snapshot",
    "verify",
]
