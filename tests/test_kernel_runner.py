# runner JSON 解析失败重试自测
"""NodeRunner._call_model_with_retry 的 JSON 失败重试测试（零网络、零真实 API）。

覆盖任务书第三节口径 3 与第二节「数据流/失败边界」：

- JSON 解析失败（NodeExecutionError，node="llm"）与 schema 校验失败共用同一条重试路径、
  同一个 spec.max_retries 上限；
- 一次失败一次成功 → 返回结果，且第二次调用的上下文中逐字包含口径 3 的反馈文案；
- 两次都失败（或 max_retries=1）→ 抛 NodeExecutionError（node=节点名），不返回残缺结果；
- output_schema 为 None 时 JSON 失败同样重试；
- schema 校验失败重试路径保持不变（回归）；
- run_text 文本通道不做重试（口径 4：不改）；
- 端到端：真实 build_llm 闭包（extract_json 报截断）+ 假 ChatLiteLLM，重试后恢复成功。

运行（cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python -m pytest tests/test_kernel_runner.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Windows GBK 控制台兜底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from kernel.exceptions import NodeExecutionError  # noqa: E402
from kernel.model import build_llm  # noqa: E402
from kernel.runner import JSON_FORMAT_FEEDBACK, NodeRunner  # noqa: E402
from kernel.spec import NodeSpec  # noqa: E402

# 口径 3 原文（独立硬编码，避免与实现常量同源而失去校验意义）
EXPECTED_FEEDBACK = (
    "[输出格式反馈] 上次输出不是合法 JSON（可能被输出长度上限截断），"
    "请只输出完整合法的 JSON；内容较长时压缩字段与描述，不要省略括号或引号"
)

TRUNCATED_JSON = (
    '{   "readiness": "needs_clarification",   "readiness_notes": "已按纵切成 15 张'
    "可演示工单：P0=导入、立场确认、切分定位；P1=多合同对照、留痕。其中 4 张 HITL"
    "工单在开工前需要人拍板：I2"
)


class _OutSchema(BaseModel):
    value: int


class SequenceLLM:
    """按次序返回预制结果或抛异常的假模型（零网络、零花费）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _json_error() -> NodeExecutionError:
    """模拟 build_llm 闭包里 extract_json 的截断报错（node="llm"）。"""
    return NodeExecutionError(
        "llm", "模型返回非JSON（疑似被 max_tokens 截断，原始长度 300）: {   \"readiness\""
    )


def _spec(**overrides) -> NodeSpec:
    kwargs = {"name": "test_node", "prompt_template": "测试提示词", "max_retries": 2}
    kwargs.update(overrides)
    return NodeSpec(**kwargs)


# ────────────────────────── 1. 口径 3：JSON 失败重试 ──────────────────────────


class TestJsonFailureRetry(unittest.TestCase):
    def test_feedback_text_matches_spec(self):
        self.assertEqual(JSON_FORMAT_FEEDBACK, EXPECTED_FEEDBACK)

    def test_fail_once_then_success_returns_result(self):
        llm = SequenceLLM([_json_error(), {"ok": 1}])
        runner = NodeRunner(llm=llm)
        result = runner.run_raw(_spec(), {})
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(len(llm.calls), 2)

    def test_retry_context_appends_feedback_verbatim(self):
        llm = SequenceLLM([_json_error(), {"ok": 1}])
        NodeRunner(llm=llm).run_raw(_spec(), {})
        second_prompt = llm.calls[1]["prompt"]
        self.assertIn(EXPECTED_FEEDBACK, second_prompt)
        self.assertIn("测试提示词", second_prompt)
        # 首次调用不得带反馈
        self.assertNotIn("输出格式反馈", llm.calls[0]["prompt"])

    def test_fail_twice_raises_node_execution_error(self):
        llm = SequenceLLM([_json_error(), _json_error()])
        runner = NodeRunner(llm=llm)
        with self.assertRaises(NodeExecutionError) as ctx:
            runner.run_raw(_spec(), {})
        self.assertEqual(ctx.exception.node_name, "test_node")
        self.assertEqual(len(llm.calls), 2)  # 不超 spec.max_retries 上限
        self.assertIsInstance(ctx.exception.cause, NodeExecutionError)

    def test_max_retries_one_raises_after_single_call(self):
        llm = SequenceLLM([_json_error()])
        runner = NodeRunner(llm=llm)
        with self.assertRaises(NodeExecutionError):
            runner.run_raw(_spec(max_retries=1), {})
        self.assertEqual(len(llm.calls), 1)

    def test_json_retry_works_without_output_schema(self):
        llm = SequenceLLM([_json_error(), {"mock_output": "ok"}])
        result = NodeRunner(llm=llm).run_raw(_spec(output_schema=None), {})
        self.assertEqual(result, {"mock_output": "ok"})
        self.assertEqual(len(llm.calls), 2)

    def test_error_message_mentions_json_and_retry_limit(self):
        llm = SequenceLLM([_json_error(), _json_error()])
        with self.assertRaises(NodeExecutionError) as ctx:
            NodeRunner(llm=llm).run_raw(_spec(), {})
        msg = ctx.exception.message
        self.assertIn("不是合法JSON", msg)
        self.assertIn("已达最大重试次数 2", msg)


# ────────────────────────── 2. schema 重试路径回归 ──────────────────────────


class TestSchemaRetryUnchanged(unittest.TestCase):
    def test_bad_schema_then_valid_returns(self):
        llm = SequenceLLM([{"value": "not-an-int"}, {"value": 7}])
        result = NodeRunner(llm=llm).run_raw(
            _spec(output_schema=_OutSchema), {}
        )
        self.assertEqual(result, {"value": 7})
        self.assertIn("[校验失败反馈]", llm.calls[1]["prompt"])
        self.assertEqual(len(llm.calls), 2)

    def test_bad_schema_twice_raises_old_message(self):
        llm = SequenceLLM([{"value": "x"}, {"value": "y"}])
        with self.assertRaises(NodeExecutionError) as ctx:
            NodeRunner(llm=llm).run_raw(_spec(output_schema=_OutSchema), {})
        self.assertEqual(
            ctx.exception.message, "模型输出校验失败，已达最大重试次数 2"
        )

    def test_mock_llm_path_untouched(self):
        """llm=None 走内置 mock_llm，行为不变。"""
        result = NodeRunner().run_raw(_spec(output_schema=None), {})
        self.assertEqual(result, {"mock_output": "ok"})


# ────────────────────────── 3. 口径 4：run_text 不重试 ──────────────────────────


class TestRunTextUnchanged(unittest.TestCase):
    def test_run_text_does_not_retry(self):
        llm = SequenceLLM([_json_error(), "不应被调用"])
        runner = NodeRunner(llm=llm)
        with self.assertRaises(NodeExecutionError):
            runner.run_text(_spec(), {})
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(llm.calls[0]["as_text"])


# ────────────────────────── 4. 端到端：真实闭包 + 假 ChatLiteLLM ──────────────────────────


class _QueuedChat:
    """假 ChatLiteLLM：按队列返回文本内容（不建网络连接）。"""

    queue: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def invoke(self, messages):
        return SimpleNamespace(content=_QueuedChat.queue.pop(0))


class TestEndToEndTruncatedThenRecovered(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "llm": {
                "default_provider": "fake-local",
                "temperature": 0.3,
                "max_retries": 2,
                "max_tokens": 8192,
                "providers": {
                    "fake-local": {
                        "litellm_model": "ollama/qwen2.5",
                        "api_key_env": "",
                    }
                },
            }
        }

    def test_truncated_output_retried_by_real_extract_json(self):
        _QueuedChat.queue = [TRUNCATED_JSON, '{"ok": true}']
        with patch("kernel.model.ChatLiteLLM", _QueuedChat):
            llm = build_llm(self._config())
            runner = NodeRunner(llm=llm)
            result = runner.run_raw(_spec(), {})
        self.assertEqual(result, {"ok": True})

    def test_truncated_output_twice_raises(self):
        _QueuedChat.queue = [TRUNCATED_JSON, TRUNCATED_JSON]
        with patch("kernel.model.ChatLiteLLM", _QueuedChat):
            llm = build_llm(self._config())
            runner = NodeRunner(llm=llm)
            with self.assertRaises(NodeExecutionError) as ctx:
                runner.run_raw(_spec(), {})
        self.assertEqual(ctx.exception.node_name, "test_node")
        self.assertIn("截断", str(ctx.exception.cause))


if __name__ == "__main__":
    unittest.main(verbosity=2)


