# Bothouse Windows 大厅 · Tailscale 直连

本机 Windows 运行上游 `role-household` Hub，监听 `127.0.0.1:8790`。远端主角色走 Tailnet TCP 8790 直连本机，本机 Codex 走回环，两边都使用 Bothouse 官方 `cloud_web` 平台。可另开 Tailnet HTTPS 443，供 iPhone 上的大厅 PWA 接收 Web Push。两个入口都由 Tailscale Serve 提供。

## 1. 启动本机 Hub

在两台机器安装同一版本的 Bothouse 和 Tailscale，确认相应 CLI 已登录并能完成普通对话。命令从仓库根目录运行。沿用 [Windows 双机部署](../windows-team/README.md) 的 Hub 状态目录，或在仓库外准备 `hub.json`，填入各自生成的 `web_token` 和 `bot_token`。迁移前备份 `hub.json`、`hub.sqlite3` 和已有的 `vapid_private.pem`。

~~~powershell
$hubDir = Join-Path $env:USERPROFILE '.bothouse/hub'
.\deploy\windows-team\Start-Lobby.ps1 -StateDirectory $hubDir -UserMentionId '<USER_DISCORD_ID>'
~~~

`-UserMentionId` 让 Hub 将 `<@用户ID>` 识别为点名本人，可触发手机通知。省略时沿用现有环境。可用 `-UvPath` 指定 `uv.exe`，或用 `-PythonPath` 指定已安装 `aiohttp` 和 `cryptography` 的 Python。

## 2. 设置 Tailnet 访问权限

在 Tailscale 管理后台的访问控制策略中合并 [grants 模板](tailscale-acl.grants.example.json)。把 `100.64.0.10/32` 换成本机 Tailnet IPv4，把 `100.64.0.20/32` 换成远端的地址。需要手机访问时，启用注释中的手机规则并把 `100.64.0.30/32` 换成 iPhone 的地址。手机只通过 PWA 连接时，可在手机规则里只保留 `tcp:443`。

Tailscale 的授权规则会叠加。若现有 `grants` 或 `acls` 已允许其他设备访问本机这些端口，先收窄相应规则，才能实现白名单。保留 Tailnet 内其他业务所需的授权。

## 3. 开启本机 Tailscale Serve

在本机另开一个 PowerShell 窗口。先看计划，再执行：

~~~powershell
.\deploy\tailscale-lobby\Enable-TailscaleLobbyServe.ps1 -DryRun
.\deploy\tailscale-lobby\Enable-TailscaleLobbyServe.ps1
~~~

如果需要手机 Web Push，在 Tailscale 管理后台的 **DNS** 页面启用 **MagicDNS** 和 **HTTPS Certificates**，再运行：

~~~powershell
.\deploy\tailscale-lobby\Enable-TailscaleLobbyServe.ps1 -EnableHttps
~~~

脚本检查 TCP 8790 和所选的 HTTPS 监听，最后显示 Serve 状态。已有其他服务占用这些端口时，先确认端口分配。若更改端口，同步修改访问规则和客户端 URL。

## 4. 配置远端主角色和本机 Codex

在远端 Windows 管理员 PowerShell 中开启无人值守模式。保留已有部署所需的其他 `tailscale up` 参数。远端通过 Tailnet IPv4 直连，继续使用本机的 DNS 设置：

~~~powershell
tailscale up --unattended --accept-dns=false
Test-NetConnection '<LOCAL_TAILSCALE_IP>' -Port 8790
~~~

长期无人值守的远端可以在 Tailscale 管理后台 **Machines** 中选择该设备的 **Disable key expiry**，避免节点密钥过期造成断连。

1. 在远端现有主角色项目下追加 [远端 cloud_web 配置](remote-cloud-web.toml.example)。填写本机的 Tailnet IPv4、主角色 ID、提及 ID 和显示名。
2. 将 [本机 Codex 配置](local-codex-cloud-web.toml.example) 合并到本机已有的 Codex 项目。设置实际的工作目录、可用的 `cmd` 和独立的 `codex_home`，并在该 home 中准备 Codex 登录。追加 `cloud_web` 平台，填入 Codex 的角色 ID、提及 ID 和显示名。Codex 的 `team_lead_bot` 要与远端的 `mention_id` 一致。
3. 让两端 Bothouse 进程都从各自的启动环境读取 `BOTHOUSE_LOBBY_BOT_TOKEN`，值为 Hub 状态里的 `bot_token`。保留 TOML 中的环境变量引用。若显示名含中文、空格或特殊字符，先对 URL 查询参数值做百分号编码。
4. 重启受影响的两个角色，确认日志出现 `cloud_web: websocket connected`。

## 5. 在 iPhone 安装大厅并打开通知

1. 让 iPhone 上的 Tailscale 连到同一 Tailnet。在 Safari 打开 `https://<LOCAL_MAGICDNS_HOST>/`，使用本机完整的 MagicDNS 名称。填写 Hub 的网页访问口令。
2. 在 Safari 分享菜单选「添加到主屏幕」。从新建的主屏幕图标打开大厅，按需重新登录，然后点「开通知」并允许通知。iPhone 需要 iOS 16.4 或更新版本。
3. 将大厅切到后台，请角色发一条点名你的消息，确认通知出现且点开后能进入对应频道。

## 6. 像素办公室（T3 Code × Bothouse）与 Web 管理面板

`role-household` 现已内置 **T3 Code × Bothouse 像素办公室（`/collab/desk`）** 与 **Bothouse Web 管理面板反向代理（`/collab/panel/`）**，桌面端、T3 Preview 与 iPhone PWA 均可复用现有的 `443` Tailscale Serve 入口与 Cookie 登录态，无需新增 Tailnet 端口或 ACL 规则：

1. 本机配置启用管理面板与 Bridge 后重启 Bothouse（详见 `local-codex-cloud-web.toml.example` 末尾注释中的 `[management]` 与 `[bridge]` 节；建议绑定回环端口并分别配置随机 token，`rh.hub` 通过 `/bridge/ws` 反向代理面板的实时对话 WebSocket）。
2. 启动 Hub 时传入 `-T3StateDb`、`-PanelConfig`、可选 `-WikiDir` 与可选 `-LogFile`：

~~~powershell
.\deploy\windows-team\Start-Lobby.ps1 `
    -StateDirectory (Join-Path $env:USERPROFILE '.bothouse/hub') `
    -UserMentionId '<USER_DISCORD_ID>' `
    -T3StateDb '<T3CODE_STATE_DB>' `
    -PanelConfig '<BOTHOUSE_CONFIG_TOML>' `
    -WikiDir (Join-Path $env:USERPROFILE '.bothouse/wiki') `
    -LogFile (Join-Path $env:USERPROFILE '.bothouse/hub/hub.log')
~~~

- **Windows APNs 证书自动注入**：传入 `-PythonPath` 且未预设 `SSL_CERT_FILE` 时，`Start-Lobby.ps1` 会在启动 `rh.hub` 前自动读取 `certifi.where()` 写入 `SSL_CERT_FILE`，确保 Windows `aiohttp` 向苹果 APNs 发送 Web Push 时 TLS 验证通过。
- **统一入口**：
  - 聊天大厅 `/` 左侧服务器栏新增 **`办`**（`/collab/desk`）与 **`管`**（`/collab/panel/`），登录聊天大厅时自动同步 `HttpOnly` Cookie（`rh_auth`）。
  - 像素办公室 `/collab/desk` 同时实时呈现大厅 Bot（如 `remote-ll`、`local-codex`）与 T3 Code 只读状态库（`t3-antigravity` 主控与 `t3-codex` 子代理委派/完成光束及回放）。
  - 知识星河 `/collab/wiki-galaxy`（数据源 `/collab/api/wiki-galaxy`）直接扫描 `RH_WIKI_DIR`（默认 `<state_parent>/wiki`）渲染 Markdown 图谱。
  - **团队调度模式**：聊天大厅顶部栏提供 `单兵` / `混合` / `团队` 三档模式切换（支持工作区继承主频道配置），也可发送 `/solo`、`/mixed`、`/team` 持久切换，或用 `/team <任务>` / `团队模式：<任务>` 单轮强制触发团队协作。
  - **智能闸门（Gate）分流（可选）**：当本机启用 Bothouse `gate`（配合本机 Ollama / JEV 模型）且仅在 `cloud_web` 平台开启 `group_reply_all = true`（`discord` 保持 `false` 隔离）时，可通过 `[projects].gate_url` 接入智能判断；远端主角色可直接通过 `rh.hub` 在 Tailnet 8790 提供的鉴权反向代理 `http://<LOCAL_TAILSCALE_IP>:8790/gate/<BOT_TOKEN>/decide` 共用同一个本地 Gate，实现不 `@` 自动选人接话与打断/插话控制。

## 验证

在本机运行以下命令。`-ExpectOnlineBots` 使用 URL 中的 `bot` 值。做 WebSocket 检查时，`-PythonPath` 指向安装了 `aiohttp` 的 Python 3.11 或更新版本：

~~~powershell
.\deploy\tailscale-lobby\Test-TailscaleLobby.ps1 `
    -HubStatePath (Join-Path $env:USERPROFILE '.bothouse/hub/hub.json') `
    -CheckTailscale -ExpectOnlineBots @('<LEAD_BOT_ID>', '<CODEX_BOT_ID>')
~~~

脚本检查首页和 `/api/push` 的 HTTP 200，只显示推送公钥长度。它还检查 Serve 的 TCP 转发和指定角色的 WebSocket 在线状态。可在另一台有权限的设备上用 `-LobbyUrl 'http://<LOCAL_TAILSCALE_IP>:8790'` 验证直连，或用 `-LobbyUrl 'https://<LOCAL_MAGICDNS_HOST>'` 验证 HTTPS。运行访问探测的设备需要能读取同一个 Hub 的状态文件，`-CheckTailscale` 用于运行 Serve 的本机。

最后在大厅点名主角色，让它派单给 Codex，再核对 Codex 的回执和主角色在同一频道给出的结论。

## 排查

- **本机首页正常，远端连不上。** 先用 `Test-NetConnection` 检查 Tailnet TCP 8790，再核对 Serve 状态和授权规则。若 TCP 可达但 HTTP 或 WebSocket 被本机代理截获，在发起连接的设备上为大厅地址或 Tailnet 网段 `100.64.0.0/10` 添加直连规则，并检查 Bothouse 进程环境里的 `NO_PROXY` 是否包含大厅的准确地址。
- **PowerShell 5.1 找不到 `-NoProxy`。** 用 `Test-TailscaleLobby.ps1`，HTTP 和 WebSocket 探测会绕过系统代理。在 PowerShell 7 中也可用 `Invoke-WebRequest -NoProxy` 单独检查首页。
- **Codex 升级后找不到可执行文件。** 用 `Get-Command codex.exe` 查安装位置。若旧的版本目录已轮换，更新 `cmd` 或 Bothouse 启动环境的 PATH，再重启角色。
- **Codex 在线，任务停在工具审批。** 检查 `mode`。 `suggest` 需要交互确认。此模板使用 `full-auto`，在工作区沙箱内自动执行。改模式后重启角色。
- **iPhone 能打开页面但收不到通知。** 检查 HTTPS 证书、主屏幕应用的通知权限和 Hub 状态目录中的 VAPID 私钥。若私钥轮换，从主屏幕图标重新打开应用，让它重新登记订阅，再测试点名。

## 回滚

在本机关闭大厅对应的 Serve 监听。脚本会检查默认 TCP 8790 和 HTTPS 443，移除存在的对应监听。使用过自定义端口时传入相同端口参数：

~~~powershell
.\deploy\tailscale-lobby\Enable-TailscaleLobbyServe.ps1 -Disable -DryRun
.\deploy\tailscale-lobby\Enable-TailscaleLobbyServe.ps1 -Disable
~~~

撤销为大厅新增的 Tailnet 授权，恢复两端原角色配置和启动方式，再重启受影响的角色。若恢复 [SSH 隧道方案](../windows-team/README.md)，先确认原转发端口可用。还需要恢复旧库时，先备份当前数据库及 VAPID 私钥。
