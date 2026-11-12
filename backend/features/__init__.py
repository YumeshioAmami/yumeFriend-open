"""静态功能模块层（v7.20 / D373）。

这里只做三件事：

1. `catalog`：一份**功能清单** —— 每个功能拥有哪些工具 / prompt 块 / 后台任务 /
   数据表 / HTTP 入口 / 界面入口；
2. `flags`：功能的全局开关（存在 `settings.features` JSON 里），带进程内缓存；
3. `events`：功能之间唯一允许的"互相通知"方式（进程内事件总线）。

设计口径：**不做动态加载**。功能在启动时静态注册，关闭只是"不启用"，
不是把代码从进程里卸掉。这样调试、打包、类型检查都和普通代码一样。
"""

from backend.features.catalog import (
    BY_ID,
    CORE_FEATURE_ID,
    FEATURES,
    FeatureSpec,
    feature_for_job,
    feature_for_path,
    feature_for_prompt_block,
    feature_for_tool,
    get_feature,
)

__all__ = [
    "BY_ID",
    "CORE_FEATURE_ID",
    "FEATURES",
    "FeatureSpec",
    "feature_for_job",
    "feature_for_path",
    "feature_for_prompt_block",
    "feature_for_tool",
    "get_feature",
]
