# 给 AI 的安装说明

目标：让用户指定的 MCP 宿主启动本地 Mobile Use，并通过 **宿主里的** `get_status` 验证。安装本身不需要连接或操作手机。以下命令在用户的电脑上运行；每步成功才继续。

### 1. 选择安装位置

使用用户选定的持久目录，不要放在临时目录。已有本仓库就复用，检查 `pyproject.toml` 的项目名为 `mobile-use`、同级有 `uv.lock` 和 `src/mobile_use/server.py`。不要重置、覆盖已有修改或自动更新版本。全新安装时，在目标父目录执行：

```sh
git clone https://github.com/NingCold/mobile-use.git
cd mobile-use
```

### 2. 安装并验证入口

在仓库根目录执行：

```sh
uv --version
uv sync --frozen
uv run --frozen mobile-use --help
```

成功信号：同步正常退出，帮助列出 `--artifacts`。缺少 `uv` 时先按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/) 配置，再继续。依赖同步失败时保留锁文件，报告第一个具体错误，不另换依赖版本。不要单独启动一个常驻服务：stdio 进程由宿主启动。

### 3. 注册到用户指定的宿主

**Codex：**先运行 `codex mcp get mobile-use --json`。已有条目指向同一安装且能工作就复用（包括直接用项目虚拟环境启动的条目）；若指向不同目录或服务，说明冲突，先让用户选择。仅在确认没有该条目时添加。在仓库根目录按当前 shell 选一组：

PowerShell：

```powershell
$project = (Resolve-Path .).Path
$uv = (Get-Command uv -CommandType Application | Select-Object -First 1).Source
codex mcp add mobile-use --env PYTHONIOENCODING=utf-8 -- "$uv" run --directory "$project" --frozen mobile-use
codex mcp get mobile-use --json
```

macOS / Linux：

```sh
codex mcp add mobile-use --env PYTHONIOENCODING=utf-8 -- "$(command -v uv)" run --directory "$(pwd -P)" --frozen mobile-use
codex mcp get mobile-use --json
```

核对返回的启动程序、绝对项目路径和参数。[Codex 的 MCP 配置说明](https://developers.openai.com/codex/mcp)有其他配置方式。**其他支持 stdio 的宿主**：按其注册方式填写同一启动程序和参数；不要覆盖整份配置。对应的值为：

```json
{
  "command": "<uv 可执行文件的绝对路径>",
  "args": ["run", "--directory", "<仓库的绝对路径>", "--frozen", "mobile-use"],
  "env": {"PYTHONIOENCODING": "utf-8"}
}
```

替换两个占位符；路径即使有空格也是单个参数。可选的 [Skill](../skills/mobile-use/SKILL.md) 是操作手册，不是安装依赖。

### 4. 在实际宿主内验收

1. 重新加载该 MCP 或新开聊天，使宿主读取配置。调用 Mobile Use 的 `get_status()`；能读到 `server_version` 和 `capabilities` 即表示已加载。`connected=false` 是正常的。若安装助手无权重载或看不到工具，报告“已注册，待宿主验证”，不要把 CLI 配置回显当成运行成功。
2. **仅在用户还要验证设备时**，确认 [Platform Tools](https://developer.android.com/tools/releases/platform-tools) 中的 `adb` 可用，调用 `list_devices()`。空列表表示暂时没有在线设备，不必重装软件；`unauthorized` 时由用户在设备上确认调试授权。多个设备需确认目标；不猜无线端口、不连接未指定设备。
3. 若用户授权只读查看该设备，按 `connect(serial)` → `observe(detail="brief", image="never")` → `disconnect()` 执行，成功连接后即使读取失败也释放租约。不用点击、输入、解锁或截图来证明安装成功。

向用户报告安装路径、宿主是否已验证，以及设备是否另行验证；若未完成，指出卡在哪一步。
