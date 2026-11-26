# Pure Discord 单机队伍

Discord 是队伍的聊天入口。Bothouse 在同一台电脑上接收消息，把任务交给本机的 Claude Code、Codex 或 Gemini CLI。Gate 负责团队路由，smart 模式可接本机 JEV 判断谁该回复。套件通过命令行配置，全程无需 Web Lobby 或 Tailscale。示例中的 Codex 角色有独立的配置目录和 SQLite 状态，桌面 Codex 会话留在原有目录。

分享时请保留 `deploy/pure-discord/` 和同级的 `deploy/personas/`。脚本需要 Python 3.11+，只使用标准库。

## 准备 Discord 和本机 CLI

1. 在 Discord Developer Portal 为每个角色创建独立的应用和 Bot。在 Bot 页启用 **Message Content Intent** 和 **Server Members Intent**，保存每个 Bot 的 Token。
2. 在 Discord 的高级设置开启开发者模式。右键服务器复制 Server ID，右键自己的用户复制 User ID。若要指定默认频道，也复制该频道的 ID。
3. 安装并登录要用的 CLI。双角色示例需要 `claude auth login` 和 `codex login`。使用 Gemini 的角色可运行 `gemini` 完成登录。安装 Bothouse 后运行 `bothouse setup clis` 查看检测结果。
4. 确认你有权限邀请应用到服务器，并给 Bot 访问目标频道、读消息历史、发送消息及添加反应的权限。

## macOS 快速开始

在仓库根目录运行。Apple Silicon 和 Intel 均可使用默认的 addressed 模式。

```bash
bash deploy/pure-discord/mac/install-bothouse-mac.sh
export PATH="$HOME/.local/bin:$PATH"
python3 deploy/pure-discord/setup_discord.py init-manifest
```

若安装器选用了带版本号的 Python，请在后续命令中使用它输出的解释器路径。打开 `~/.bothouse/pure-discord/team.toml`，填写 `guild_id`、橘的 `DISCORD_JU_TOKEN` 环境变量和龙娘的 `token`。也可以为龙娘改用 `token_env`。只允许自己发指令时，把 `allow_from` 改成自己的 Discord User ID。

```bash
team="$HOME/.bothouse/pure-discord/team.toml"
python3 deploy/pure-discord/setup_discord.py apply "$team" --dry-run
python3 deploy/pure-discord/setup_discord.py apply "$team"
```

打开输出中的邀请链接，把两只 Bot 加到同一服务器。添加后再运行一次 `apply`，确认 `Saved: yes` 并处理提示。然后登录龙娘的独立 Codex 工作环境并启动 Bothouse。

```bash
CODEX_HOME="$HOME/.bothouse/codex-home/longniang" codex login
bothouse daemon install
```

在 Discord 里用 `@橘` 或 `@龙娘` 发送第一条消息。已有 daemon 的用户在应用后可运行 `bothouse daemon restart` 载入更新。若希望连本机 Bothouse 管理面板也关闭，可在启动前把 `config.toml` 中的 `[management].enabled` 改为 `false`。

### Apple Silicon 的 MLX JEV

装好 `uv`，从原生 arm64 终端运行以下命令。安装器会按需安装 `mlx-lm`，并注册用户登录时运行的 LaunchAgent。首次启动需要下载模型。

```bash
bash deploy/pure-discord/mac/install-bothouse-mac.sh --with-mlx
```

在清单启用 MLX 的 `[jev]` 示例，模型为 `mlx-community/Qwen3.5-4B-MLX-4bit`，地址为 `http://127.0.0.1:9941/v1`。把 `[gate].mode` 改为 `"smart"` 后重新应用。

### macOS 的 Ollama JEV

Apple Silicon 和 Intel 可按各自的 Ollama 系统要求使用这一路径。安装并启动 Ollama，下载模型，再安装兼容 shim。

```bash
ollama pull qwen3.5:4b
bash deploy/pure-discord/mac/install-bothouse-mac.sh --with-ollama-shim
```

清单中启用 Ollama 的 `[jev]` 示例和 smart 模式，再重新应用。Shim 在 `127.0.0.1:11435` 接收 JEV 请求，再发给默认位于 `127.0.0.1:11434` 的 Ollama。若设置了 `OLLAMA_JEV_TOKEN`，同时启用清单中的 `api_key_env`。

## Windows 快速开始

从仓库根目录用 PowerShell 运行。可以用 `-PythonPath` 选择 Python 3.11+ 解释器，并在后续命令中使用同一解释器。

```powershell
.\deploy\pure-discord\windows\Install-PureDiscord.ps1
python .\deploy\pure-discord\setup_discord.py init-manifest
$team = Join-Path $HOME '.bothouse\pure-discord\team.toml'
notepad $team
python .\deploy\pure-discord\setup_discord.py apply $team --dry-run
python .\deploy\pure-discord\setup_discord.py apply $team
```

清单字段、邀请顺序和 Discord 权限与 macOS 相同。根据输出邀请机器人并重新应用。启动后台服务前，使用独立 Codex 环境登录。

```powershell
$previousCodexHome = $env:CODEX_HOME
$env:CODEX_HOME = Join-Path $HOME '.bothouse\codex-home\longniang'
try { codex login } finally { $env:CODEX_HOME = $previousCodexHome }
bothouse daemon install
```

若要用 smart 模式，安装并启动 Windows 版 Ollama，然后运行以下命令。

```powershell
ollama pull qwen3.5:4b
.\deploy\pure-discord\windows\Install-PureDiscord.ps1 -SkipInstall -InstallOllamaShim
Start-ScheduledTask -TaskName BothouseOllamaJevShim
```

计划任务也会在当前用户下一次登录时启动。可用 `-ShimPort` 和 `-OllamaUrl` 修改端口和上游。脚本可通过 `-ShimToken` 将 Bearer token 存入当前账户的用户环境变量，供后续登录时启动的任务读取。启用清单里的 Ollama `[jev]`、匹配的端口和 smart 模式后重新应用。Discord 需要本机代理时，在 `[discord]` 中配置，例如 `proxy = "http://127.0.0.1:7897"`。

## 路由、角色与头像

- **addressed** 处理 @、回复和显式点名。单纯闲聊让频道保持安静，启动时可以省去 JEV。
- **smart** 也将未点名的消息送到 Gate。JEV 使用 OpenAI 兼容接口评估角色是否该参与。确认 `[gate].enabled` 开启，并用 `doctor` 检查 JEV。
- Codex 角色默认启用 `codex_home_isolate`。套件在 Bothouse 数据目录下创建 `codex-home/<project>`，并让 `sqlite_home` 指向该目录。自定义 `codex_home` 时，请用专门为该角色创建的目录，并用同一目录登录相应 CLI。

```bash
python3 deploy/pure-discord/setup_discord.py personas
python3 deploy/pure-discord/setup_discord.py set-avatars "$team" --role longniang --dry-run
python3 deploy/pure-discord/setup_discord.py set-avatars "$team"
```

Windows 运行同样的子命令时，使用 `python` 或指定的解释器。内置 12 个角色，清单中的 `persona` 可填预设 ID 或自定义文本，也可通过 `persona_file` 载入提示词。自定义头像请用 `avatar_file`，支持 PNG、JPEG 和 WebP。两个文件字段都可使用相对于清单的路径。套件可在应用时上传自定义头像，或在 Bothouse 提示内置头像未设置时回退上传。使用 `apply --no-avatars` 可跳过套件补传。查看各角色及头像的来源。 [角色库](../personas/README.md)

## 诊断与回滚

```bash
python3 deploy/pure-discord/setup_discord.py doctor
python3 deploy/pure-discord/setup_discord.py doctor --jev-url http://127.0.0.1:11435/v1
```

- **邀请或登录提示**。Bothouse 的 `apply --dry-run` 会用清单凭据执行验证。按输出处理 Token、Intent、邀请链接或 CLI 登录问题。`User action required` 时也请查看 `Saved`。
- **Bot 准备就绪但不回复**。检查消息发送者是否在 `allow_from`，频道权限是否齐全，以及 Bot Token 是否分别对应两个应用。在 addressed 模式先试 @ Bot。
- **Gateway 连接失败**。检查 `discord.proxy` 和本机代理进程，再应用并重启 daemon。`doctor` 会显示项目是否启用了代理和确认反应。
- **Smart Gate 无法判断**。用 `doctor` 查看本机 Gate 和模型端点，核对 `[jev]` 的地址、模型名及 Bearer token。已鉴权的 shim 对诊断探测返回 HTTP 401 时，表示端口可达且要求鉴权。
- **头像返回 HTTP 429**。等 Discord 的限流窗口结束后，再单独补传目标角色头像。

回滚前运行 `bothouse daemon stop`。如果要卸载后台服务，运行 `bothouse daemon uninstall`。恢复应用前自行备份的 `config.toml` 或 Bothouse 提供的备份，再按需启动 daemon。若需要撤销 Discord 入口，在服务器移除相应 Bot。若要停止本工具包创建的 JEV 服务，按所用平台执行。

```bash
# macOS。仅运行已安装的服务对应的命令。
launchctl bootout "gui/$(id -u)/com.bothouse.mlx-jev"
launchctl bootout "gui/$(id -u)/com.bothouse.ollama-jev-shim"
```

要停止 macOS 下次登录时自动加载，可在卸载后把对应 plist 移出 `~/Library/LaunchAgents/`。

```powershell
Stop-ScheduledTask -TaskName BothouseOllamaJevShim
Unregister-ScheduledTask -TaskName BothouseOllamaJevShim
```

队伍配置、清单和独立 Codex 目录可留作恢复使用。
