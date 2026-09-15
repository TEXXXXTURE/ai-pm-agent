# [C 2026-09-08] M1 内核骨架 - 节点执行器
"""NodeRunner：按 NodeSpec 执行单个节点（prompt 渲染 -> 模型调用 -> 校验 -> 自检 -> 输出映射）。"""
from typing import Any

from jinja2 import Template

from kernel.exceptions import CheckResult, NodeExecutionError
from kernel.spec import NodeSpec

# [C 2026-09-15 by codebuddy-ds41flash] S046 JSON 解析失败重试反馈文案：
# 与 schema 校验失败共用同一条重试路径、同一个 spec.max_retries 上限
JSON_FORMAT_FEEDBACK = (
    "[输出格式反馈] 上次输出不是合法 JSON（可能被输出长度上限截断），"
    "请只输出完整合法的 JSON；内容较长时压缩字段与描述，不要省略括号或引号"
)


class NodeRunner:
    """节点执行器。

    Args:
        llm: 语言模型调用对象；首版传 None 时使用内置 mock_llm。
        kb: 知识库检索对象；传 None 时跳过检索（预留接口）。
    """

    def __init__(self, llm: Any = None, kb: Any = None):
        self.llm = llm
        self.kb = kb

    # ────────────────── 主入口 ──────────────────
    def run_raw(self, spec: NodeSpec, state: dict) -> dict:
        """执行单个节点，返回校验 + 自检后的**原始结果 dict**（不做 output_mapping）。

        流程：组装上下文 -> 模型调用(含 schema 校验重试) -> 自检(含一次重试)。
        """
        context = self._assemble_context(spec, state)
        result = self._call_model_with_retry(spec, context)
        result = self._run_self_checks(spec, result, context)
        return result

    def run(self, spec: NodeSpec, state: dict) -> dict:
        """执行单个节点，返回经 output_mapping 映射后的 state 更新字典。

        流程：run_raw(组装上下文 -> 模型调用重试 -> 自检重试) -> 输出映射。
        """
        result = self.run_raw(spec, state)
        return self._map_outputs(spec, result)
        # [C 2026-09-09] M6 抽出 run_raw：节点直接拿原始结果 dict，run 保留映射行为

    def run_text(self, spec: NodeSpec, state: dict) -> str:
        """文本模式执行节点，返回模型原文 str（[C 2026-09-09] T1）。

        流程：组装上下文（复用 _assemble_context）-> 文本通道调用模型。
        与 run_raw/run 的区别：**不做 schema 校验、不做 guard 自检、不做输出映射**，
        适用于"模型原生输出 Markdown 全文、文本即产物"的节点（如 T1 的 PRD 生成）。
        """
        context = self._assemble_context(spec, state)
        return self._invoke_model(context, as_text=True)
        # [C 2026-09-09] T1 新增 run_text：文本通道，原文直返

    # ────────────────── 步骤 1：组装上下文 ──────────────────
    def _assemble_context(self, spec: NodeSpec, state: dict) -> str:
        """渲染 Jinja2 prompt 模板，并拼接知识库检索结果（kb=None 时为空串）。"""
        template = Template(spec.prompt_template)
        prompt = template.render(**state)
        kb_text = ""
        if self.kb is not None:
            # 预留接口：kb 对象需提供 retrieve(state) -> str
            try:
                kb_text = self.kb.retrieve(state) or ""
            except Exception:
                kb_text = ""
        if kb_text:
            return f"{prompt}\n\n[知识库检索结果]\n{kb_text}"
        return prompt

    # ────────────────── 步骤 2：模型调用 + schema 校验重试 ──────────────────
    def _call_model_with_retry(self, spec: NodeSpec, context: str) -> dict:
        """调用模型（mock 或真实 llm），失败则带 feedback 重试。

        [C 2026-09-15 by codebuddy-ds41flash] S046 两类失败共用同一重试路径与
        同一 spec.max_retries 上限：
        - JSON 解析失败（NodeExecutionError，node="llm"，如输出被 max_tokens 截断）：
          追加 JSON_FORMAT_FEEDBACK 重试；
        - schema 校验失败：追加 [校验失败反馈] 重试。

        达到 max_retries 仍失败则抛 NodeExecutionError，不静默返回残缺结果。
        """
        last_error: BaseException | None = None
        last_error_was_json = False
        working_context = context
        for attempt in range(spec.max_retries):
            try:
                raw = self._invoke_model(working_context)
            except NodeExecutionError as exc:
                last_error = exc
                last_error_was_json = True
                working_context = f"{working_context}\n\n{JSON_FORMAT_FEEDBACK}"
                continue
            if spec.output_schema is None:
                return raw
            try:
                validated = spec.output_schema.model_validate(raw)
                return validated.model_dump()
            except Exception as exc:
                last_error = exc
                last_error_was_json = False
                working_context = f"{working_context}\n\n[校验失败反馈] {exc}"
        if last_error_was_json:
            message = (
                f"模型输出不是合法JSON，已达最大重试次数 {spec.max_retries}: "
                f"{getattr(last_error, 'message', last_error)}"
            )
        else:
            message = f"模型输出校验失败，已达最大重试次数 {spec.max_retries}"
        raise NodeExecutionError(spec.name, message, last_error)

    def _invoke_model(self, context: str, as_text: bool = False) -> Any:
        """调用模型：llm 非空时用 llm，否则用 mock_llm。

        as_text=False（默认）走 JSON 通道返回 dict；as_text=True 走文本通道返回 str。
        """
        if self.llm is not None:
            return self.llm(context, as_text=as_text)
        return self.mock_llm(context, as_text=as_text)
        # [C 2026-09-09] T1 _invoke_model 透传 as_text

    @staticmethod
    def mock_llm(context: str, as_text: bool = False) -> Any:
        """首版 mock LLM，不调网络。

        JSON 模式固定返回 {"mock_output": "ok"}；
        文本模式（[C 2026-09-09] T1）返回占位 str "mock text output"。
        """
        if as_text:
            return "mock text output"
        return {"mock_output": "ok"}

    # ────────────────── 步骤 3：自检（失败重试一次）──────────────────
    def _run_self_checks(self, spec: NodeSpec, result: dict, context: str) -> dict:
        """执行所有 self_checks；任一失败则带 feedback 重试一次，仍失败抛 NodeExecutionError。"""
        if not spec.self_checks:
            return result

        passed, feedback = self._check_all(spec, result)
        if passed:
            return result

        # 带 feedback 重试一次（仅一次）
        retry_context = f"{context}\n\n[自检失败反馈] {feedback}"
        retried = self._call_model_with_retry(spec, retry_context)
        passed, feedback = self._check_all(spec, retried)
        if passed:
            return retried
        raise NodeExecutionError(
            spec.name,
            f"自检失败，重试一次后仍未通过: {feedback}",
        )

    @staticmethod
    def _check_all(spec: NodeSpec, result: dict) -> tuple[bool, str]:
        """遍历所有 self_checks，返回 (是否全部通过, 失败反馈拼接)。

        兼容两种 guard 返回值：
        - tuple: (passed, feedback)（components/guards 约定）；
        - CheckResult 或任意带 .passed / .feedback 属性的对象。
        """
        feedbacks: list[str] = []
        for check in spec.self_checks:
            cr = check(result)
            if isinstance(cr, tuple):
                passed, feedback = cr[0], cr[1]
            else:
                passed = getattr(cr, "passed", False)
                feedback = getattr(cr, "feedback", "")
            if not passed:
                feedbacks.append(str(feedback))
        if feedbacks:
            return False, "; ".join(feedbacks)
        return True, ""
        # [C 2026-09-09] M6 _check_all 兼容 tuple 与 CheckResult 两种 guard 返回值

    # ────────────────── 步骤 4：输出映射 ──────────────────
    @staticmethod
    def _map_outputs(spec: NodeSpec, result: dict) -> dict:
        """按 spec.output_mapping 将模型输出字段映射为 state 字段。"""
        updates: dict[str, Any] = {}
        for schema_field, state_field in spec.output_mapping.items():
            if schema_field in result:
                updates[state_field] = result[schema_field]
        return updates


# [C 2026-09-08] runner.py 实现完成
