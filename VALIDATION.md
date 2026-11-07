# 开发与验证

MCP 工具入口在 [server.py](src/mobile_use/server.py)，由 [service.py](src/mobile_use/service.py) 管理观察、设备租约和输入。修改导航动作，从 [options.py](src/mobile_use/options.py) 的参数约束和 [navigation.py](src/mobile_use/navigation.py) 的执行逻辑入手；只改给助手的操作说明时，编辑 [Skill](skills/mobile-use/SKILL.md)。具体应用的选择器、页面流程和停留策略应由调用方提供。

在仓库根目录运行。日常改动只跑与改动相关的测试；提交对外发布的候选时跑完整离线套件。它使用测试设备替身，不需要手机，也不能证明真实界面的速度。

```sh
uv sync --frozen --extra test
uv run --frozen --extra test python -m pytest -q
```

**验证 stdio 连接**：运行 `uv run --frozen python scripts/workflow_probe.py`。不传设备序列号时只检查工具发现、服务状态和设备列表。实机只读检查可显式传 `--serial <本次在线设备序列号>`；不要与已持有设备租约的宿主同时运行。

**验证一个动作**：在允许操作的测试页取得最新观察，选一个无副作用的按钮。先用 `dry_run=true` 检查目标，再从最新状态发一次真实动作，检查目标页。另测同名按钮、页面刷新和新弹窗：不能在有歧义时向设备发送输入。多步调用中须逐步检查哪些输入已发送；工具返回错误不等于每一步都没有执行。

**修改动作校验范围**：运行 `python -m pytest -q tests/test_action_scopes.py`。空间动作应允许不相交的边缘窗口变化，同时拦截轨迹上的新窗口、焦点窗口及弹窗；全局返回键不能沿用空间豁免。结构目标可用 `context_texts` 显式绑定稳定事实，未声明的周边状态可以变化，但目标自身内容、声明的价格/身份、歧义与遮挡仍须校验。未使用该参数的调用保留原上下文规则。对比调用与设备读取次数，不把替身结果当作实机耗时。

**报告变慢时**：标明设备连接方式、宿主、计时起止事件和素材是否相同。分列整段墙钟、调用内部耗时、调用间隙、设备截图读取次数和实际交付的图片数。计时从动作发送确认开始，不能把规定的停留时间算成识别开销。分享最小复现时用合成界面或脱敏数据，不上传原始审计文件。

**排查设备异常**：`get_status.trace_dir` 指向本次本地审计目录，首次写入审计或截图时才创建。查看 `actions.jsonl` 中的 `device_read_failed`、`action_unknown`、`sequence_input_unknown` 或 `navigation_input_unknown`：`exception` 包含阶段、类型、有界且经脱敏的原因，以及适用时的 `errno`。工具结果仍以 `input_status` 和已发送步骤为准，不因日志中找到了原因就重放输入。分享前检查脱敏结果，不上传完整请求、输入内容或原始审计。

修改OCR缓存或首帧候选时，针对性运行 `python -m pytest -q tests/test_cached_visual_choices.py tests/test_vision.py tests/test_visual_navigation.py`。检查同帧查询是否复用检测、新标签是否增量识别、新帧是否隔离，以及复核帧新增竞争按钮时是否零输入。记录 `vision` 的检测/识别计数，并与设备截图次数分开；不拿合成页面墙钟代替实机入口时间。

同帧条件批量查询另见 `tests/test_visual_batch.py`：验证一次OCR读取、只读条件无动作ID、前提未满足时不发ID，以及按钮不变但复核帧中的前提消失或产生歧义时零输入。批量条件由调用方命名，通用代码与测试不依赖具体应用；条件及候选使用同一截图，但只有 `require` 列出的条件随点击重新校验。

**提交改动**：向 GitHub 提交代码后，在 [Tests](https://github.com/NingCold/mobile-use/actions/workflows/tests.yml) 查看结果。发往 `main` 的推送和拉取请求会检查锁文件、运行离线测试；实机耗时单独在授权设备上验证。
