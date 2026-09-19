# 断点续跑自测
"""scripts/run_prd_workflow.py `_run_resume` 三态零 API 测试（假图，不调模型、不建真 graph）。

覆盖任务书「修复 A」的口径：

1. `values` 空（thread 不存在） -> `STATUS: ERROR`（含「thread_id 不存在」），退出码 1，不 stream；
2. `values` 非空且 `next` 空（已到结尾） -> 幂等 `STATUS: DONE`，退出码 0，不 stream；
3. `values` 非空且 `next` 非空（节点执行中途崩溃的中间态） -> 从待执行节点续跑
   （`graph.stream(None, config, stream_mode="updates")`，与既有一次性脚本等价）：
   - 停在下一个停等点 -> `STATUS: HITL`，退出码 0；
   - 一路跑完 -> `STATUS: DONE`，退出码 0；
4. 既有「有待处理中断 -> Command(resume)」路径未改动（回归护栏）；
5. ：有待处理中断时 `--answer` 为空串 / 未传 /
   纯空白 -> 不放行：打印「未收到答复」并重新输出同一节点 `STATUS: HITL`，退出码 0，不 stream；
   给明确答复 -> 正常 `Command(resume)` 推进。

中断收集与输出一律走真实实现（`cli.hitl_cli.collect_interrupts` + `_emit_hitl` / `_emit_done`），
假图只提供 `get_state` / `stream`，用例断言输出块里的节点特化问句与中断载荷，即证明复用而非新造。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python -m pytest tests/test_run_prd_workflow_resume.py -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

# Windows GBK 控制台兜底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from langgraph.types import Command  # noqa: E402

from cli.hitl_cli import collect_interrupts  # noqa: E402

THREAD_ID = "tid-1"


def _load_workflow_module():
    """按文件路径加载 scripts/run_prd_workflow.py（非包内模块，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "run_prd_workflow_under_test_resume",
        REPO_ROOT / "scripts" / "run_prd_workflow.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSnapshot:
    """假状态快照：`values` / `next` / `tasks` 三件套，形态对齐 langgraph StateSnapshot。"""

    def __init__(self, values: dict, next_nodes=(), interrupts=()):
        self.values = values
        self.next = tuple(next_nodes)
        self.tasks = (
            [SimpleNamespace(interrupts=[SimpleNamespace(value=v) for v in interrupts])]
            if interrupts
            else []
        )


class _FakeGraph:
    """假图：get_state 返回当前相位快照；stream 调用后推进到下一相位。

    phases[0] 是 `_run_resume` 进来时看到的状态，phases[1] 是续跑/恢复后的状态；
    只推进一次，便于断言「下一次 get_state 看到的结果」。
    """

    def __init__(self, *phases: _FakeSnapshot):
        self._phases = list(phases)
        self.stream_calls: list[tuple] = []

    def get_state(self, config):  # noqa: ARG002
        return self._phases[0]

    def stream(self, inputs, config, stream_mode=None):
        self.stream_calls.append((inputs, config, stream_mode))
        if len(self._phases) > 1:
            self._phases.pop(0)
        return iter([{}])


class TestResumeThreeStates(unittest.TestCase):
    """无待处理中断时的三态。"""

    @staticmethod
    def _run(graph: _FakeGraph, answer: str | None = None) -> tuple[int, str]:
        mod = _load_workflow_module()
        config = {"configurable": {"thread_id": THREAD_ID}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod._run_resume(graph, config, THREAD_ID, answer)
        return code, buf.getvalue()

    def test_missing_thread_reports_error(self):
        graph = _FakeGraph(_FakeSnapshot({}, ()))
        code, out = self._run(graph)
        self.assertEqual(code, 1)
        self.assertIn("STATUS: ERROR", out)
        self.assertIn("thread_id 不存在", out)
        self.assertEqual(graph.stream_calls, [])

    def test_finished_thread_emits_done_without_streaming(self):
        graph = _FakeGraph(
            _FakeSnapshot({"artifacts": {"prd": "output/finished/prd.md"}}, ())
        )
        code, out = self._run(graph)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: DONE", out)
        self.assertIn("output/finished/prd.md", out)
        self.assertEqual(graph.stream_calls, [])

    def test_crashed_state_resumes_and_stops_at_next_hitl(self):
        payload = {
            "node": "issue_confirm",
            "issue_plan": {"issues": [{"id": "I1"}]},
        }
        graph = _FakeGraph(
            _FakeSnapshot({"requirement_name": "小说助手"}, ("issue_splitting",)),
            _FakeSnapshot(
                {"requirement_name": "小说助手", "issue_plan": payload["issue_plan"]},
                ("issue_confirm",),
                interrupts=[payload],
            ),
        )
        code, out = self._run(graph)

        self.assertEqual(code, 0)
        self.assertIn("STATUS: HITL", out)
        self.assertIn("NODE: issue_confirm", out)
        self.assertIn(f"THREAD_ID: {THREAD_ID}", out)
        # 节点特化问句与 [中断载荷] 段由真实 _emit_hitl 产出（证明复用，未新造一套）
        self.assertIn("工单确认门", out)
        self.assertIn("[中断载荷]", out)

        # 续跑口径：输入 None + stream_mode="updates" + 同一个 config
        self.assertEqual(len(graph.stream_calls), 1)
        inputs, cfg, mode = graph.stream_calls[0]
        self.assertIsNone(inputs)
        self.assertEqual(mode, "updates")
        self.assertEqual(cfg["configurable"]["thread_id"], THREAD_ID)

    def test_crashed_state_resumes_to_done(self):
        graph = _FakeGraph(
            _FakeSnapshot({"requirement_name": "小说助手"}, ("eval_gate",)),
            _FakeSnapshot(
                {
                    "requirement_name": "小说助手",
                    "artifacts": {"launch_plan": "output/crashed/launch_plan.md"},
                },
                (),
            ),
        )
        code, out = self._run(graph)

        self.assertEqual(code, 0)
        self.assertIn("STATUS: DONE", out)
        self.assertIn("output/crashed/launch_plan.md", out)
        self.assertEqual(len(graph.stream_calls), 1)
        self.assertIsNone(graph.stream_calls[0][0])

    def test_resume_reuses_hitl_cli_collect_interrupts(self):
        """中断收集必须复用 cli.hitl_cli.collect_interrupts，不得另起一套。"""
        mod = _load_workflow_module()
        self.assertIs(mod.collect_interrupts, collect_interrupts)


class TestResumeWithPendingInterruptUnchanged(unittest.TestCase):
    """回归护栏：有待处理中断时仍走 Command(resume) 路径。"""

    def test_pending_interrupt_resumes_with_command(self):
        mod = _load_workflow_module()
        graph = _FakeGraph(
            _FakeSnapshot(
                {"requirement_name": "小说助手"},
                ("requirement_confirm",),
                interrupts=[{"node": "requirement_confirm"}],
            ),
            _FakeSnapshot({"requirement_name": "小说助手", "artifacts": {}}, ()),
        )
        config = {"configurable": {"thread_id": THREAD_ID}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod._run_resume(graph, config, THREAD_ID, "非AI")
        out = buf.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("STATUS: DONE", out)
        self.assertEqual(len(graph.stream_calls), 1)
        inputs = graph.stream_calls[0][0]
        self.assertIsInstance(inputs, Command)
        self.assertEqual(inputs.resume, "非AI")


class TestResumeEmptyAnswerDoesNotAdvance(unittest.TestCase):
    """S056：有待处理中断时，空答复 / 未传 --answer 不放行，重新输出当前节点 HITL。"""

    @staticmethod
    def _run(answer: str | None) -> tuple[int, str, _FakeGraph]:
        mod = _load_workflow_module()
        graph = _FakeGraph(
            _FakeSnapshot(
                {"requirement_name": "供应商合同审查 AI 助手"},
                ("requirement_confirm",),
                interrupts=[{"node": "requirement_confirm"}],
            ),
            _FakeSnapshot(
                {
                    "requirement_name": "供应商合同审查 AI 助手",
                    "artifacts": {},
                },
                (),
            ),
        )
        config = {"configurable": {"thread_id": THREAD_ID}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod._run_resume(graph, config, THREAD_ID, answer)
        return code, buf.getvalue(), graph

    def _assert_停下不放行(self, answer: str | None):
        code, out, graph = self._run(answer)
        self.assertEqual(code, 0)
        self.assertIn("未收到答复", out)
        self.assertIn("STATUS: HITL", out)
        self.assertIn("NODE: requirement_confirm", out)
        self.assertNotIn("STATUS: DONE", out)
        # 不 resume：没有调用 graph.stream，节点不推进
        self.assertEqual(graph.stream_calls, [])

    def test_empty_string_answer_reprints_hitl(self):
        self._assert_停下不放行("")

    def test_missing_answer_reprints_hitl(self):
        self._assert_停下不放行(None)

    def test_whitespace_only_answer_reprints_hitl(self):
        self._assert_停下不放行("   ")

    def test_explicit_answer_advances(self):
        code, out, graph = self._run("confirmed")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: DONE", out)
        self.assertEqual(len(graph.stream_calls), 1)
        inputs = graph.stream_calls[0][0]
        self.assertIsInstance(inputs, Command)
        self.assertEqual(inputs.resume, "confirmed")


if __name__ == "__main__":
    unittest.main(verbosity=2)


