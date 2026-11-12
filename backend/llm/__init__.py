"""LLM 抽象层（LLM协议§1）。

上层（agent）只认 ILLMProvider 接口，**不感知具体厂商** ——
所以代码里永远不写 `if provider == "deepseek"` 这类分支（D13）。
"""
