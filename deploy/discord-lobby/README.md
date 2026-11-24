# Bothouse Discord 转发版

基于 Bothouse 官方 `v0.1.89`。本机运行官方网页大厅和此转发程序，AWS 上的 Bothouse 继续通过 Discord 调用 Claude CLI。网页大厅的消息经 Discord 发给目标机器人，目标机器人的回复和任务卡回到大厅。运行链路仅需要本机访问 Discord HTTPS API，不需要 AWS 后端访问本机，也不需要 SSH 隧道。

## 安装

1. 按[大厅启动说明](../windows-team/README.md)准备 `hub.json` 和 `hub.sqlite3`，只启动 `Start-Lobby.ps1`。
2. 选择一个用于转发的 Discord 机器人账号和一个目标机器人账号。给转发机器人在指定频道的查看频道、读消息历史、发送消息权限，并启用 Message Content Intent。在目标 Bothouse 角色的 Discord 平台配置里，把转发机器人的用户 ID 加到现有的 `allow_bots`。团队派活由目标机器人的官方 Bothouse 处理，该机器人需要在相应分类有管理频道权限。
3. 在 Windows 用将来运行转发程序的用户，从仓库根目录安装依赖。

```powershell
python -m pip install -r deploy/discord-lobby/requirements.txt
```

4. 已有 Windows DPAPI 凭据时，将 `sender_credential` 指向该凭据。新建凭据时，在 Windows PowerShell 运行以下命令。第二行会提示输入转发机器人的 Discord Bot Token。

```powershell
$dir = Join-Path $env:USERPROFILE '.bothouse/hub/discord-relay'
$secret = Read-Host 'Discord relay bot token' -AsSecureString
Add-Type -AssemblyName System.Security
[void][IO.Directory]::CreateDirectory($dir)
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $bytes = [Text.Encoding]::UTF8.GetBytes([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr))
    $encrypted = [Security.Cryptography.ProtectedData]::Protect(
        $bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    [IO.File]::WriteAllBytes((Join-Path $dir 'discord-token.dpapi'), $encrypted)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    if ($bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
    $secret.Dispose()
}
```

5. 根据 [配置模板](relay.toml.example) 在仓库外创建 `relay.toml`，填入 Discord 服务器、频道、两个机器人用户 ID，以及要连接的大厅频道 ID。用同一 Windows 用户执行只读检查，再启动。

```powershell
$dir = Join-Path $env:USERPROFILE '.bothouse/hub/discord-relay'
python -B deploy/discord-lobby/relay.py "$dir/relay.toml" --state "$dir/relay.sqlite3" --check
python -B deploy/discord-lobby/relay.py "$dir/relay.toml" --state "$dir/relay.sqlite3" --log "$dir/relay.log"
```

也可以用 Windows 计划任务在登录时以同一用户启动，脚本、配置、数据库和日志均使用绝对路径。状态数据库保存已转发的消息 ID、已同步的 Discord 消息 ID 和频道对应关系。重启后仍会同步已登记任务卡在最新 50 条消息中的编辑。每个大厅频道与 Discord 频道的关联使用独立的数据库。

## 切换与验证

先在单独的大厅频道用 `@ll-discord` 发送一条短消息，检查回复和 Discord 原消息链接。配置切到正式频道时，先等旧连接上的任务交回，再停止旧连接。把 `bot_id` 和 `alias` 改为原大厅角色 ID 和显示名，启动新版并检查大厅在线状态。

转发程序只处理大厅用户明确点名的文本，并在指定 Discord 频道镜像目标机器人的新回复。遇到 Discord 暂时性故障时，会用相同的去重标识短暂重试。收到未确认送达的提示时，先检查原频道有没有收到该条请求。它可以同步目标机器人回复中明确链接的同分类 `工作区N` 文字频道。Discord 仍是任务板和频道的来源。网页发出的消息在 Discord 上以转发机器人身份显示，目标角色按人隔离会话时，网页与真人账号的聊天上下文各自独立。首次启动从当前消息位置开始，大厅离线期间发出的请求需在转发角色重新上线后重发。

从仓库根目录运行集成测试。测试使用官方大厅和本地 Discord 测试数据。

```powershell
python -m pip install cryptography
$env:PYTHONPATH = (Resolve-Path role-household).Path
python -B deploy/discord-lobby/test_relay.py -v
```

## 回滚

停止 Discord 转发进程，恢复原大厅 SSH 隧道和 AWS 角色原来的 `cloud_web` 连接。两版可以共用大厅数据库，转发程序的 SQLite 状态单独保存。
