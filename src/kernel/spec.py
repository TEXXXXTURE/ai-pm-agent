# [C 2026-09-08] M1 内核骨架 - 节点规格定义
"""NodeSpec：描述单个节点的执行规格（prompt / schema / 映射 / 自检 / 重试）。"""
from dataclasses import dataclass, field
from typing import Any, Callable

from kernel.exceptions import CheckResult


@dataclass
class NodeSpec:
    """单个 LangGraph 节点的执行规格。

    Attributes:
        name: 节点名称（唯一标识）
        prompt_template: Jinja2 模板字符串，首版直接传字符串
        output_schema: Pydantic Model 类，用于结构化输出校验；None 表示跳过校验
        output_mapping: schema 字段 -> state 字段的映射，如 {"completeness": "info_completeness"}
        self_checks: guard 函数列表，签名 (result_dict) -> CheckResult
        is_hitl: 是否人机交互节点
        max_retries: 最大重试次数（含首次调用，默认 2）
    """

    name: str
    prompt_template: str = ""
    output_schema: type | None = None
    output_mapping: dict[str, str] = field(default_factory=dict)
    self_checks: list[Callable[[dict[str, Any]], CheckResult]] = field(default_factory=list)
    is_hitl: bool = False
    max_retries: int = 2


# [C 2026-09-08] spec.py 实现完成
