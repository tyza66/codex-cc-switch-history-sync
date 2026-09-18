# 基于 CC Switch 的 Codex 会话同步工具

用于 **Codex 聊天记录同步**、**Codex 历史会话同步**、**Codex 会话记录同步** 的跨平台小工具（Windows / macOS / Linux）。它可以在使用 cc-switch 切换官方 / 中转 provider 时，尽量保持同一套本地 Codex 会话历史可见。

> English: see [README.en.md](README.en.md).

## 本次更新目标

本次更新将原本只适配 Windows 的 PowerShell 实现，整体替换为一套**纯 Python 标准库**的跨平台实现（Windows / macOS / Linux 共用同一套代码）：

- 修复同步窗口偶尔未真正执行，导致历史记录没有完成同步。
- 防止旧 provider 配置将当前 GPT-5.6 模型回退为 GPT-5.5 / GPT-5.4。
- 修复 Codex Desktop 使用过期登录缓存后出现的 `401 Unauthorized` / `token_expired`。
- 区分官方与中转 provider，避免模型配置和登录状态互相覆盖。
- 兼容各平台 Codex Desktop / Codex CLI 的进程名与启动方式。

更新后的自动流程为：检测 provider 变化 → 关闭 Codex → 按 provider 类型清理必要缓存 → 同步配置与历史记录 → 重新启动 Codex。

## 关键词

如果你在搜索这些问题，这个工具可能适用：

- Codex 聊天记录同步
- Codex 历史会话同步
- Codex 会话记录同步
- Codex conversations / sessions / history sync
- cc-switch 切换中转后 Codex 历史丢失
- Codex 切换 provider 后历史会话不可见
- Codex 本地会话恢复 / session_index 修复
- `~/.codex/sessions`、`state_5.sqlite`、`session_index.jsonl` 修复

## 解决什么问题

Codex 的本地历史会同时依赖 rollout 文件、`state_5.sqlite` 和 `session_index.jsonl`。使用 cc-switch 切换不同 Codex provider 后，历史可能因为 `model_provider` 不一致而分裂或不可见。

这个工具会：

- 监听 cc-switch 的当前 Codex provider 变化。
- 运行时扫描 cc-switch 中现有的 Codex provider：官方 OpenAI 保持 `openai`，其余中转统一写成 `ccs`。
- 修复 Codex 本地历史索引和 SQLite 状态。
- 兼容新版 Codex 的多数据库布局：`~/.codex/sqlite/*.db`（`threads` / `local_thread_catalog` / `messages` / `sessions` 等），而不只是旧的 `state_5.sqlite`。
- 修复 `local_thread_catalog` 侧边栏目录（补缺失行、删除子代理行）。
- 归一化 `.codex-global-state.json`（工作区根目录 / 线程 ID / 路径去重）。
- 清理历史里的模型窗口后缀 `gpt-5.5[1M]` → `gpt-5.5`（`threads.model` 与 `logs_2.sqlite`）。
- 保留当前 Codex 顶层模型默认值，避免 cc-switch 旧 provider 配置把 `gpt-5.6-*` 回退成旧模型。
- 切到中转 provider 时清理会导致 `token_expired` 的 Codex Desktop 过期 web 登录缓存。
- 删除会覆盖 `auth.json` 的用户级 `CODEX_API_KEY` 环境变量。
- 切换后弹出一个简洁进度窗口（tkinter，有图形界面；无图形环境时自动退化为控制台进度条）。
- 成功后自动启动 Codex。
- 每天首次同步前自动备份关键状态。

## 支持范围

- Windows / macOS / Linux
- OpenAI Codex Desktop / Codex CLI
- cc-switch
- Python 3.8+（仅标准库；进度弹窗可选使用 tkinter，缺失时自动退化）

## 安装

在仓库根目录运行：

```bash
python3 main.py install
```

安装脚本会：

- 每天首次同步前备份关键状态到 `~/.codex/history-sync-tool-backups/install_<timestamp>`
- 注册自启动项：
  - Windows：`Startup` 目录下的 `Codex History Sync.vbs`（用 `pythonw.exe` 隐藏启动）
  - macOS：`~/Library/LaunchAgents/com.codex.history-sync.plist`
  - Linux：`~/.config/autostart/codex-history-sync.desktop`
- 立即启动后台 watcher

> 也可以 `pip install -e .` 后使用 `codex-history-sync` 命令代替 `python3 main.py`。

## 使用

安装后正常使用 cc-switch。点击某个 Codex provider 的“启用”后：

1. watcher 检测到 provider 变化；
2. 弹出同步进度窗口；
3. 自动关闭正在运行的 Codex，避免旧 provider / 旧 token 状态残留；
4. 如果目标是中转 provider，清理 Codex Desktop 过期 web 登录缓存；
5. 自动同步会话历史；
6. 同步完成后自动启动 Codex。

手动同步（带进度/确认 UI）：

```bash
python3 main.py run
```

手动无 UI 同步：

```bash
python3 main.py sync
```

只修复本地历史索引（不碰 cc-switch provider 配置，自动识别官方/中转）：

```bash
python3 main.py repair            # 完整修复（自动从 cc-switch 识别目标 provider）
python3 main.py repair --dry-run  # 只预览会改什么，不写入
```

只读诊断当前历史状态（不改任何文件）：

```bash
python3 main.py doctor
```

全方位检查并自动修复整个 Codex 本地状态（config.toml、cc-switch DB、rollout 元数据、state 数据库、侧边栏目录、全局状态、模型后缀、session_index）：

```bash
python3 main.py fix
```

打开图形界面主窗口（带「立即同步 / 诊断 / 安装自启动 / 导出记录 / 导入记录 / Codex修复」按钮）：

```bash
python3 main.py gui     # 直接运行不带子命令也会打开
```

> 双击 macOS 的 `CodexHistorySync.app` 会打开这个窗口。

导出聊天记录到 zip（默认文件名带时间戳，可自定义保存位置；可选导出的内容）：

```bash
python3 main.py export                                    # 导出全部到 ~/Desktop/codex-history-<时间戳>.zip
python3 main.py export -o ~/backup/my-history.zip         # 自定义路径和文件名
python3 main.py export --include sessions,session_index.jsonl  # 只导出部分
```

从 zip 加载聊天记录（导入前先校验 zip 内部目录结构，不合法则拒绝；自动备份当前状态）：

```bash
python3 main.py import export.zip              # 校验并导入
python3 main.py import --dry-run export.zip    # 仅校验，不写入
python3 main.py import --include sessions export.zip  # 只导入部分
python3 main.py import --no-backup export.zip  # 跳过自动备份（谨慎）
```

手动运行后台 watcher：

```bash
python3 main.py watch
```

## 打包发布（GitHub Actions）

推一个 `v*` 版本 tag 会自动在三个平台各打一个单文件可执行包，并上传到对应的 GitHub Release：

```bash
git tag v1.0.20260917
git push origin v1.0.20260917
```

产物（`.github/workflows/release.yml`）：

| 平台 | 产物 |
|---|---|
| Windows | `codex-history-sync-v*-windows-x64.exe`（免安装单文件） |
| macOS | `codex-history-sync-v*-macos-arm64.dmg`（Apple Silicon，内含 ad-hoc 签名的 `.app`） |
| Linux | `codex-history-sync-v*-linux-x64.AppImage` |

- `-rc` / `-beta` / `-alpha` tag 会自动标记为 pre-release。
- Linux AppImage 首次运行前需 `chmod +x`；macOS `.dmg` 双击后把 `.app` 拖入「应用程序」。
- macOS 为 ad-hoc 签名（无 Apple 开发者证书），首次运行若被 Gatekeeper 拦截，右键「打开」或 `xattr -dr com.apple.quarantine <app>`；Windows 未签名可能触发 SmartScreen，点「仍要运行」即可。

所有命令都支持 `--codex-home <dir>` 覆盖 Codex 目录（默认 `~/.codex`）。

## 恢复 / 卸载

只停用工具，不恢复数据库：

```bash
python3 main.py uninstall
```

或等价地：

```bash
python3 main.py restore
```

恢复最近一次安装备份（会提示输入 `RESTORE` 确认）：

```bash
python3 main.py restore --restore-latest-backup
```

## 风险说明

这个工具会修改 Codex 本地状态索引，包括：

- `config.toml`
- `session_index.jsonl`
- `state_5.sqlite`
- cc-switch 的 Codex provider 配置
- Codex Desktop 的 Chromium web 登录缓存（仅中转 provider 自动切换时）
- 用户级 `CODEX_API_KEY` 环境变量覆盖项

它不会上传任何数据，也不会把你的会话同步到云端。所有处理都在本机完成。

注意：

- 中转历史会统一标记为 `ccs`，不会保留每条历史原始中转名；中转列表来自 cc-switch 运行时扫描，不依赖固定服务商名单。
- 自动切换 provider 时会关闭 Codex，正在运行的任务可能会被中断。
- 工具不会备份 `auth.json`，也不会复制 API key 或登录 token；web 缓存清理前只做本地备份。
- 直接切换到官方的途径登陆可能会导致官方的会话丢失。
- 不要把自己的 `.codex`、`.cc-switch`、备份、`auth.json`、SQLite 数据库提交到 GitHub。

## 平台差异说明

- **进度窗口**：优先用 tkinter 弹窗；无图形环境（或 Python 未编译 tkinter）时自动退化为控制台进度条。macOS 上 Homebrew 的 `python@3.13` 默认不含 tkinter，如需弹窗可 `brew install python-tk@3.13`。
- **自启动**：Windows 用 Startup 目录 `.vbs`，macOS 用 LaunchAgent，Linux 用 XDG autostart `.desktop`。
- **Codex Desktop 缓存清理**：各平台候选路径不同，均为尽力而为扫描；找不到对应目录时安全跳过。
- **旧版 Windows PowerShell 脚本**：保留在 `scripts/` 目录作为 legacy 参考，新实现不再依赖它们。

## 不包含什么

- 不修改 cc-switch 本体。
- 不提供健康检查脚本。
- 不清理 Codex 的 `logs_2.sqlite` 运行日志。
- 不处理跨设备同步。

## 图标

应用图标来自 [cc-switch](https://github.com/farion1231/cc-switch)（[ccswitch.io](https://www.ccswitch.io/zh/)）。

## License

MIT
全方位检查并自动修复整个 Codex 本地状态（config.toml、cc-switch DB、rollout 元数据、rollout 首行修复、state 数据库、跳过列表清理、侧边栏目录、全局状态、模型后缀、session_index）：
