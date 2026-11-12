# mock 游戏适配器（P0）

这不是一个游戏，只是 `docs/game_adapter.md` 契约 v1 的**测试替身**：
独立进程 · 本地 HTTP + WebSocket · 一次性 token。

## 跑法

主代码 / 测试会这样启动它（端口和 token 由启动器给）：

```bash
python adapters/mock/mock_adapter.py --port 12345 --token <token> --heartbeat 5
```

## 它能证明什么

- 适配器是**独立进程**，主代码只通过契约跟它说话；
- `start → snapshot → command → event → stop` 全链路能跑通；
- `task_done` / `chat` 事件能实时推到主代码；
- 没有 token / 动作不存在时，会返回**人话**错误。

## 它故意不做什么

- 不连任何游戏、不装任何游戏依赖；
- 不碰数据库 / LLM / 人格 / 记忆；
- 不含任何游戏逻辑（"挖矿"只是睡 50ms 然后报完成）。
