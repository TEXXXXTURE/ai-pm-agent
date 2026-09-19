# 内核骨架 - 异常与自检结果
"""节点执行异常与自检结果定义。"""
from dataclasses import dataclass


class NodeExecutionError(Exception):
    """节点执行失败异常。

    携带 node_name（哪个节点失败）、message（失败描述）、cause（原始异常或原因）。
    """

    def __init__(self, node_name: str, message: str, cause: BaseException | str | None = None):
        self.node_name = node_name
        self.message = message
        self.cause = cause
        super().__init__(f"[{node_name}] {message}" + (f" (cause: {cause})" if cause else ""))


@dataclass
class CheckResult:
    """自检结果。

    passed: 是否通过
    feedback: 不通过时的反馈信息（通过时可为空串）
    """

    passed: bool
    feedback: str = ""


