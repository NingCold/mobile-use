# 开发与验证

MCP 工具入口在 [server.py](src/mobile_use/server.py)，由 [service.py](src/mobile_use/service.py) 管理观察、设备租约和输入。修改导航动作，从 [options.py](src/mobile_use/options.py) 的参数约束和 [navigation.py](src/mobile_use/navigation.py) 的执行逻辑入手；只改给助手的操作说明时，编辑 [Skill](skills/mobile-use/SKILL.md)。具体应用的选择器、页面流程和停留策略应由调用方提供。

在仓库根目录运行。日常改动只跑与改动相关的测试；提交对外发布的候选时跑完整离线套件。它使用测试设备替身，不需要手机，也不能证明真实界面的速度。

```sh
uv sync --frozen --extra test
uv run --frozen --extra test python -m pytest -q
```

**验证 stdio 连接**：运行 `uv run --frozen python scripts/workflow_probe.py`。不传设备序列号时只检查工具发现、服务状态和设备列表。实机只读检查可显式传 `--serial <本次在线设备序列号>`；不要与已持有设备租约的宿主同时运行。

**验证一个动作**：在允许操作的测试页取得最新观察，选一个无副作用的按钮。先用 `dry_run=true` 检查目标，再从最新状态发一次真实动作，检查目标页。另测同名按钮、页面刷新和新弹窗：不能在有歧义时向设备发送输入。多步调用中须逐步检查哪些输入已发送；工具返回错误不等于每一步都没有执行。

**报告变慢时**：标明设备连接方式、宿主、计时起止事件和素材是否相同。分列整段墙钟、调用内部耗时、调用间隙、设备截图读取次数和实际交付的图片数。计时从动作发送确认开始，不能把规定的停留时间算成识别开销。分享最小复现时用合成界面或脱敏数据，不上传原始审计文件。

**提交改动**：向 GitHub 提交代码后，在 [Tests](https://github.com/NingCold/mobile-use/actions/workflows/tests.yml) 查看结果。发往 `main` 的推送和拉取请求会检查锁文件、运行离线测试；实机耗时单独在授权设备上验证。
