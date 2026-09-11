# [C 2026-09-11] 块2 发布计划确认门（launch_confirm）自测
"""launch_confirm 节点零成本自测：patch 掉 nodes.launch_plan.interrupt，不发起任何真实模型调用。

覆盖（同构 tests/test_issue_confirm.py）：
1. classify_launch_answer 纯函数：确认精确集合（含空串、"发布""发布吧"）/回工单关键词
   包含判定/"可以，但要改"判 feedback/"回工单重拆，确认"判 redo_issues/英文大小写；
2. 节点级（假 interrupt 依次返回预置答复）：
   - confirm（confirmed/空/确认/发布）只留痕、不写意见字段、plan 原样保留；
   - feedback 计数 0->1、1->2，意见按固定中文格式包装；
   - 第 3 版（count=2）feedback 触发 escalated 第二次 interrupt，
     二次答复 confirm 落盘 / 带新决策意见继续调（count=3）；
   - 首次回工单（redo=0）清空 plan、redo=1、count=0、issue_revision_feedback 含原话；
   - redo=1 再回工单触发 escalated：二次 confirm 落盘 / 普通意见重调 /
     仍要求回工单转"仍需回工单："前缀意见；
   - 修订升级暂停时二次答复改选回工单（redo=0）正常发起回工单；
3. route_after_launch_confirm 三分支 + 缺字段安全默认；
4. make_launch_plan 返回 launch_revision_feedback=""（块2 补丁：消费即清零）；
   issue_splitting 消费 launch_confirm 回工单写入的 issue_revision_feedback 后清零；
5. build_graph 编译通过且 11 节点含 launch_confirm；
6. run_prd_workflow._build_question launch_confirm 特化文案含"发布计划确认门"；
7. AGENTS.md 静态含 launch_confirm 协助说明。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  C:\\Users\\A\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe tests/test_launch_confirm.py
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

from components.registry import ComponentRegistry  # noqa: E402
from kernel.artifact import ArtifactManager  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.issues import make_issue_splitting  # noqa: E402
from nodes.launch_plan import (  # noqa: E402
    MAX_ESCALATION_DEPTH,
    MAX_LAUNCH_REVISIONS,
    _REDO_ESCALATION_REASON,
    _REVISION_ESCALATION_REASON,
    classify_launch_answer,
    make_launch_confirm,
    make_launch_plan,
    route_after_launch_confirm,
)

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
    "tier": "2",
    "positioning": "帮 PM 把会议纪要从录音变成可分享的文档",
    "workstreams": [{"name": "录音转写线", "owner": "A"}],
}


def confirm_state(**overrides):
    """发布计划确认门入口 state（plan 非空、计数/意见为默认值）。"""
    state = {
        "requirement_name": "demo-req",
        "launch_plan": dict(DEMO_PLAN),
        "launch_revision_count": 0,
        "launch_issue_redo_count": 0,
        "launch_revision_feedback": "",
        "issue_revision_feedback": "",
        "human_feedback": [],
    }
    state.update(overrides)
    return state


def run_confirm_node(state, answers):
    """patch 掉 nodes.launch_plan.interrupt，按 answers 次序回放 resume 值。

    返回 (节点输出 dict, 历次 interrupt 载荷 list)。
    """
    payloads: list[dict] = []
    queue = list(answers)

    def fake_interrupt(value):
        payloads.append(value)
        return queue.pop(0)

    node = make_launch_confirm(None)  # deps 不使用：确认门不调模型、不取依赖
    with patch("nodes.launch_plan.interrupt", side_effect=fake_interrupt):
        out = node(state)
    return out, payloads


def valid_launch_plan():
    """符合 LaunchPlanSchema 的最小合法发布计划（供 launch_plan 节点契约测试）。

    字段对齐 src/components/schemas/launch_plan.py 的 Pydantic 约束
    （含 min_length、嵌套必填字段、Literal tier 等），避免 schema 校验重试。
    """
    return {
        "tier": "2",
        "tier_rationale": "Tier2 标准发布：已有竞品验证，定向宣告即可",
        "positioning": "对于 PM 中饱受会议纪要整理耗时的人，AI 纪要工具能把录音变成可分享文档，与人工整理不同的是 10 分钟出稿",
        "success_metrics": {
            "d7": "试用账号 500",
            "d30": "周活 200",
        },
        "workstreams": [
            {
                "workstream": "产品就绪度",
                "owner": "A",
                "deliverable": "功能验收通过",
                "deadline": "T-7",
                "status": "进行中",
            },
        ],
        "timeline": [
            {
                "t_minus": "T-7",
                "milestone": "功能冻结",
                "owner": "A",
                "is_critical_path": True,
            },
        ],
        "rollback": {
            "stages": [
                {
                    "stage": "beta",
                    "timing": "T-3",
                    "metric": "转写错误率",
                    "threshold": "<1%",
                }
            ],
            "rollback_trigger": "错误率>2%",
            "rollback_steps": ["切流回旧版转写服务", "通知用户"],
            "rollback_owner": "A",
        },
        "go_no_go_checklist": [
            {"item": "转写错误率<1%", "owner": "A"},
        ],
        "on_call": {
            "dashboard_owner": "A",
            "feedback_channels": ["用户群"],
            "oncall_roster": [
                {"day": "Day1", "owner": "A", "focus": "盯错误率"},
            ],
            "first_retro_date": "T+7",
        },
        "risks": [
            {
                "risk": "转写不准",
                "mitigation": "人工抽检 10%",
                "early_warning": "抽检准确率<90%",
            }
        ],
        "tier1_extension": None,
    }


# ────────────────────────── 1. 分类纯函数 ──────────────────────────


class TestClassifyLaunchAnswer(unittest.TestCase):
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
            "同意发布",
            "发布",
            "发布吧",
            "可以发布",
        ):
            self.assertEqual(classify_launch_answer(word), "confirm", msg=word)

    def test_empty_string_is_confirm(self):
        # 空串=确认，与 requirement_confirm / issue_confirm 空答复放行一致
        self.assertEqual(classify_launch_answer(""), "confirm")
        self.assertEqual(classify_launch_answer("   "), "confirm")

    def test_redo_issues_keywords(self):
        for word in (
            "回工单",
            "回到工单",
            "重拆工单",
            "重新拆单",
            "重拆单",
            "回拆单",
            "回工单拆",
            "重拆",
        ):
            self.assertEqual(classify_launch_answer(word), "redo_issues", msg=word)

    def test_qualified_permission_is_feedback(self):
        # 精确匹配："可以，但要改"不得判确认
        self.assertEqual(classify_launch_answer("可以，但 T-3 灰度太高"), "feedback")
        self.assertEqual(classify_launch_answer("不通过，要改"), "feedback")

    def test_redo_issues_with_confirm_word_priority(self):
        # 回工单包含判定优先于确认精确匹配
        self.assertEqual(classify_launch_answer("回工单重拆，确认"), "redo_issues")

    def test_english_case_insensitive(self):
        self.assertEqual(classify_launch_answer("OK"), "confirm")
        self.assertEqual(classify_launch_answer(" Confirmed "), "confirm")
        self.assertEqual(classify_launch_answer("YES"), "confirm")

    def test_plain_text_defaults_feedback(self):
        self.assertEqual(
            classify_launch_answer("T-3 的灰度比例从 10% 调到 5%"), "feedback"
        )
        self.assertEqual(classify_launch_answer(None), "confirm")  # None 安全归一为空=确认

    def test_negated_redo_issues_is_feedback(self):
        # [C 2026-09-11] 紧邻否定语的"回工单/重拆工单"是"不回工单"，不得判 redo_issues
        self.assertEqual(
            classify_launch_answer("不用回工单，直接改计划"), "feedback"
        )
        self.assertEqual(
            classify_launch_answer("不要重拆工单"), "feedback"
        )
        # 顺带覆盖其他否定语，确保落 feedback 而非误判回工单
        self.assertEqual(classify_launch_answer("不必回工单"), "feedback")
        self.assertEqual(classify_launch_answer("不需要回工单"), "feedback")
        self.assertEqual(classify_launch_answer("先别回工单"), "feedback")

    def test_negated_redo_issues_expanded_words(self):
        # [C 2026-09-12 by pi-deepseek-flash] 第③项修复：此前漏配「无需」，
        # 「无需回工单」被误判为 redo_issues；补词后一律落 feedback
        for word in (
            "无需回工单",
            "无需回 工单",
            "无需重拆工单",
            "不需要重新拆单",
        ):
            self.assertEqual(classify_launch_answer(word), "feedback", msg=word)
        # 反向回归：不带否定回工单关键词仍判 redo_issues
        self.assertEqual(classify_launch_answer("需要回工单"), "redo_issues")

    def test_spaced_redo_issues_keywords(self):
        # [C 2026-09-11] 关键词内部带空格（含全角空格）归一化后仍判 redo_issues
        self.assertEqual(classify_launch_answer("回 工单"), "redo_issues")
        self.assertEqual(classify_launch_answer("重拆 工单"), "redo_issues")
        self.assertEqual(classify_launch_answer("回\u3000工单"), "redo_issues")

    def test_negative_prefix_not_over_broad(self):
        # 否定语只看关键词紧邻前 3 字：离得远的否定不得拦截回工单判定
        self.assertEqual(
            classify_launch_answer("这个不用你管，我确定回工单"), "redo_issues"
        )
        self.assertEqual(
            classify_launch_answer("不用回工单那句收回，现在就要回工单"),
            "redo_issues",
        )


# ────────────────────────── 2. 确认门节点级行为 ──────────────────────────


class TestLaunchConfirmNode(unittest.TestCase):
    def test_confirm_variants_keep_plan_and_only_log(self):
        # confirmed / 空 / 确认 / 发布 四种确认：首次载荷是 draft；输出只含 human_feedback，
        # 不写任何意见/计数字段（plan 原样保留在 state），合并后路由落盘
        for answer in ("confirmed", "", "确认", "发布"):
            out, payloads = run_confirm_node(confirm_state(), [answer])
            self.assertEqual(payloads[0]["node"], "launch_confirm")
            self.assertEqual(payloads[0]["status"], "draft")
            self.assertEqual(payloads[0]["requirement_name"], "demo-req")
            self.assertIn("workstreams", payloads[0]["launch_plan"])
            self.assertEqual(set(out.keys()), {"human_feedback"}, msg=answer)
            self.assertEqual(len(out["human_feedback"]), 1, msg=answer)
            self.assertEqual(out["human_feedback"][0]["kind"], "confirm", msg=answer)
            merged = {**confirm_state(), **out}
            self.assertEqual(
                route_after_launch_confirm(merged), "artifact_persist", msg=answer
            )

    def test_feedback_first_round(self):
        out, payloads = run_confirm_node(
            confirm_state(), ["T-3 灰度 10% 太高，调到 5%"]
        )
        self.assertEqual(out["launch_revision_count"], 1)
        feedback = out["launch_revision_feedback"]
        self.assertTrue(feedback.startswith("【第1轮发布计划修改意见】"))
        self.assertIn("T-3 灰度 10% 太高，调到 5%", feedback)
        self.assertIn("未要求改的部分保持稳定", feedback)
        self.assertNotIn("launch_plan", out)  # plan 保留在 state，不动它
        self.assertEqual(out["human_feedback"][-1]["kind"], "feedback")
        # 合并后条件边回 launch_plan 重调
        self.assertEqual(route_after_launch_confirm({**confirm_state(), **out}),
                         "launch_plan")

    def test_feedback_second_round(self):
        out, _ = run_confirm_node(
            confirm_state(launch_revision_count=1), ["第二版还要把回滚触发降到 1%"]
        )
        self.assertEqual(out["launch_revision_count"], 2)
        self.assertTrue(out["launch_revision_feedback"].startswith("【第2轮发布计划修改意见】"))
        self.assertIn("回滚触发降到 1%", out["launch_revision_feedback"])

    def test_third_version_escalation_then_confirm(self):
        # count=2（第 3 版）仍提意见 -> 第二次 interrupt，载荷 status=escalated、
        # reason 含"升级"与三选项；二次答复确认 -> 落盘
        out, payloads = run_confirm_node(
            confirm_state(launch_revision_count=MAX_LAUNCH_REVISIONS),
            ["第三版还不满意-AAA", "确认"],
        )
        self.assertEqual(len(payloads), 2)
        esc = payloads[1]
        self.assertEqual(esc["status"], "escalated")
        self.assertEqual(esc["node"], "launch_confirm")
        self.assertIn("升级", esc["reason"])
        self.assertIn("workstreams", esc["launch_plan"])
        self.assertIn("prior_feedbacks", esc)
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(len(out["human_feedback"]), 2)  # 首次意见 + 二次确认都留痕
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")
        self.assertEqual(
            route_after_launch_confirm({**confirm_state(launch_revision_count=2), **out}),
            "artifact_persist",
        )

    def test_third_version_escalation_then_new_opinion(self):
        # 升级暂停后，人带来新决策的具体意见 -> 主动再调一轮（count=3），
        # 意见用第二次答复文本（首次意见不泄漏进反馈）
        out, payloads = run_confirm_node(
            confirm_state(launch_revision_count=MAX_LAUNCH_REVISIONS),
            ["第三版还不满意-AAA", "新决策：本期不做灰度改为全量发布-BBB"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["launch_revision_count"], 3)
        feedback = out["launch_revision_feedback"]
        self.assertTrue(feedback.startswith("【第3轮发布计划修改意见】"))
        self.assertIn("BBB", feedback)
        self.assertNotIn("AAA", feedback)
        self.assertEqual(
            route_after_launch_confirm({**confirm_state(launch_revision_count=2), **out}),
            "launch_plan",
        )

    def test_redo_issues_first_time(self):
        # redo=0 首次回工单：清空 plan、redo 置 1、发布计划修订计数归零、
        # launch_revision_feedback 清空、issue_revision_feedback 含用户原话
        out, _ = run_confirm_node(
            confirm_state(), ["回工单，工单 I1 粒度太粗需要重新拆-CCC"]
        )
        self.assertEqual(out["launch_plan"], {})
        self.assertEqual(out["launch_issue_redo_count"], 1)
        self.assertEqual(out["launch_revision_count"], 0)
        self.assertEqual(out["launch_revision_feedback"], "")
        redo_text = out["issue_revision_feedback"]
        self.assertIn("工单 I1 粒度太粗需要重新拆-CCC", redo_text)
        self.assertIn("完整的工单拆解方案", redo_text)
        self.assertIn("重新生成发布计划", redo_text)
        # [C 2026-09-11] 阻断1回归：原话只由 handle_redo_issues 统一留痕一次，
        # draft 分支不得再 append 一遍
        self.assertEqual(len(out["human_feedback"]), 1)
        log = out["human_feedback"][0]
        self.assertEqual(log["kind"], "redo_issues")
        self.assertEqual(log["round"], "redo-1")
        self.assertIn("工单 I1 粒度太粗需要重新拆-CCC", log["feedback"])
        # 合并后条件边去 issue_splitting 重拆
        self.assertEqual(route_after_launch_confirm({**confirm_state(), **out}),
                         "issue_splitting")

    def test_redo_issues_second_time_escalates_then_confirm(self):
        # redo=1 再回工单 -> escalated；二次 confirm -> 按当前版落盘（不再回工单）
        out, payloads = run_confirm_node(
            confirm_state(launch_issue_redo_count=1),
            ["回工单", "确认"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertIn("回工单重拆 1 次", payloads[1]["reason"])
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")
        merged = {**confirm_state(launch_issue_redo_count=1), **out}
        self.assertEqual(route_after_launch_confirm(merged), "artifact_persist")

    def test_redo_issues_second_time_then_feedback(self):
        # redo=1 升级后，二次给普通发布计划意见 -> 按意见重调（不再回工单）
        out, payloads = run_confirm_node(
            confirm_state(launch_issue_redo_count=1),
            ["回工单", "把 T-3 灰度改为 5%-DDD"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["launch_revision_count"], 1)
        self.assertIn("DDD", out["launch_revision_feedback"])
        self.assertNotIn("issue_revision_feedback", out)

    def test_redo_issues_insist_twice_becomes_prefixed_feedback(self):
        # redo=1 升级后二次仍要求回工单 -> 不再次回 issue_splitting，
        # 转"仍需回工单："前缀的发布计划意见
        out, payloads = run_confirm_node(
            confirm_state(launch_issue_redo_count=1),
            ["回工单", "回工单重拆"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["launch_revision_count"], 1)
        self.assertIn("仍需回工单：回工单重拆", out["launch_revision_feedback"])
        self.assertNotIn("issue_revision_feedback", out)

    def test_revision_escalation_second_answer_redo_issues_redo_zero(self):
        # 第 3 版升级暂停时，二次答复改选回工单，且 redo=0 -> 正常发起唯一一次回工单
        out, payloads = run_confirm_node(
            confirm_state(launch_revision_count=MAX_LAUNCH_REVISIONS),
            ["第三版意见-AAA", "回工单"],
        )
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(out["launch_plan"], {})
        self.assertEqual(out["launch_issue_redo_count"], 1)
        self.assertEqual(out["launch_revision_count"], 0)
        self.assertTrue(out["issue_revision_feedback"])
        # [C 2026-09-11] 阻断1回归：首次意见 1 条 + 回工单由 handle_redo_issues
        # 统一留痕 1 条（round=redo-1），同一原话不得出现两条 redo_issues
        self.assertEqual(len(out["human_feedback"]), 2)
        kinds = [item["kind"] for item in out["human_feedback"]]
        self.assertEqual(kinds, ["feedback", "redo_issues"])
        rounds = [item["round"] for item in out["human_feedback"]]
        self.assertEqual(rounds, ["draft-feedback-3", "redo-1"])

    # ── [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级深度硬上限 ──

    def test_escalation_depth_cap_stops_auto_rework(self):
        # 已达升级深度上限仍继续喂意见：暂停循环反复抛同一 escalated 中断等真人输入，
        # 前几次意见都停在 escalated、不自动重调；只有最后一次「确认」才跳出循环落盘。
        # [C 2026-09-12 by pi-deepseek-flash-r2] 第⑥项返工：非确认不得静默落盘。
        state = confirm_state(
            launch_revision_count=MAX_LAUNCH_REVISIONS,
            launch_escalation_depth=MAX_ESCALATION_DEPTH,
        )
        out, payloads = run_confirm_node(
            state,
            [
                "第四版还不满意-AAA",
                "再来一轮-BBB",
                "第三次意见-CCC",
                "确认",
            ],
        )
        # draft -> 上限暂停 3 次；前两次意见各停一轮，最后一次确认才结束
        self.assertEqual(len(payloads), 4)
        for payload in payloads[1:]:
            self.assertEqual(payload["status"], "escalated")
            self.assertIn("人工介入上限", payload["reason"])
            self.assertIn("线下核实", payload["reason"])
        self.assertNotIn("launch_revision_feedback", out)
        self.assertNotIn("launch_revision_count", out)
        self.assertNotIn("launch_escalation_depth", out)
        # 四次喂入答复恰好各留痕一条，末条才是确认
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(len(out["human_feedback"]), 4)
        self.assertEqual(
            [item["kind"] for item in out["human_feedback"]],
            ["feedback", "feedback", "feedback", "confirm"],
        )
        self.assertEqual(
            route_after_launch_confirm(
                {**confirm_state(launch_revision_count=2), **out}
            ),
            "artifact_persist",
        )

    def test_escalation_depth_cap_confirm_lands(self):
        # 上限暂停后回「确认」仍可按当前版落盘
        state = confirm_state(
            launch_revision_count=MAX_LAUNCH_REVISIONS,
            launch_escalation_depth=MAX_ESCALATION_DEPTH,
        )
        out, payloads = run_confirm_node(state, ["还有意见-AAA", "确认"])
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")

    def test_escalation_recursion_bounded_by_depth_cap(self):
        # 回工单额度用尽 + 3 版保险丝：连续喂意见时升级深度递增，
        # 达上限后不再自动重调，而是反复停在 escalated 等真人决策；
        # 只有最后的「确认」才落盘。
        # [C 2026-09-12 by pi-deepseek-flash-r2] 第⑥项返工：末端改停 escalated。
        state = confirm_state(
            launch_issue_redo_count=1,
            launch_revision_count=MAX_LAUNCH_REVISIONS,
            launch_escalation_depth=0,
        )
        out, payloads = run_confirm_node(
            state, ["回工单", "新决策意见-A", "仍然不同意-B", "确认"]
        )
        self.assertEqual(len(payloads), 4)  # draft -> redo升级 -> 上限暂停 x2
        self.assertEqual(payloads[1]["status"], "escalated")
        self.assertIn("回工单重拆 1 次", payloads[1]["reason"])
        # 达上限后的两次喂入意见都停在 escalated，不自动递归重调
        self.assertEqual(payloads[2]["status"], "escalated")
        self.assertIn("人工介入上限", payloads[2]["reason"])
        self.assertEqual(payloads[3]["status"], "escalated")
        self.assertIn("人工介入上限", payloads[3]["reason"])
        # 未产出重调意见 -> 只写 human_feedback，确认后才落盘
        self.assertNotIn("launch_revision_feedback", out)
        self.assertEqual(set(out.keys()), {"human_feedback"})
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")
        self.assertEqual(
            route_after_launch_confirm({**state, **out}), "artifact_persist"
        )


# ────────────────────────── 3. 条件边路由 ──────────────────────────


class TestRouteAfterLaunchConfirm(unittest.TestCase):
    def test_three_branches(self):
        # 回工单：plan 空 + issue_revision_feedback 非空 -> issue_splitting
        self.assertEqual(
            route_after_launch_confirm(
                {"launch_plan": {}, "issue_revision_feedback": "回工单意见",
                 "launch_revision_feedback": ""}
            ),
            "issue_splitting",
        )
        # 发布计划意见：launch_revision_feedback 非空 -> launch_plan
        self.assertEqual(
            route_after_launch_confirm(
                {"launch_plan": {"tier": "2"}, "issue_revision_feedback": "",
                 "launch_revision_feedback": "【第1轮发布计划修改意见】x"}
            ),
            "launch_plan",
        )
        # 确认：plan 非空、两个意见皆空 -> artifact_persist
        self.assertEqual(
            route_after_launch_confirm(
                {"launch_plan": {"tier": "2"},
                 "issue_revision_feedback": "", "launch_revision_feedback": ""}
            ),
            "artifact_persist",
        )

    def test_empty_state_safe_defaults_to_persist(self):
        # 缺字段（None/缺键）不应炸，默认落盘
        self.assertEqual(route_after_launch_confirm({}), "artifact_persist")
        self.assertEqual(
            route_after_launch_confirm({"launch_plan": None, "issue_revision_feedback": None}),
            "artifact_persist",
        )


# ────────────────────────── 4. 跨节点契约（消费即清零）──────────────────────────


class TestConsumeAndClearContracts(unittest.TestCase):
    def test_launch_plan_returns_clearing_field(self):
        # [C 2026-09-11] 块2 补丁：launch_plan 节点返回时清零 launch_revision_feedback，
        # 确认门条件边才不会把已消化的意见再次路由回 launch_plan
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(json_queue=[valid_launch_plan()])
            deps = make_deps(Path(tmp), fake)
            state = {
                "requirement_name": "demo-req",
                "prd_markdown": "# demo PRD",
                "issue_plan": {"issues": [{"id": "I1"}]},
                # 模拟带着上一轮意见进来
                "launch_revision_feedback": "【第1轮发布计划修改意见】陈旧意见",
            }
            out = make_launch_plan(deps)(state)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(out["launch_revision_feedback"], "")
            self.assertTrue(out["launch_plan"]["workstreams"])

    def test_issue_splitting_clears_launch_redo_feedback(self):
        # [C 2026-09-11] launch_confirm 回工单写入的 issue_revision_feedback
        # 由 issue_splitting 消费即清零（issue_splitting 已在 S023 块2 实现清零，
        # 此处验证 launch_confirm → issue_splitting 的回工单意见链条闭环）
        with tempfile.TemporaryDirectory() as tmp:
            # issue_splitting 需要 valid issue plan JSON（对齐 IssueSplittingSchema，
            # 避免 schema 校验重试耗尽假 LLM 队列）
            valid_issue_plan = {
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
            fake = FakeLLM(json_queue=[valid_issue_plan])
            deps = make_deps(Path(tmp), fake)
            state = {
                "requirement_name": "demo-req",
                "prd_markdown": "# demo PRD\n\n## 主流程\n登录",
                "red_team_review": {"verdict": "pass", "warnings": [], "blockers": []},
                # 模拟 launch_confirm 回工单写入的意见
                "issue_revision_feedback": "【发布计划阶段回工单意见】陈旧回工单意见",
                "prd_rewrite_feedback": "",
            }
            out = make_issue_splitting(deps)(state)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(out["issue_revision_feedback"], "")
            self.assertEqual(out["prd_rewrite_feedback"], "")
            self.assertTrue(out["issue_plan"]["issues"])


# ────────────────────────── 5. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_eleven_nodes_and_launch_confirm(self):
        # [C 2026-09-11] 块2 新增 launch_confirm：launch_plan -> launch_confirm
        # -> 条件边三分支（确认落盘 / 意见回 launch_plan 重调 / 回工单回 issue_splitting）
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
                "issue_confirm",     # 块2 工单确认门
                "launch_plan",       # 块1 发布计划节点
                "launch_confirm",    # [C 2026-09-11] 块2 发布计划确认门
                "artifact_persist",
            ):
                self.assertIn(name, names)
            drawn = graph.get_graph().draw_mermaid()
            # launch_confirm 三分支目标都在图上
            for token in ("launch_confirm", "launch_plan", "issue_splitting",
                          "artifact_persist"):
                self.assertIn(token, drawn)


# ────────────────────────── 6/7. 流水线文案 + AGENTS.md ──────────────────────────


class TestWorkflowQuestionAndAgentsDoc(unittest.TestCase):
    @staticmethod
    def _load_workflow_module():
        """以独立模块名加载 scripts/run_prd_workflow.py（加载不触发 main）。"""
        script_path = REPO_ROOT / "scripts" / "run_prd_workflow.py"
        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test_launch", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_build_question_specialized_and_agents_doc(self):
        module = self._load_workflow_module()

        # status 缺省按 draft 文案处理
        question = module._build_question("launch_confirm", {}, {})
        self.assertIn("发布计划确认门", question)
        self.assertIn("回工单", question)
        self.assertIn("升级暂停", question)
        # 旧的 issue_confirm / requirement_confirm 特化不受影响
        self.assertIn(
            "工单确认门", module._build_question("issue_confirm", {}, {})
        )
        self.assertIn(
            "需求确认门", module._build_question("requirement_confirm", {}, {})
        )

        agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("launch_confirm", agents_md)
        self.assertIn("发布计划确认门", agents_md)
        self.assertIn("status=escalated", agents_md)
        self.assertIn("回工单", agents_md)

    def test_build_question_draft_vs_escalated_copy(self):
        # [C 2026-09-11] 阻断2b：draft 与 escalated 文案不同；
        # escalated（含回工单额度用尽）不承诺一定可回工单，语义以 reason 为准
        module = self._load_workflow_module()
        draft_q = module._build_question(
            "launch_confirm", {"status": "draft"}, {}
        )
        revision_esc = module._build_question(
            "launch_confirm",
            {"status": "escalated", "reason": _REVISION_ESCALATION_REASON},
            {},
        )
        redo_esc = module._build_question(
            "launch_confirm",
            {"status": "escalated", "reason": _REDO_ESCALATION_REASON},
            {},
        )
        self.assertNotEqual(draft_q, revision_esc)
        self.assertNotEqual(draft_q, redo_esc)
        # draft 保持原三选一文案（仍承诺可回工单 1 次）
        self.assertIn("回 issue_splitting 重拆（限1次）", draft_q)
        # escalated 两场景：指向 reason、不再承诺回工单
        for q in (revision_esc, redo_esc):
            self.assertIn("升级暂停", q)
            self.assertIn("reason", q)
            self.assertIn("以 reason 的说明为准", q)
            self.assertNotIn("回 issue_splitting 重拆（限1次）", q)
            self.assertIn("不再承诺", q)

    def test_emit_hitl_escalated_exposes_status_reason_priors(self):
        # [C 2026-09-11] 阻断2a：escalated 载荷经 _emit_hitl 必须向 Pi 透传
        # status / reason / prior_feedbacks（list 走 JSON），且不重复打印
        module = self._load_workflow_module()
        payload = {
            "node": "launch_confirm",
            "status": "escalated",
            "reason": _REDO_ESCALATION_REASON,
            "requirement_name": "demo-req",
            "launch_plan": {"tier": "2", "workstreams": [{"name": "x"}]},
            "prior_feedbacks": [
                {"kind": "feedback", "round": "draft-feedback-1",
                 "feedback": "把 T-3 灰度调到 5%"}
            ],
        }
        graph = MagicMock()
        graph.get_state.return_value.values = {}
        buf = io.StringIO()
        with redirect_stdout(buf):
            module._emit_hitl(graph, {}, "tid-launch-1", payload)
        out = buf.getvalue()
        self.assertIn("STATUS: HITL", out)
        self.assertIn("NODE: launch_confirm", out)
        self.assertIn("QUESTION:", out)
        self.assertIn("status: escalated", out)
        self.assertIn("reason:", out)
        self.assertIn("prior_feedbacks:", out)
        self.assertIn("draft-feedback-1", out)  # list 已 JSON 展开
        # QUESTION 是升级暂停文案，不向 Pi 承诺一定还能回工单
        self.assertIn("升级暂停", out)
        self.assertNotIn("回 issue_splitting 重拆（限1次）", out)
        # 三个升级字段各只出现一次标题行（防 draft 时代重复 append 回归）
        self.assertEqual(out.count("status: escalated"), 1)
        self.assertEqual(out.count("prior_feedbacks:"), 1)

    def test_emit_hitl_does_not_pollute_requirement_confirm(self):
        # [C 2026-09-11] 阻断2a：升级三字段仅 issue_confirm / launch_confirm 输出；
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
