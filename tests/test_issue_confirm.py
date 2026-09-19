# [C 2026-09-11] 块2 工单确认门（issue_confirm）自测
"""issue_confirm 节点零成本自测：patch 掉 nodes.issues.interrupt，不发起任何真实模型调用。

覆盖：
1. classify_confirm_answer 纯函数：确认精确集合（含空串）/回PRD关键词包含判定/
   "可以，但粒度太粗"判 feedback/"回PRD重写，确认"判 back_to_prd/英文大小写；
2. 节点级（假 interrupt 依次返回预置答复）：
   - confirm（confirmed/确认/行吧/按这个来）只留痕、不写意见字段、plan 原样保留；
     空答复不当作确认：再抛 interrupt、不放行；
   - feedback 计数 0->1、1->2，意见按固定中文格式包装；
   - 第 3 版（count=2）feedback 触发 escalated 第二次 interrupt，
     二次答复 confirm 落盘 / 带新决策意见继续拆（count=3）；
   - 首次回PRD（redo=0）清空 plan、redo=1、count=0、回炉意见含原话；
   - redo=1 再回PRD 触发暂停：二次 confirm 落盘 / 普通意见重拆 /
     仍要求回炉按其意思再回炉（留痕，不再降级成前缀意见）；
   - 修订升级暂停时二次答复改选回PRD（redo=0）正常发起回炉；
3. route_after_issue_confirm 三分支 + 缺字段安全默认；
4. make_issue_splitting 返回两个清零字段；prd_generation 返回 prd_rewrite_feedback="";
5. build_graph 编译通过且 9 节点含 issue_confirm；
6. prd_generation.md 回炉条件块（非空渲染"回炉"/空不渲染）；
7. run_prd_workflow._build_question issue_confirm 特化文案含"工单确认门"；
8. AGENTS.md 静态含 issue_confirm 协助说明。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python tests/test_issue_confirm.py
也可用 pytest 收集（无 pytest 时直接脚本运行，仅依赖标准库 unittest）。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# Windows GBK 控制台兜底：断言信息与脚本输出尽量 ASCII，中文仅存在于测试数据中
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from jinja2 import Template  # noqa: E402

from components.registry import ComponentRegistry  # noqa: E402
from kernel.artifact import ArtifactManager  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.issues import (  # noqa: E402
    MAX_ESCALATION_DEPTH,
    MAX_ISSUE_REVISIONS,
    _REDO_ESCALATION_REASON,
    _REVISION_ESCALATION_REASON,
    classify_confirm_answer,
    make_issue_confirm,
    make_issue_splitting,
    route_after_issue_confirm,
)
from nodes.prd import make_prd_generation  # noqa: E402

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"


# ────────────────────────── 测试替身与夹具 ──────────────────────────


class FakeLLM:
    """按通道与调用次序返回预制响应的假模型（零网络、零花费）。"""

    def __init__(self, json_queue=None, text_queue=None):
        self.json_queue = list(json_queue or [])
        self.text_queue = list(text_queue or [])
        self.calls = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        if as_text:
            return self.text_queue.pop(0)
        return self.json_queue.pop(0)


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(runner=runner, registry=registry, artifacts=artifacts, kb=None)


# 确认门节点测试用最小 plan（节点本身不校验结构，仅随载荷传递/保留）
DEMO_PLAN = {
    "readiness": "pass",
    "issues": [{"id": "I1", "title": "用户用手机号登录后看到首页"}],
    "coverage": [],
}


def confirm_state(**overrides):
    """工单确认门入口 state（plan 非空、计数/意见为默认值）。"""
    state = {
        "requirement_name": "demo-req",
        "issue_plan": dict(DEMO_PLAN),
        "issue_revision_count": 0,
        "issue_prd_redo_count": 0,
        "issue_revision_feedback": "",
        "prd_rewrite_feedback": "",
        "human_feedback": [],
    }
    state.update(overrides)
    return state


def run_confirm_node(state, answers):
    """patch 掉 nodes.issues.interrupt，按 answers 次序回放 resume 值。

    返回 (节点输出 dict, 历次 interrupt 载荷 list)。
    """
    payloads: list[dict] = []
    queue = list(answers)

    def fake_interrupt(value):
        payloads.append(value)
        return queue.pop(0)

    node = make_issue_confirm(None)  # deps 不使用：确认门不调模型、不取依赖
    with patch("nodes.issues.interrupt", side_effect=fake_interrupt):
        out = node(state)
    return out, payloads


def valid_issue_plan():
    """符合 IssueSplittingSchema 的最小合法工单方案（供拆单节点契约测试）。"""
    return {
        "readiness": "pass",
        "readiness_notes": "默认 pass：工单可开工",
        "assumptions": ["假设验证码通道沿用既有短信服务"],
        "version_map": [],
        "issues": [
            {
                "id": "I1",
                "title": "用户用手机号登录后看到首页",
                "issue_type": "AFK",
                "decision_needed": "",
                "priority": "P0",
                "labels": ["账号"],
                "source_sections": ["## 主流程"],
                "user_value": "注册用户能进入自己的工作台",
                "what_to_build": "手机号加验证码登录，成功跳首页，错误原位提示",
                "acceptance_criteria": [
                    "正确验证码登录后跳转首页",
                    "错误验证码原位提示不清空手机号",
                ],
                "verification": "测试环境用预设验证码演示主路径与错误路径",
                "blocked_by": [],
                "open_questions": [],
            }
        ],
        "coverage": [
            {
                "prd_item": "手机号登录（## 主流程）",
                "status": "covered",
                "covered_by": ["I1"],
                "notes": "",
            }
        ],
        "summary": "1 张 AFK，整体可开工",
    }


# ────────────────────────── 1. 分类纯函数 ──────────────────────────


class TestClassifyConfirmAnswer(unittest.TestCase):
    def test_confirm_exact_words(self):
        for word in (
            "confirmed",
            "confirm",
            "ok",
            "okay",
            "yes",
            "确认",
            "通过",
            "同意",
            "没问题",
            "可以",
            "行",
            "就这样",
            "就这版",
            "落盘",
            "放行",
            "同意拆",
            # [MA 2026-09-19] S056：补的日常肯定说法
            "行吧",
            "按这个来",
            "好的",
            "听你的",
            "没意见",
            "通过吧",
        ):
            self.assertEqual(classify_confirm_answer(word), "confirm", msg=word)

    def test_empty_string_is_not_confirm(self):
        # [MA 2026-09-19] S056：空串不再算确认（节点在分类前拦空、继续停等）
        self.assertEqual(classify_confirm_answer(""), "feedback")
        self.assertEqual(classify_confirm_answer("   "), "feedback")

    def test_back_to_prd_keywords(self):
        for word in (
            "回prd",
            "回PRD",
            "回到prd",
            "重做prd",
            "重写prd",
            "重新讨论prd",
            "重新走prd",
            "prd重写",
            "回炉",
        ):
            self.assertEqual(classify_confirm_answer(word), "back_to_prd", msg=word)

    def test_qualified_permission_is_feedback(self):
        # 精确匹配："可以，但要改"不得判确认
        self.assertEqual(classify_confirm_answer("可以，但粒度太粗"), "feedback")
        self.assertEqual(classify_confirm_answer("不通过，要改"), "feedback")

    def test_back_prd_with_confirm_word_priority(self):
        # 回PRD 包含判定优先于确认精确匹配
        self.assertEqual(classify_confirm_answer("回PRD重写，确认"), "back_to_prd")

    def test_english_case_insensitive(self):
        self.assertEqual(classify_confirm_answer("OK"), "confirm")
        self.assertEqual(classify_confirm_answer(" Confirmed "), "confirm")
        self.assertEqual(classify_confirm_answer("YES"), "confirm")

    def test_plain_text_defaults_feedback(self):
        self.assertEqual(classify_confirm_answer("I1 和 I2 粒度太粗，合并"), "feedback")
        # [MA 2026-09-19] S056：None 归一为空串，空串不再算确认
        self.assertEqual(classify_confirm_answer(None), "feedback")

    def test_negated_back_to_prd_is_feedback(self):
        # [C 2026-09-11] 紧邻否定语的"回炉/重做PRD"是"不回炉"，不得判 back_to_prd
        self.assertEqual(
            classify_confirm_answer("不用回炉，直接改工单"), "feedback"
        )
        self.assertEqual(
            classify_confirm_answer("不要重做PRD"), "feedback"
        )
        # 顺带覆盖其他否定语，确保落 feedback 而非误判回炉
        self.assertEqual(classify_confirm_answer("不必回炉"), "feedback")
        self.assertEqual(classify_confirm_answer("先别回PRD"), "feedback")

    def test_negated_back_to_prd_expanded_words(self):
        # [C 2026-09-12 by pi-deepseek-flash] 第③项修复：「不需要/无需」此前漏配，
        # 「不需要回炉」被误判为 back_to_prd；补词后一律落 feedback
        for word in (
            "不需要回炉",
            "不需要回炉，确认吧",
            "无需回PRD",
            "无需回 PRD",
            "不需要重做PRD",
        ):
            self.assertEqual(classify_confirm_answer(word), "feedback", msg=word)
        # 反向回归：不带否定回炉关键词仍判 back_to_prd
        self.assertEqual(classify_confirm_answer("需要回炉"), "back_to_prd")

    def test_spaced_back_to_prd_keywords(self):
        # [C 2026-09-11] 关键词内部带空格（含全角空格）归一化后仍判 back_to_prd
        self.assertEqual(classify_confirm_answer("回 PRD"), "back_to_prd")
        self.assertEqual(classify_confirm_answer("重写 PRD"), "back_to_prd")
        self.assertEqual(classify_confirm_answer("回\u3000PRD"), "back_to_prd")

    def test_negative_prefix_not_over_broad(self):
        # 否定语只看关键词紧邻前 3 字：离得远的否定不得拦截回炉判定
        self.assertEqual(
            classify_confirm_answer("这个不用你管，我确定回PRD"), "back_to_prd"
        )
        self.assertEqual(
            classify_confirm_answer("不用回炉那句收回，现在就要回 PRD"),
            "back_to_prd",
        )


# ────────────────────────── 2. 确认门节点级行为 ──────────────────────────


class TestIssueConfirmNode(unittest.TestCase):
    def test_confirm_variants_keep_plan_and_only_log(self):
        # confirmed / 确认 / 行吧 / 按这个来 四种确认：首次载荷是 draft；输出只含 human_feedback，
        # 不写任何意见/计数字段（plan 原样保留在 state），合并后路由落盘
        for answer in ("confirmed", "确认", "行吧", "按这个来"):
            out, payloads = run_confirm_node(confirm_state(), [answer])
            self.assertEqual(payloads[0]["node"], "issue_confirm")
            self.assertEqual(payloads[0]["status"], "draft")
            self.assertEqual(payloads[0]["requirement_name"], "demo-req")
            self.assertIn("issues", payloads[0]["issue_plan"])
            self.assertEqual(set(out.keys()), {"human_feedback"}, msg=answer)
            self.assertEqual(len(out["human_feedback"]), 1, msg=answer)
            self.assertEqual(out["human_feedback"][0]["kind"], "confirm", msg=answer)
            merged = {**confirm_state(), **out}
            self.assertEqual(
                route_after_issue_confirm(merged), "artifact_persist", msg=answer
            )

    def test_empty_answer_keeps_waiting_not_confirmed(self):
        # [MA 2026-09-19] S056 用例 a：draft 阶段空答复再抛 interrupt、不放行；
        # 下一句「确认」才只留痕、路由落盘
        out, payloads = run_confirm_node(confirm_state(), ["", "确认"])
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(payloads[1]["status"], "draft")
        self.assertIn("issue_plan", payloads[1])
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")
        self.assertEqual(
            route_after_issue_confirm({**confirm_state(), **out}), "artifact_persist"
        )

    def test_feedback_first_round(self):
        out, payloads = run_confirm_node(
            confirm_state(), ["I1 粒度太粗，请和 I2 合并"]
        )
        self.assertEqual(out["issue_revision_count"], 1)
        feedback = out["issue_revision_feedback"]
        self.assertTrue(feedback.startswith("【第1轮工单修改意见】"))
        self.assertIn("I1 粒度太粗，请和 I2 合并", feedback)
        self.assertIn("未要求改的部分保持稳定", feedback)
        self.assertNotIn("issue_plan", out)  # plan 保留在 state，不动它
        self.assertEqual(out["human_feedback"][-1]["kind"], "feedback")
        # 合并后条件边回 issue_splitting 重拆
        self.assertEqual(route_after_issue_confirm({**confirm_state(), **out}),
                         "issue_splitting")

    def test_feedback_second_round(self):
        out, _ = run_confirm_node(
            confirm_state(issue_revision_count=1), ["第二版还要拆出退款异常流"]
        )
        self.assertEqual(out["issue_revision_count"], 2)
        self.assertTrue(out["issue_revision_feedback"].startswith("【第2轮工单修改意见】"))
        self.assertIn("退款异常流", out["issue_revision_feedback"])

    def test_third_version_escalation_then_confirm(self):
        # count=2（第 3 版）仍提意见 -> 第二次 interrupt，载荷 status=escalated、
        # reason 含"升级"与三选项；二次答复确认 -> 落盘
        out, payloads = run_confirm_node(
            confirm_state(issue_revision_count=MAX_ISSUE_REVISIONS),
            ["第三版还不满意-AAA", "确认"],
        )
        self.assertEqual(len(payloads), 2)
        esc = payloads[1]
        self.assertEqual(esc["status"], "escalated")
        self.assertEqual(esc["node"], "issue_confirm")
        self.assertIn("第 3 版", esc["reason"])
        self.assertIn("issues", esc["issue_plan"])
        self.assertIn("prior_feedbacks", esc)
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(len(out["human_feedback"]), 2)  # 首次意见 + 二次确认都留痕
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")
        self.assertEqual(
            route_after_issue_confirm({**confirm_state(issue_revision_count=2), **out}),
            "artifact_persist",
        )

    def test_third_version_escalation_then_new_opinion(self):
        # 升级暂停后，人带来新决策的具体意见 -> 主动再拆一轮（count=3），
        # 意见用第二次答复文本（首次意见不泄漏进反馈）
        out, payloads = run_confirm_node(
            confirm_state(issue_revision_count=MAX_ISSUE_REVISIONS),
            ["第三版还不满意-AAA", "新决策：本期不做优惠券退款，据此重拆-BBB"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["issue_revision_count"], 3)
        feedback = out["issue_revision_feedback"]
        self.assertTrue(feedback.startswith("【第3轮工单修改意见】"))
        self.assertIn("BBB", feedback)
        self.assertNotIn("AAA", feedback)
        self.assertEqual(
            route_after_issue_confirm({**confirm_state(issue_revision_count=2), **out}),
            "issue_splitting",
        )

    def test_back_to_prd_first_time(self):
        # redo=0 首次回PRD：清空 plan、redo 置 1、工单修订计数归零、
        # issue_revision_feedback 清空、prd_rewrite_feedback 含用户原话
        out, _ = run_confirm_node(
            confirm_state(), ["回PRD，退款口径需要重新讨论-CCC"]
        )
        self.assertEqual(out["issue_plan"], {})
        self.assertEqual(out["issue_prd_redo_count"], 1)
        self.assertEqual(out["issue_revision_count"], 0)
        self.assertEqual(out["issue_revision_feedback"], "")
        redo_text = out["prd_rewrite_feedback"]
        self.assertIn("退款口径需要重新讨论-CCC", redo_text)
        self.assertIn("完整修订版 PRD", redo_text)
        self.assertIn("重新拆单", redo_text)
        # [C 2026-09-11] 阻断1回归：原话只由 handle_back_to_prd 统一留痕一次，
        # draft 分支不得再 append 一遍（此前 draft-back_to_prd 与 redo-1 重复）
        self.assertEqual(len(out["human_feedback"]), 1)
        log = out["human_feedback"][0]
        self.assertEqual(log["kind"], "back_to_prd")
        self.assertEqual(log["round"], "redo-1")
        self.assertIn("退款口径需要重新讨论-CCC", log["feedback"])
        # 合并后条件边去 prd_generation 回炉
        self.assertEqual(route_after_issue_confirm({**confirm_state(), **out}),
                         "prd_generation")

    def test_back_to_prd_second_time_escalates_then_confirm(self):
        # redo=1 再回PRD -> escalated；二次 confirm -> 按当前版落盘（不再回 PRD）
        out, payloads = run_confirm_node(
            confirm_state(issue_prd_redo_count=1),
            ["回PRD", "确认"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertIn("回炉重写 PRD 1 次", payloads[1]["reason"])
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")
        merged = {**confirm_state(issue_prd_redo_count=1), **out}
        self.assertEqual(route_after_issue_confirm(merged), "artifact_persist")

    def test_back_to_prd_second_time_then_feedback(self):
        # redo=1 升级后，二次给普通工单意见 -> 按意见重拆（不再回 PRD）
        out, payloads = run_confirm_node(
            confirm_state(issue_prd_redo_count=1),
            ["回PRD", "把 I1 拆成两张端到端工单-DDD"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["issue_revision_count"], 1)
        self.assertIn("DDD", out["issue_revision_feedback"])
        self.assertNotIn("prd_rewrite_feedback", out)

    def test_back_to_prd_insist_twice_goes_upstream(self):
        # [MA 2026-09-19] S056 用例 d：redo=1 暂停后二次仍要求回炉
        # -> 按其意思再回炉（留痕 back_to_prd），不再降级成"仍需回炉PRD："前缀意见
        out, payloads = run_confirm_node(
            confirm_state(issue_prd_redo_count=1),
            ["回PRD", "回PRD重写"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["issue_plan"], {})
        self.assertTrue(out["prd_rewrite_feedback"])
        self.assertIn("回PRD重写", out["prd_rewrite_feedback"])
        self.assertEqual(out["human_feedback"][-1]["kind"], "back_to_prd")
        self.assertEqual(out["issue_revision_feedback"], "")
        self.assertEqual(
            route_after_issue_confirm({**confirm_state(issue_prd_redo_count=1), **out}),
            "prd_generation",
        )

    def test_revision_escalation_second_answer_back_to_prd_redo_zero(self):
        # 第 3 版升级暂停时，二次答复改选回PRD，且 redo=0 -> 正常发起唯一一次回炉
        out, payloads = run_confirm_node(
            confirm_state(issue_revision_count=MAX_ISSUE_REVISIONS),
            ["第三版意见-AAA", "回PRD"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["issue_plan"], {})
        self.assertEqual(out["issue_prd_redo_count"], 1)
        self.assertEqual(out["issue_revision_count"], 0)
        self.assertTrue(out["prd_rewrite_feedback"])
        # [C 2026-09-11] 阻断1回归：首次意见 1 条 + 回PRD 由 handle_back_to_prd
        # 统一留痕 1 条（round=redo-1），同一原话不得出现两条 back_to_prd
        self.assertEqual(len(out["human_feedback"]), 2)
        kinds = [item["kind"] for item in out["human_feedback"]]
        self.assertEqual(kinds, ["feedback", "back_to_prd"])
        rounds = [item["round"] for item in out["human_feedback"]]
        self.assertEqual(rounds, ["draft-feedback-3", "redo-1"])

    # ── [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级深度硬上限 ──

    def test_escalation_limit_opinion_redrafts(self):
        # [MA 2026-09-19] S056 用例 b：已超建议轮数后给具体意见
        # -> 按其意见再拆一轮（计数 +1、意见写进 issue_revision_feedback、留痕）
        state = confirm_state(
            issue_revision_count=MAX_ISSUE_REVISIONS,
            issue_escalation_depth=MAX_ESCALATION_DEPTH,
        )
        out, payloads = run_confirm_node(
            state, ["第四版还不满意-EEE", "第五版按新决策再拆-FFF"]
        )
        self.assertEqual(len(payloads), 2)  # draft + 上限暂停
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertIn("人工介入上限", payloads[1]["reason"])
        self.assertEqual(out["issue_revision_count"], MAX_ISSUE_REVISIONS + 1)
        self.assertIn("FFF", out["issue_revision_feedback"])
        self.assertNotIn("EEE", out["issue_revision_feedback"])
        self.assertEqual(out["human_feedback"][-1]["kind"], "feedback")
        self.assertEqual(
            route_after_issue_confirm({**state, **out}), "issue_splitting"
        )

    def test_escalation_limit_empty_answer_keeps_waiting(self):
        # [MA 2026-09-19] S056 用例 a：上限暂停时空答复再抛 interrupt、不放行；
        # 下一句「确认」才按当前版落盘
        state = confirm_state(
            issue_revision_count=MAX_ISSUE_REVISIONS,
            issue_escalation_depth=MAX_ESCALATION_DEPTH,
        )
        out, payloads = run_confirm_node(state, ["还有意见-EEE", "", "确认"])
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(payloads[2]["status"], "escalated")
        self.assertIn("人工介入上限", payloads[2]["reason"])
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")

    def test_redo_limit_insist_goes_upstream(self):
        # [MA 2026-09-19] S056 用例 d：回炉额度用尽 + 3 版建议线，
        # 二次答复换意见后又坚持回炉 -> 按其意思回 prd_generation（留痕）
        state = confirm_state(
            issue_prd_redo_count=1,
            issue_revision_count=MAX_ISSUE_REVISIONS,
            issue_escalation_depth=0,
        )
        out, payloads = run_confirm_node(
            state, ["回PRD", "新决策意见-A", "回PRD重写"]
        )
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertIn("回炉重写 PRD 1 次", payloads[1]["reason"])
        self.assertEqual(payloads[2]["status"], "escalated")
        self.assertIn("人工介入上限", payloads[2]["reason"])
        # 按其意思回上游：清空工单 + 写回炉意见，不再把"回PRD"降级成工单意见
        self.assertEqual(out["issue_plan"], {})
        self.assertTrue(out["prd_rewrite_feedback"])
        self.assertIn("回PRD重写", out["prd_rewrite_feedback"])
        self.assertEqual(out["human_feedback"][-1]["kind"], "back_to_prd")
        self.assertEqual(
            route_after_issue_confirm({**state, **out}), "prd_generation"
        )


# ────────────────────────── 3. 条件边路由 ──────────────────────────


class TestRouteAfterIssueConfirm(unittest.TestCase):
    def test_three_branches(self):
        # 回炉：plan 空 + prd_rewrite_feedback 非空 -> prd_generation
        self.assertEqual(
            route_after_issue_confirm(
                {"issue_plan": {}, "prd_rewrite_feedback": "回炉意见",
                 "issue_revision_feedback": ""}
            ),
            "prd_generation",
        )
        # 工单意见：issue_revision_feedback 非空 -> issue_splitting
        self.assertEqual(
            route_after_issue_confirm(
                {"issue_plan": {"issues": []}, "prd_rewrite_feedback": "",
                 "issue_revision_feedback": "【第1轮工单修改意见】x"}
            ),
            "issue_splitting",
        )
        # 确认：plan 非空、两个意见皆空 -> artifact_persist
        self.assertEqual(
            route_after_issue_confirm(
                {"issue_plan": {"issues": [{"id": "I1"}]},
                 "prd_rewrite_feedback": "", "issue_revision_feedback": ""}
            ),
            "artifact_persist",
        )

    def test_empty_state_goes_back_to_confirm(self):
        # [MA 2026-09-19] S056：缺字段（None/缺键）不炸，也不再兜底落盘——回本节点继续停等
        self.assertEqual(route_after_issue_confirm({}), "issue_confirm")
        self.assertEqual(
            route_after_issue_confirm({"issue_plan": None, "prd_rewrite_feedback": None}),
            "issue_confirm",
        )


# ────────────────────────── 4. 跨节点契约（消费即清零）──────────────────────────


class TestConsumeAndClearContracts(unittest.TestCase):
    def test_issue_splitting_returns_clearing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(json_queue=[valid_issue_plan()])
            deps = make_deps(Path(tmp), fake)
            state = {
                "requirement_name": "demo-req",
                "prd_markdown": "# demo PRD\n\n## 主流程\n手机号登录",
                "red_team_review": {"verdict": "pass", "warnings": [], "blockers": []},
                # 模拟带着上一轮意见进来
                "issue_revision_feedback": "【第1轮工单修改意见】陈旧意见",
                "prd_rewrite_feedback": "陈旧回炉意见",
            }
            out = make_issue_splitting(deps)(state)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(out["issue_revision_feedback"], "")
            self.assertEqual(out["prd_rewrite_feedback"], "")
            self.assertTrue(out["issue_plan"]["issues"])

    def test_prd_generation_clears_redo_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(text_queue=["# 修订版 PRD\n\n正文"])
            deps = make_deps(Path(tmp), fake)
            state = {
                "confirmed_requirement": "需求 X",
                "section_plan": {},
                "user_insights": {},
                "red_team_review": {},
                "prd_rewrite_feedback": "【工单拆解阶段回炉意见】陈旧回炉意见",
            }
            out = make_prd_generation(deps)(state)
            self.assertEqual(out["prd_markdown"], "# 修订版 PRD\n\n正文")
            self.assertEqual(out["prd_rewrite_feedback"], "")
            self.assertTrue(all(c["as_text"] for c in fake.calls))


# ────────────────────────── 5. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_ten_nodes_and_launch_plan_branch(self):
        # [C 2026-09-11] 块1 新增 launch_plan：确认分支改走 launch_plan -> artifact_persist
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())  # 队列空：只编译不执行
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            for name in (
                "kb_lookup",
                "intake",
                "requirement_confirm",
                "needs_discovery",
                "prd_generation",
                "prd_review",
                "issue_splitting",
                "issue_confirm",  # 块2 工单确认门
                "launch_plan",    # [C 2026-09-11] 块1 发布计划节点
                "artifact_persist",
            ):
                self.assertIn(name, names)
            drawn = graph.get_graph().draw_mermaid()
            # 确认分支经 launch_plan 到达 artifact_persist；三分支目标都在图上
            for token in ("issue_confirm", "issue_splitting", "prd_generation",
                          "launch_plan", "artifact_persist"):
                self.assertIn(token, drawn)


# ────────────────────────── 6. PRD prompt 回炉条件块 ──────────────────────────


class TestPrdPromptRedoBlock(unittest.TestCase):
    def test_redo_feedback_block_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_generation")
            rendered = Template(raw).render(
                confirmed_requirement="需求 X",
                section_plan={},
                user_insights={},
                red_team_review={},
                prd_rewrite_feedback="【工单拆解阶段回炉意见】退款口径要重讨论-EEE",
            )
            self.assertIn("工单拆解阶段回炉意见", rendered)
            self.assertIn("回炉", rendered)
            self.assertIn("退款口径要重讨论-EEE", rendered)

    def test_redo_feedback_block_absent_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_generation")
            rendered = Template(raw).render(
                confirmed_requirement="需求 X",
                section_plan={},
                user_insights={},
                red_team_review={},
                prd_rewrite_feedback="",
            )
            self.assertNotIn("工单拆解阶段回炉意见", rendered)
            # 首轮原 prompt 关键引导仍在
            self.assertIn("资深 PM 的质量标杆", rendered)


# ────────────────────────── 7/8. 流水线文案 + AGENTS.md ──────────────────────────


class TestWorkflowQuestionAndAgentsDoc(unittest.TestCase):
    @staticmethod
    def _load_workflow_module():
        """以独立模块名加载 scripts/run_prd_workflow.py（加载不触发 main）。"""
        script_path = REPO_ROOT / "scripts" / "run_prd_workflow.py"
        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_build_question_specialized_and_agents_doc(self):
        module = self._load_workflow_module()

        # status 缺省按 draft 文案处理
        question = module._build_question("issue_confirm", {}, {})
        self.assertIn("工单确认门", question)
        self.assertIn("回PRD", question)
        self.assertIn("升级暂停", question)
        # 旧的 requirement_confirm 特化不受影响
        self.assertIn(
            "需求确认门", module._build_question("requirement_confirm", {}, {})
        )

        agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("issue_confirm", agents_md)
        self.assertIn("status=escalated", agents_md)

    def test_build_question_draft_vs_escalated_copy(self):
        # [C 2026-09-11] 阻断2b：draft 与 escalated 文案不同；
        # escalated（含 redo 额度用尽）不承诺一定可回 PRD，语义以 reason 为准
        module = self._load_workflow_module()
        draft_q = module._build_question(
            "issue_confirm", {"status": "draft"}, {}
        )
        revision_esc = module._build_question(
            "issue_confirm",
            {"status": "escalated", "reason": _REVISION_ESCALATION_REASON},
            {},
        )
        redo_esc = module._build_question(
            "issue_confirm",
            {"status": "escalated", "reason": _REDO_ESCALATION_REASON},
            {},
        )
        self.assertNotEqual(draft_q, revision_esc)
        self.assertNotEqual(draft_q, redo_esc)
        # draft 保持原三选一文案（仍承诺可回炉 1 次）
        self.assertIn("回炉重写 PRD（限1次）", draft_q)
        # escalated 两场景：指向 reason、不再承诺回炉
        for q in (revision_esc, redo_esc):
            self.assertIn("升级暂停", q)
            self.assertIn("reason", q)
            self.assertIn("以 reason 的说明为准", q)
            self.assertNotIn("回炉重写 PRD（限1次）", q)
            self.assertIn("不再承诺", q)

    def test_emit_hitl_escalated_exposes_status_reason_priors(self):
        # [C 2026-09-11] 阻断2a：escalated 载荷经 _emit_hitl 必须向 Pi 透传
        # status / reason / prior_feedbacks（list 走 JSON），且不重复打印
        module = self._load_workflow_module()
        payload = {
            "node": "issue_confirm",
            "status": "escalated",
            "reason": _REDO_ESCALATION_REASON,
            "requirement_name": "demo-req",
            "issue_plan": {"issues": [{"id": "I1"}]},
            "prior_feedbacks": [
                {"kind": "feedback", "round": "draft-feedback-1",
                 "feedback": "把 I1 拆细"}
            ],
        }
        graph = MagicMock()
        graph.get_state.return_value.values = {}
        buf = io.StringIO()
        with redirect_stdout(buf):
            module._emit_hitl(graph, {}, "tid-1", payload)
        out = buf.getvalue()
        self.assertIn("STATUS: HITL", out)
        self.assertIn("NODE: issue_confirm", out)
        self.assertIn("QUESTION:", out)
        self.assertIn("status: escalated", out)
        self.assertIn("reason:", out)
        self.assertIn("prior_feedbacks:", out)
        self.assertIn("draft-feedback-1", out)  # list 已 JSON 展开
        # QUESTION 是升级暂停文案，不向 Pi 承诺一定还能回 PRD
        self.assertIn("升级暂停", out)
        self.assertNotIn("回炉重写 PRD（限1次）", out)
        # 三个升级字段各只出现一次标题行（防 draft 时代重复 append 回归）
        self.assertEqual(out.count("status: escalated"), 1)
        self.assertEqual(out.count("prior_feedbacks:"), 1)

    def test_emit_hitl_does_not_pollute_requirement_confirm(self):
        # [C 2026-09-11] 阻断2a：升级三字段仅 issue_confirm 输出；
        # 即便 requirement_confirm 载荷碰巧带同名键也不得泄漏进 STATUS 块
        module = self._load_workflow_module()
        payload = {
            "node": "requirement_confirm",
            "requirement_name": "demo-req",
            "raw_requirement": "做个会议纪要工具",
            "status": "should-not-show",
            "reason": "should-not-show",
            "prior_feedbacks": [{"x": 1}],
        }
        graph = MagicMock()
        graph.get_state.return_value.values = {}
        buf = io.StringIO()
        with redirect_stdout(buf):
            module._emit_hitl(graph, {}, "tid-2", payload)
        out = buf.getvalue()
        self.assertIn("NODE: requirement_confirm", out)
        self.assertNotIn("should-not-show", out)
        self.assertNotIn("prior_feedbacks", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
