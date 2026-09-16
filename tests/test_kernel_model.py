# [C 2026-09-15 by codebuddy-ds41flash] S046 extract_json 截断识别与 max_tokens 透传自测
"""kernel.model 的截断识别 + max_tokens 透传测试（零网络、零真实 API）。

覆盖任务书第三节口径 2、口径 4 与第二节接口约定：

口径 2（截断识别）：
- "{" 与 "}" 数量不配对 → 疑似截断；
- 字符串引号未闭合（括号配对） → 疑似截断；
- 两种情况的错误文案为
  `模型返回非JSON（疑似被 max_tokens 截断，原始长度 N）: <原文前 200 字>`，
  且 node_name 仍为 "llm"；
- 不满足截断特征时维持旧文案 `模型返回非JSON: <原文前 200 字>`；
- 空响应仍沿用 `模型返回非JSON: 响应内容为空`。

第二节接口（max_tokens 透传）：
- build_llm / build_chat 在 max_tokens 非 None 时透传给 ChatLiteLLM；
- max_tokens 为 None 时不传该参数（向后兼容）。

口径 4（不改的部分）：围栏剥离、首尾 {} 截取、正常 JSON 解析行为保持不变。

[S048 修复B] 空内容报错说明追加：
- 空正文且 finish_reason == "length" -> 原句后补可执行提示（疑似隐藏思考占满输出额度…）；
- 空正文但响应无截断信号 -> 文案逐字不变；
- 空正文但 additional_kwargs 带 reasoning 段 -> 同样补提示；
- 正文非空时新参数不影响解析（提示只在为空分支生效）。

运行（cwd=项目根）：
  $env:PYTHONPATH="src"
  C:\\Users\\A\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe -m pytest tests/test_kernel_model.py -v
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

from kernel.exceptions import NodeExecutionError  # noqa: E402
from kernel.model import (  # noqa: E402
    EMPTY_CONTENT_HINT,
    build_chat,
    build_llm,
    extract_json,
)

# 真机现象原文片段（拆工单节点被截断的真实输出形态：字符串中途断在 I2）
REAL_TRUNCATED = (
    '{   "readiness": "needs_clarification",   "readiness_notes": "整体方向与范围来自'
    "已评审的 PRD，已按纵切成 15 张可演示工单：P0=导入、立场确认、切分定位、风险判定、"
    "建议、逐条确认、导出与注入/越权/数据隔离硬约束；P1=多合同对照、留痕、上下文、"
    "评测接入、调用链与业务指标埋点。其中 4 张 HITL 工单在开工前需要人拍板：I2"
)

TRUNCATION_PREFIX = "模型返回非JSON（疑似被 max_tokens 截断，原始长度 "


class _FakeChat:
    """假 ChatLiteLLM：记录构造 kwargs，invoke 返回预制文本内容。"""

    last_kwargs: dict | None = None
    next_content: str = '{"status": "ok"}'

    def __init__(self, **kwargs):
        _FakeChat.last_kwargs = kwargs
        self.kwargs = kwargs

    def invoke(self, messages):
        return SimpleNamespace(content=_FakeChat.next_content)


class _FakeChatWithMeta:
    """假 ChatLiteLLM：invoke 返回带响应元数据的预制响应对象（S048 修复B 用）。

    响应形态对齐 langchain AIMessage：content / response_metadata / additional_kwargs。
    """

    next_response: object = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def invoke(self, messages):
        return _FakeChatWithMeta.next_response


def _fake_llm_config(**llm_overrides) -> dict:
    """最小配置：api_key_env 为空串，不依赖环境变量、不读网络。"""
    llm_section = {
        "default_provider": "fake-local",
        "temperature": 0.3,
        "max_retries": 2,
        "providers": {
            "fake-local": {
                "litellm_model": "ollama/qwen2.5",
                "api_key_env": "",
            }
        },
    }
    llm_section.update(llm_overrides)
    return {"llm": llm_section}


# ────────────────────────── 1. 口径 2：截断识别 ──────────────────────────


class TestExtractJsonTruncation(unittest.TestCase):
    def test_unbalanced_braces_flagged_as_truncated(self):
        content = '{"readiness": "ok", "items": [1, 2'
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json(content)
        msg = str(ctx.exception)
        self.assertIn(TRUNCATION_PREFIX, msg)
        self.assertIn(f"原始长度 {len(content)}", msg)
        self.assertEqual(ctx.exception.node_name, "llm")

    def test_real_incident_output_flagged_as_truncated(self):
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json(REAL_TRUNCATED)
        msg = str(ctx.exception)
        self.assertIn("疑似被 max_tokens 截断", msg)
        self.assertIn(f"原始长度 {len(REAL_TRUNCATED)}", msg)
        self.assertEqual(ctx.exception.node_name, "llm")

    def test_unclosed_string_with_balanced_braces_flagged(self):
        """括号配对但引号未闭合：靠引号扫描判截断。"""
        content = '{"a": 1, "b": "未闭合的字符串}'
        self.assertEqual(content.count("{"), content.count("}"))
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json(content)
        self.assertIn("疑似被 max_tokens 截断", str(ctx.exception))

    def test_truncated_after_fence_stripping(self):
        content = '```json\n{"a": "truncated\n```'
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json(content)
        self.assertIn("疑似被 max_tokens 截断", str(ctx.exception))

    def test_message_contains_preview_of_original(self):
        content = '{"notes": "中文预览'
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json(content)
        msg = ctx.exception.message
        self.assertTrue(msg.endswith(content[:200].replace("\n", " ")))

    def test_non_truncated_broken_json_keeps_old_message(self):
        """不满足截断特征：维持 `模型返回非JSON: <预览>`。"""
        content = '{invalid json here}'
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json(content)
        msg = ctx.exception.message
        self.assertNotIn("截断", msg)
        self.assertEqual(msg, f"模型返回非JSON: {content}")

    def test_empty_response_keeps_old_message(self):
        for empty in ("", "   ", None):
            with self.assertRaises(NodeExecutionError) as ctx:
                extract_json(empty)
            self.assertEqual(
                ctx.exception.message, "模型返回非JSON: 响应内容为空"
            )


# ────────────────────────── 2. 口径 4：解析行为零变化 ──────────────────────────


class TestExtractJsonUnchanged(unittest.TestCase):
    def test_plain_json_parsed(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json_parsed(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prose_wrapped_json_parsed(self):
        content = '好的，结果如下：{"a": 1} 以上。'
        self.assertEqual(extract_json(content), {"a": 1})

    def test_closing_brace_inside_string_still_parsed(self):
        """字符串内含 "}" 的合法 JSON 不受截断识别影响。"""
        self.assertEqual(extract_json('{"a": "}"}'), {"a": "}"})

    def test_escaped_quote_inside_string_still_parsed(self):
        self.assertEqual(
            extract_json('{"a": "he said \\"hi\\""}'), {"a": 'he said "hi"'}
        )

    def test_non_object_json_rejected(self):
        with self.assertRaises(NodeExecutionError) as ctx:
            extract_json("[1, 2]")
        self.assertIn("不是对象", ctx.exception.message)


# ────────────────────────── 3. 第二节：max_tokens 透传 ──────────────────────────


class TestMaxTokensPassthrough(unittest.TestCase):
    def test_build_llm_passes_max_tokens(self):
        with patch("kernel.model.ChatLiteLLM", _FakeChat):
            build_llm(_fake_llm_config(max_tokens=8192))
            self.assertEqual(_FakeChat.last_kwargs["max_tokens"], 8192)

    def test_build_chat_passes_max_tokens(self):
        with patch("kernel.model.ChatLiteLLM", _FakeChat):
            build_chat(_fake_llm_config(max_tokens=8192))
            self.assertEqual(_FakeChat.last_kwargs["max_tokens"], 8192)

    def test_build_llm_omits_max_tokens_when_none(self):
        """max_tokens 未配置：不传该参数，保持 provider 默认（向后兼容）。"""
        with patch("kernel.model.ChatLiteLLM", _FakeChat):
            build_llm(_fake_llm_config())
            self.assertNotIn("max_tokens", _FakeChat.last_kwargs)

    def test_build_chat_omits_max_tokens_when_none(self):
        with patch("kernel.model.ChatLiteLLM", _FakeChat):
            build_chat(_fake_llm_config())
            self.assertNotIn("max_tokens", _FakeChat.last_kwargs)

    def test_build_llm_keeps_other_kwargs(self):
        with patch("kernel.model.ChatLiteLLM", _FakeChat):
            llm = build_llm(_fake_llm_config(max_tokens=8192))
            kwargs = _FakeChat.last_kwargs
        self.assertEqual(kwargs["model"], "ollama/qwen2.5")
        self.assertEqual(kwargs["temperature"], 0.3)
        self.assertEqual(kwargs["max_retries"], 2)
        self.assertNotIn("api_key", kwargs)  # api_key 为空串时不传
        self.assertEqual(llm("随便"), {"status": "ok"})

    def test_llm_text_channel_not_parsed(self):
        _FakeChat.next_content = "# 标题\n正文"
        try:
            with patch("kernel.model.ChatLiteLLM", _FakeChat):
                llm = build_llm(_fake_llm_config(max_tokens=8192))
                self.assertEqual(llm("写文档", as_text=True), "# 标题\n正文")
        finally:
            _FakeChat.next_content = '{"status": "ok"}'


# ────────────────────────── 4. S048 修复B：空内容报错文案 ──────────────────────────

EMPTY_PREFIX = "模型返回非JSON: 响应内容为空"


class TestEmptyContentMessage(unittest.TestCase):
    """空正文时错误文案：有截断信号补可执行提示，无信号保持原文案。"""

    def _invoke(self, response) -> NodeExecutionError:
        """用预制响应对象跑一遍 build_llm 闭包，返回抛出的 NodeExecutionError。"""
        _FakeChatWithMeta.next_response = response
        with patch("kernel.model.ChatLiteLLM", _FakeChatWithMeta):
            llm = build_llm(_fake_llm_config(max_tokens=8192))
            with self.assertRaises(NodeExecutionError) as ctx:
                llm("出题")
        return ctx.exception

    def test_empty_with_finish_reason_length_adds_hint(self):
        """空 + finish_reason=length：保留原句，补一句可执行提示。"""
        exc = self._invoke(
            SimpleNamespace(
                content="",
                response_metadata={"finish_reason": "length"},
                additional_kwargs={},
            )
        )
        self.assertEqual(exc.node_name, "llm")
        self.assertEqual(exc.message, EMPTY_PREFIX + EMPTY_CONTENT_HINT)
        self.assertIn("疑似隐藏思考占满输出额度", exc.message)
        self.assertIn("max_tokens", exc.message)
        self.assertIn("非思考模型", exc.message)

    def test_empty_without_reasoning_info_keeps_old_message(self):
        """空 + 无 reasoning 信息（finish_reason=stop）：文案逐字不变。"""
        exc = self._invoke(
            SimpleNamespace(
                content="   ",
                response_metadata={"finish_reason": "stop"},
                additional_kwargs={},
            )
        )
        self.assertEqual(exc.message, EMPTY_PREFIX)
        self.assertNotIn("max_tokens", exc.message)

    def test_empty_without_response_metadata_keeps_old_message(self):
        """响应对象不带元数据（旧假对象形态）：文案逐字不变，不抛二次异常。"""
        exc = self._invoke(SimpleNamespace(content=""))
        self.assertEqual(exc.message, EMPTY_PREFIX)

    def test_empty_with_reasoning_content_adds_hint(self):
        """空正文但 additional_kwargs 带 reasoning 段：同样补提示（thinking 块吃满额度）。"""
        exc = self._invoke(
            SimpleNamespace(
                content="",
                response_metadata={},
                additional_kwargs={"reasoning_content": "先想一下题目…"},
            )
        )
        self.assertEqual(exc.message, EMPTY_PREFIX + EMPTY_CONTENT_HINT)

    def test_non_empty_content_parses_unchanged(self):
        """正文非空：带截断信号也不影响解析（提示只在为空分支生效）。"""
        _FakeChatWithMeta.next_response = SimpleNamespace(
            content='```json\n{"status": "ok"}\n```',
            response_metadata={"finish_reason": "length"},
            additional_kwargs={"reasoning_content": "思考"},
        )
        with patch("kernel.model.ChatLiteLLM", _FakeChatWithMeta):
            llm = build_llm(_fake_llm_config(max_tokens=8192))
            self.assertEqual(llm("随便"), {"status": "ok"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


# [C 2026-09-15 by codebuddy-ds41flash] tests/test_kernel_model.py 新增完成
# [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 修复B：补第 4 节空内容文案用例
