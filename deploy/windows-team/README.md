# Bothouse 双机部署

基线为 Bothouse 官方 `v0.1.89`，上游提交 `8ec73a8f32573b5307922df3c79675a934cdefb7`。网页大厅由上游 `role-household` 提供，本机 Codex 和远端 Claude Code 使用官方 `cloud_web` 平台连接。已有 Discord 平台可以同时启用。

## 启动

1. 在两台机器使用同一版本的官方 Bothouse，并确认相应 CLI 能完成一次普通对话。本机 Codex 使用独立的 `codex_home` 和 `sqlite_home`。
2. 在仓库外准备状态目录。`hub.json` 包含分别生成的 `web_token` 和 `bot_token`。迁移旧大厅时，先备份 `hub.json` 和 `hub.sqlite3`。
3. 本机运行 `Start-Lobby.ps1 -StateDirectory <状态目录>`。默认监听 `127.0.0.1:8790`。如果 `uv.exe` 未加入 PATH，用 `-UvPath` 指定安装路径。也可使用已有 `aiohttp` 和 `cryptography` 的 Python，通过 `-PythonPath` 指定解释器。
4. 同一台本机运行 `Start-LobbyTunnel.ps1 -SshHost <SSH 配置别名>`，建立从远端 `127.0.0.1:18879` 到本机大厅的 SSH 转发。首次使用前在本机核对并保存 SSH 主机指纹。Windows 计划任务可在登录时启动这两个脚本。
5. 按 `cloud-web.toml.example` 在两端已有的角色项目中各加一个平台。两端配置使用同一个大厅 bot token，本机连 `127.0.0.1:8790`，远端连 `127.0.0.1:18879`。地址形式为 `ws://127.0.0.1:<端口>/cloud-web/ws?bot=<角色ID>&mention_id=<提及ID>&name=<显示名>`。Codex 的 `team_lead_bot` 填 Claude 角色的提及 ID。
6. 重启受影响的两个 Bothouse 角色。在大厅只点名主角色，验证它点名队员派单、队员报告实测证据、主角色在同一频道给出结论。

在主角色的配置中说明大厅任务用正常消息点名队员。在队员的配置中要求将验收字段和关键证据放在回执开头。两份配置模板只使用环境变量占位符，实际令牌、数据库和本地路径都由现场配置。

## 回滚

先停止新启动任务，再恢复原角色配置和原大厅启动方式。迁移后已有新消息时先备份当前数据库，再决定是否恢复旧库。
