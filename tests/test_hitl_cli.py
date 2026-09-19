# CLI 交互层空答复不复用自测（零 API、零图）
"""cli.hitl_cli.handle_hitl 空答复口径测试。

覆盖任务书第 1 条：
- 提示语不再写「直接回车表示确认通过」；
- 空输入不 resume：打印「没收到答复，仍在这里等你的决定」后继续等下一句；
- 非空输入（如「确认」）才用 ``Command(resume=...)`` 恢复执行。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python -m pytest tests/test_hitl_cli.py -v
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from langgraph.types import Command  # noqa: E402

from cli.hitl_cli import handle_hitl  # noqa: E402

INTERRUPT_VALUE = {
    "node": "requirement_confirm",
    "requirement_name": "demo-req",
    "raw_requirement": "帮产品经理做会议纪要总结",
}


class _FakeGraph:
    """假图：get_state 返回空 state；stream 记录调用参数。"""

    def __init__(self):
        self.stream_calls: list[tuple] = []

    def get_state(self, config):  # noqa: ARG002
        return SimpleNamespace(values={"initiative_id": "demo"})

    def stream(self, inputs, config, stream_mode=None):
        self.stream_calls.append((inputs, config, stream_mode))
        return iter([{"needs_discovery": {}}])


class TestHandleHitlEmptyInput(unittest.TestCase):
    def _run(self, answers):
        graph = _FakeGraph()
        config = {"configurable": {"thread_id": "tid-cli"}}
        buf = io.StringIO()
        with patch("cli.hitl_cli.console.input", side_effect=list(answers)):
            with contextlib.redirect_stdout(buf):
                handle_hitl(graph, config, INTERRUPT_VALUE)
        return graph, buf.getvalue()

    def test_empty_input_does_not_resume(self):
        # 空答复 -> 不 resume：打印等待提示并再问一句；「确认」才恢复执行
        graph, out = self._run(["", "确认"])
        self.assertIn("没收到答复，仍在这里等你的决定", out)
        self.assertNotIn("直接回车表示确认通过", out)
        self.assertEqual(len(graph.stream_calls), 1)
        inputs, config, mode = graph.stream_calls[0]
        self.assertIsInstance(inputs, Command)
        self.assertEqual(inputs.resume, "确认")
        self.assertEqual(config["configurable"]["thread_id"], "tid-cli")
        self.assertEqual(mode, "updates")

    def test_two_empty_inputs_then_confirm(self):
        # 连续空答复都停在原地等（不产生任何 resume），直到拿到明确答复
        graph, out = self._run(["", "   ", "行吧"])
        self.assertEqual(out.count("没收到答复，仍在这里等你的决定"), 2)
        self.assertEqual(len(graph.stream_calls), 1)
        self.assertEqual(graph.stream_calls[0][0].resume, "行吧")


if __name__ == "__main__":
    unittest.main(verbosity=2)


