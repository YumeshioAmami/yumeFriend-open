"""LLM 层错误（LLM协议§9）。

- LLMConfigError：配置问题（没配、缺 key）→ 不该重试，直接告诉用户去设置里改
- LLMCallError：调用失败（4xx/5xx/网络）→ 上层可重试，最后转成 error 事件
"""


class LLMError(Exception):
    """LLM 层错误基类。"""


class LLMConfigError(LLMError):
    """配置缺失或非法（例如没选 provider、没填 api_key）。"""


class LLMCallError(LLMError):
    """调用 LLM 失败（HTTP 错误 / 网络错误 / 响应格式异常）。"""
