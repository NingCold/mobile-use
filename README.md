# Mobile Use

让支持 MCP 的助手操作一台已授权的 Android 真机或模拟器。比如「打开计算器，算出 37 × 12」：助手先从控件树找到按键；只有界面没有提供足够的文字或控件时，才需要看截图。对于已经走过的路径，可以把几步导航交给本机一次执行。

## 接上第一台手机

**让 AI 帮你安装**：把[给 AI 的安装说明](docs/install-for-agents.md)交给助手，说「请按这份说明安装 Mobile Use，用只读调用验证服务」。手动安装按下面操作。

准备好 [uv](https://docs.astral.sh/uv/) 和 [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)。手机开启 USB 调试并接受电脑的授权；无线设备先用 ADB 配对、连接。终端运行 `adb devices -l`，确认目标设备状态为 `device`。

克隆仓库并安装锁定的依赖：

```sh
git clone https://github.com/NingCold/mobile-use.git
cd mobile-use
uv sync --frozen
```

**使用 Codex**：在刚克隆的目录下打开 PowerShell，注册本地服务。命令保存绝对路径，因此以后从其他项目打开聊天也能使用同一份安装。

```powershell
$project = (Resolve-Path .).Path
codex mcp add mobile-use --env PYTHONIOENCODING=utf-8 -- uv run --directory "$project" --frozen mobile-use
```

让宿主重新加载 MCP 或打开新聊天，调用 `get_status` 查看运行中服务的能力。其他支持 stdio 的宿主也可以使用同一条 `uv run --directory <项目绝对路径> --frozen mobile-use` 启动命令。可选的 [Mobile Use Skill](skills/mobile-use/SKILL.md) 说明助手该如何选择观察和动作；它不能代替服务注册。

先给助手一个只读任务：

> 用 Mobile Use 列出在线设备。连接我的手机，告诉我当前应用和页面上的主要控件，不要点击；然后断开连接。

如果列出了多台设备，指定本次要用的序列号。设备需保持解锁。一次只有一个 Mobile Use 会话可以控制同一序列号；结束时调用 `disconnect` 释放占用，不会关闭应用或断开无线 ADB。

## 怎么操作既快又稳

- **有文字的控件**：用 `observe(detail="brief", image="auto")` 看当前页，再把最新的 `observation_id` 和已看到的 `element_id` 交给 `tap`。明确身份的按钮可在位置变化后重新定位；不会因为旁边的倒计时变了就把整页判为失效。
- **自绘页面**：先看图。已看清文字和所属卡片时，可用[本地 OCR 定位](skills/mobile-use/references/visual-targets.md)；遇到多个匹配、遮挡或窗口变化时停止。不要猜坐标。
- **已知路径**：用 [navigate](skills/mobile-use/references/navigation.md) 声明允许的应用、页面条件和目标。刷新会移动按钮时，只等目标位置稳定。不要在每次切页后机械地追加一次观察。
- **同一动作有几种按钮文案**：如果已知文档页可能写着“打开”或“预览”，而两者都是本次允许的操作，可声明[一组精确候选](skills/mobile-use/references/navigation.md#when-a-known-page-has-alternate-button-labels)。服务在同一画面找全部候选；只有唯一匹配且复核通过才点击。两者同时出现时交回判断。
- **异常或中断**：先看动作是否已经发送。已发送或结果未知时，核对当前状态，不要重放整段。需要更多文字时，先用 `get_observation` 展开最近一次的缓存；它不刷新屏幕，也不会延长观察有效期。

已有截图时，可以在 `get_observation.visual_query_any_of` 中一次声明全部已知按钮文案与上下文，从首帧选择唯一入口。同一帧、同一区域会共享OCR检测与已识别文字；点击前仍检查新画面中是否出现竞争按钮或遮挡。参数与结果见[首帧候选比较](skills/mobile-use/references/visual-targets.md#compare-alternatives-on-the-first-returned-frame)。

如果还需要判断页面条件，例如“文档已就绪”时才能打开，使用[同帧多条件查询](skills/mobile-use/references/visual-targets.md#query-conditions-and-actions-together)：一次返回多项条件及按钮候选，将点击前提写入 `require` 后，按钮和前提会在点击前一起复核。条件名称与分支含义由任务流程定义。

## 数据留在哪里

控件解析、OCR 和手势执行在本机完成；工具返回的文字和图片会进入所用 MCP 宿主。默认不把观察截图写入磁盘。仓库安装的审计记录默认在 `artifacts/`，可能包含界面文字、设备标识和操作轨迹；调试脚本还可能主动保存图片。不要把这些文件直接贴到公开 issue。服务可用 `--artifacts` 指定私有记录目录。

[开发与验证](VALIDATION.md) · [MIT 许可证](LICENSE)
