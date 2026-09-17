# Sue 快速开始

现在可以先零成本试运行：在 Shared-Memory 目录执行 python3 orchestrator/cli.py run "你的问题" --workers 3。它会生成一套模拟结果，用来检查流程，不是真实研究答案。

若要真的让 Luna 和 Sol 工作，先执行 python3 orchestrator/cli.py --backend app-server doctor，看见 worker=Luna、leader=Sol 后，再针对一个范围很小的问题运行真实模式。真实模式每一步都会消耗 Codex 使用量；本机极短回合也约计 1.3 万 token（多数输入被缓存），所以建议先和我确定测试题目及预算。

完成后用 status 查看任务 ID、result 查看结果、logs 看过程。任何标记为 unverified 的事实都不能当成已核实。这里的“多个聊天”是后台独立会话，不会自动打开六个窗口。没有执行 iCloud 备份。
