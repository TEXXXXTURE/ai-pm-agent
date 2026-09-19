# 拆研发工单（issue_splitting）自测
"""issue_splitting 节点零成本自测：全部使用可编排 FakeLLM，不发起任何真实模型调用。

覆盖：
1. judge_issue_plan 硬判纯函数（合法/重复 id/悬空引用/自引用/循环依赖/
   covered 空引用/covered 悬空/excluded 无 notes/非 blocked 空 issues/
   blocked 空 issues 合法/横切标题警告/超 12 警告/HITL 双空警告/覆盖扇出>3 警告）；
2. 节点级：首轮合法调 1 次；首轮错二轮合法调 2 次且 self_fixed=True；两轮均错调 2 次不抛异常；
3. schema：registry 约定加载、HITL 无 decision_needed 被 pydantic 拒、id 格式/验收条数边界；
4. build_graph 图接线编译（9 节点集合含 issue_splitting 与 issue_confirm 工单确认门）；
5. issues.md.j2 模板四场景（合法/blocked 空工单/有 shape 错误/版本地图）；
6. artifact_persist 工单落盘 + 空 plan 容错不影响其他三份产物；
7. issue_splitting.md 修改意见条件块（非空注入/恒空不渲染/评审 warnings 消化）；
8. route_after_review 通过分支指向 issue_splitting。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python tests/test_issue_splitting.py
也可用 pytest 收集（无 pytest 时直接脚本运行，仅依赖标准库 unittest）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows GBK 控制台兜底
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
from nodes.artifact import make_artifact_persist  # noqa: E402
from nodes.issues import (  # noqa: E402
    judge_issue_plan,
    make_issue_splitting,
    parse_summary_counts,
)
from nodes.review import route_after_review  # noqa: E402

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"


# ────────────────────────── 测试替身与夹具 ──────────────────────────


class FakeLLM:
    """按调用次序返回预制 JSON 响应的假模型（零网络、零花费）。"""

    def __init__(self, json_queue=None):
        self.json_queue = list(json_queue or [])
        self.calls = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        return self.json_queue.pop(0)


def make_issue(iid="I1", **overrides):
    """构造一张默认合法的 AFK 纵切工单。"""
    base = {
        "id": iid,
        "title": "用户用手机号登录后看到首页",
        "issue_type": "AFK",
        "decision_needed": "",
        "priority": "P0",
        "labels": ["账号"],
        "source_sections": ["## 主流程"],
        "user_value": "注册用户能进入自己的工作台",
        "what_to_build": "手机号加验证码登录，成功跳首页，错误原位提示",
        "acceptance_criteria": ["正确验证码登录后跳转首页", "错误验证码原位提示不清空手机号"],
        "verification": "测试环境用预设验证码演示主路径与错误路径",
        "blocked_by": [],
        "open_questions": [],
    }
    base.update(overrides)
    return base


def plan_payload(issues=None, coverage=None, readiness="pass", **overrides):
    """构造默认符合 IssueSplittingSchema 的完整工单方案。"""
    if issues is None:
        issues = [make_issue()]
    if coverage is None:
        coverage = [
            {"prd_item": "手机号登录（## 主流程）", "status": "covered", "covered_by": ["I1"], "notes": ""}
        ]
    base = {
        "readiness": readiness,
        "readiness_notes": "默认 pass：评审已通过，工单可开工；优先级口径 P0/P1/P2",
        "assumptions": ["假设验证码通道沿用既有短信服务"],
        "version_map": [],
        "issues": issues,
        "coverage": coverage,
        "summary": "1 张 AFK，整体可开工",
    }
    base.update(overrides)
    return base


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(runner=runner, registry=registry, artifacts=artifacts, kb=None)


def node_state():
    return {
        "requirement_name": "demo-req",
        "prd_markdown": "# demo PRD\n\n## 主流程\n手机号登录",
        "red_team_review": {"verdict": "pass", "warnings": [], "blockers": []},
        "issue_revision_feedback": "",
    }


# ────────────────────────── 1. judge 纯函数 ──────────────────────────


class TestJudgeIssuePlan(unittest.TestCase):
    def test_legal_plan_no_errors_no_warnings(self):
        judged = judge_issue_plan(plan_payload())
        self.assertEqual(judged["errors"], [])
        self.assertEqual(judged["warnings"], [])

    def test_duplicate_id(self):
        judged = judge_issue_plan(plan_payload(issues=[make_issue("I1"), make_issue("I1")]))
        self.assertTrue(any("重复" in e and "I1" in e for e in judged["errors"]))

    def test_dangling_blocked_by(self):
        judged = judge_issue_plan(
            plan_payload(issues=[make_issue("I1"), make_issue("I2", blocked_by=["I9"])])
        )
        self.assertTrue(any("悬空" in e and "I9" in e for e in judged["errors"]))

    def test_self_reference(self):
        judged = judge_issue_plan(plan_payload(issues=[make_issue("I1", blocked_by=["I1"])]))
        self.assertTrue(any("自引用" in e for e in judged["errors"]))

    def test_dependency_cycle(self):
        judged = judge_issue_plan(
            plan_payload(
                issues=[
                    make_issue("I1", blocked_by=["I2"]),
                    make_issue("I2", blocked_by=["I1"]),
                ],
                coverage=[
                    {"prd_item": "功能A", "status": "covered", "covered_by": ["I1"], "notes": ""}
                ],
            )
        )
        self.assertTrue(any("循环依赖" in e for e in judged["errors"]))
        # 环上的边都不是悬空，不应再报悬空
        self.assertFalse(any("悬空" in e for e in judged["errors"]))

    def test_covered_with_empty_refs(self):
        judged = judge_issue_plan(
            plan_payload(
                coverage=[
                    {"prd_item": "功能A", "status": "covered", "covered_by": [], "notes": ""}
                ]
            )
        )
        self.assertTrue(any("covered_by 为空" in e for e in judged["errors"]))

    def test_covered_with_dangling_ref(self):
        judged = judge_issue_plan(
            plan_payload(
                coverage=[
                    {"prd_item": "功能A", "status": "covered", "covered_by": ["I9"], "notes": ""}
                ]
            )
        )
        self.assertTrue(any("I9" in e and "covered_by" in e for e in judged["errors"]))

    def test_excluded_without_notes(self):
        judged = judge_issue_plan(
            plan_payload(
                coverage=[
                    {"prd_item": "微信登录", "status": "excluded", "covered_by": [], "notes": ""}
                ]
            )
        )
        self.assertTrue(any("excluded" in e and "排除原因" in e for e in judged["errors"]))

    def test_pass_with_empty_issues_errors(self):
        judged = judge_issue_plan(plan_payload(issues=[], readiness="pass"))
        self.assertTrue(any("issues 为空" in e for e in judged["errors"]))

    def test_needs_clarification_empty_issues_errors(self):
        judged = judge_issue_plan(plan_payload(issues=[], readiness="needs_clarification"))
        self.assertTrue(any("issues 为空" in e for e in judged["errors"]))

    def test_blocked_empty_issues_legal(self):
        judged = judge_issue_plan(
            plan_payload(
                issues=[],
                coverage=[
                    {"prd_item": "全部功能", "status": "clarify", "covered_by": [], "notes": "待补核心口径"}
                ],
                readiness="blocked",
                readiness_notes="核心范围未拍板，整份方案阻断",
            )
        )
        self.assertEqual(judged["errors"], [])

    def test_horizontal_slice_title_warning(self):
        judged = judge_issue_plan(
            plan_payload(issues=[make_issue("I1", title="做登录页面前端")])
        )
        self.assertEqual(judged["errors"], [])
        self.assertTrue(any("横切票" in w for w in judged["warnings"]))

    def test_too_many_issues_warning(self):
        issues = [make_issue(f"I{i}") for i in range(1, 14)]  # 13 张
        judged = judge_issue_plan(plan_payload(issues=issues))
        self.assertTrue(any("13" in w and "粒度" in w for w in judged["warnings"]))

    def test_hitl_both_empty_warning(self):
        judged = judge_issue_plan(
            plan_payload(
                issues=[
                    make_issue(
                        "I1",
                        issue_type="HITL",
                        decision_needed="",
                        open_questions=[],
                    )
                ]
            )
        )
        # judge 层只告警（schema 层才校验拒绝），不阻断
        self.assertTrue(any("HITL" in w for w in judged["warnings"]))

    def test_coverage_fanout_over_three_warning(self):
        issues = [make_issue(f"I{i}") for i in range(1, 5)]
        judged = judge_issue_plan(
            plan_payload(
                issues=issues,
                coverage=[
                    {"prd_item": "功能A", "status": "covered", "covered_by": ["I1", "I2", "I3", "I4"], "notes": ""}
                ],
            )
        )
        self.assertEqual(judged["errors"], [])
        self.assertTrue(any("共同覆盖" in w for w in judged["warnings"]))

    # summary 自报计数校验

    def test_parse_summary_counts(self):
        # 常见写法：N 张 AFK / AFK N 张 / 共 N 张 / N 张工单
        self.assertEqual(
            parse_summary_counts("3 张 AFK、1 张 HITL，共 4 张"),
            {"AFK": 3, "HITL": 1, "TOTAL": 4},
        )
        self.assertEqual(parse_summary_counts("AFK 2 张"), {"AFK": 2})
        self.assertEqual(parse_summary_counts("9 张工单，整体可开工"), {"TOTAL": 9})
        self.assertEqual(parse_summary_counts("无法给出数量"), {})
        self.assertEqual(parse_summary_counts(""), {})

    def test_summary_self_report_mismatch_warns(self):
        # 实际 2 张全 AFK，summary 却自报 9 张/3 AFK/1 HITL -> 告警但不阻断、不加 error
        issues = [make_issue("I1"), make_issue("I2")]
        judged = judge_issue_plan(
            plan_payload(
                issues=issues,
                coverage=[
                    {"prd_item": "x", "status": "covered", "covered_by": ["I1"], "notes": ""}
                ],
                summary="共 9 张，其中 3 张 AFK、1 张 HITL，整体可开工",
            )
        )
        self.assertEqual(judged["errors"], [])
        mismatch = [
            w for w in judged["warnings"] if "自报计数与实际工单不一致" in w
        ]
        self.assertEqual(len(mismatch), 1)
        self.assertIn("工单总数自报 9，实际 2", mismatch[0])
        self.assertIn("AFK 数自报 3，实际 2", mismatch[0])
        self.assertIn("HITL 数自报 1，实际 0", mismatch[0])

    def test_summary_self_report_correct_no_warning(self):
        # 自报与实际一致 -> 不得出现计数告警
        issues = [
            make_issue("I1"),
            make_issue(
                "I2",
                issue_type="HITL",
                decision_needed="待拍板退款口径",
                open_questions=["本期是否做退款"],
            ),
        ]
        judged = judge_issue_plan(
            plan_payload(
                issues=issues,
                coverage=[
                    {"prd_item": "x", "status": "covered", "covered_by": ["I1"], "notes": ""}
                ],
                summary="共 2 张，其中 1 张 AFK、1 张 HITL",
            )
        )
        self.assertEqual(judged["errors"], [])
        self.assertFalse(
            any("自报计数与实际工单不一致" in w for w in judged["warnings"])
        )


# ────────────────────────── 2. 节点级行为 ──────────────────────────


class TestIssueSplittingNode(unittest.TestCase):
    def test_first_round_legal_calls_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(json_queue=[plan_payload()])
            deps = make_deps(Path(tmp), fake)
            out = make_issue_splitting(deps)(node_state())

            self.assertEqual(len(fake.calls), 1)
            plan = out["issue_plan"]
            self.assertEqual(plan["shape_errors"], [])
            self.assertFalse(plan["self_fixed"])
            self.assertEqual(len(plan["issues"]), 1)
            # 消费即清零——节点返回两个清零字段，
            # 保证确认门条件边不会把已消化的意见再次路由回重拆/回炉（取代块1"不写回"契约）
            self.assertEqual(out["issue_revision_feedback"], "")
            self.assertEqual(out["prd_rewrite_feedback"], "")

    def test_first_error_second_legal_self_fixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = plan_payload(issues=[make_issue("I1"), make_issue("I2", blocked_by=["I9"])])
            good = plan_payload()
            fake = FakeLLM(json_queue=[bad, good])
            deps = make_deps(Path(tmp), fake)
            out = make_issue_splitting(deps)(node_state())

            self.assertEqual(len(fake.calls), 2)
            # 第二次调用的 prompt 注入了中文结构反馈
            self.assertIn("工单方案结构自检未通过", fake.calls[1]["prompt"])
            self.assertIn("悬空", fake.calls[1]["prompt"])
            plan = out["issue_plan"]
            self.assertEqual(plan["shape_errors"], [])
            self.assertTrue(plan["self_fixed"])

    def test_two_rounds_both_bad_keeps_errors_no_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad1 = plan_payload(issues=[make_issue("I1"), make_issue("I2", blocked_by=["I9"])])
            bad2 = plan_payload(issues=[make_issue("I1"), make_issue("I1")])
            fake = FakeLLM(json_queue=[bad1, bad2])
            deps = make_deps(Path(tmp), fake)
            out = make_issue_splitting(deps)(node_state())  # 不抛异常

            self.assertEqual(len(fake.calls), 2)
            plan = out["issue_plan"]
            self.assertTrue(plan["shape_errors"])  # 终判仍有错，保留交人工
            self.assertTrue(any("重复" in e for e in plan["shape_errors"]))
            self.assertTrue(plan["self_fixed"])


# ────────────────────────── 3. schema 校验 ──────────────────────────


class TestIssueSchema(unittest.TestCase):
    def test_registry_loads_and_legal_payload_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("issue_splitting")
            self.assertEqual(schema_cls.__name__, "IssueSplittingSchema")
            obj = schema_cls.model_validate(plan_payload())
            self.assertEqual(obj.issues[0].id, "I1")

    def test_hitl_without_decision_needed_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("issue_splitting")
            bad = plan_payload(
                issues=[make_issue("I1", issue_type="HITL", decision_needed="", open_questions=[])]
            )
            with self.assertRaises(Exception):
                schema_cls.model_validate(bad)
            # 对照：写清 decision_needed 的 HITL 合法
            ok = plan_payload(
                issues=[
                    make_issue(
                        "I1",
                        issue_type="HITL",
                        decision_needed="待财务确定退款口径",
                        open_questions=["优惠券退吗？"],
                    )
                ]
            )
            self.assertEqual(schema_cls.model_validate(ok).issues[0].issue_type, "HITL")

    def test_id_pattern_and_acceptance_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("issue_splitting")
            # id 格式不符 ^I\d+$
            with self.assertRaises(Exception):
                schema_cls.model_validate(plan_payload(issues=[make_issue("TICKET-1")]))
            # blocked_by 元素同样受 id 格式约束
            with self.assertRaises(Exception):
                schema_cls.model_validate(
                    plan_payload(issues=[make_issue("I1"), make_issue("I2", blocked_by=["x9"])])
                )
            # 验收标准少于 2 条
            with self.assertRaises(Exception):
                schema_cls.model_validate(
                    plan_payload(issues=[make_issue("I1", acceptance_criteria=["只有一条"])])
                )
            # coverage 至少 1 条
            with self.assertRaises(Exception):
                schema_cls.model_validate(plan_payload(coverage=[]))


# ────────────────────────── 4. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_has_nine_nodes_and_confirm_gate(self):
        # 后图为 9 节点：issue_splitting 后接 issue_confirm 确认门
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            for n in [
                "kb_lookup",
                "intake",
                "requirement_confirm",
                "needs_discovery",
                "prd_generation",
                "prd_review",
                "issue_splitting",
                "issue_confirm",  # 块2 新增工单确认门（第 9 个节点）
                "artifact_persist",
            ]:
                self.assertIn(n, names)
            drawn = graph.get_graph().draw_mermaid()
            self.assertIn("issue_splitting", drawn)
            self.assertIn("artifact_persist", drawn)

    def test_route_pass_goes_to_issue_splitting(self):
        self.assertEqual(
            route_after_review({"red_team_review": {"verdict": "pass"}}), "issue_splitting"
        )


# ────────────────────────── 5. 模板渲染（四场景）──────────────────────────


def render_issues(deps, plan, prd_filename="launch-smoke-prd.md"):
    # 渲染上下文补 prd_filename
    return deps.artifacts.render(
        "issues.md.j2",
        {
            "requirement_name": "demo-req",
            "generated_at": "2026-09-11",
            "plan": plan,
            "prd_filename": prd_filename,
        },
    )


class TestIssuesTemplate(unittest.TestCase):
    def test_render_legal_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(plan_payload())
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_issues(deps, plan)
            self.assertIn("研发工单清单 — demo-req", md)
            self.assertIn("I1", md)
            self.assertIn("用户用手机号登录后看到首页", md)
            self.assertIn("AFK", md)
            self.assertIn("- [ ]", md)  # 验收标准渲染为勾选框
            self.assertIn("覆盖矩阵", md)
            self.assertIn("无", md)  # 空依赖显示"无"
            self.assertIn("gh issue create", md)  # 末尾发布提示
            self.assertIn("launch-smoke-prd.md", md)  # 来源标注用真实文件名
            self.assertNotIn("来源 PRD：prd.md", md)  # 不再写死 prd.md

    def test_render_missing_prd_filename_falls_back(self):
        # 缺 prd_filename 直接渲染不炸，容错为「未知」
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(plan_payload())
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_issues(deps, plan, prd_filename=None)
            self.assertIn("来源 PRD：未知", md)

    def test_render_blocked_empty_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = plan_payload(
                issues=[],
                coverage=[
                    {"prd_item": "全部", "status": "clarify", "covered_by": [], "notes": "待补口径"}
                ],
                readiness="blocked",
                readiness_notes="核心范围未拍板，阻断",
            )
            md = render_issues(deps, plan)
            self.assertIn("blocked", md)
            self.assertIn("没有可执行工单", md)
            self.assertIn("阻断", md)

    def test_render_shape_errors_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(plan_payload())
            plan.update(
                {
                    "shape_errors": ["工单 I2 的 blocked_by 引用了不存在的工单 I9（悬空依赖）"],
                    "shape_warnings": ["工单 I1 标题疑似横切票"],
                    "self_fixed": True,
                }
            )
            md = render_issues(deps, plan)
            self.assertIn("结构错误", md)
            self.assertIn("I9", md)
            self.assertIn("结构警告", md)
            self.assertIn("横切票", md)
            self.assertIn("已带反馈重生成", md)

    def test_render_version_map_and_hitl(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = plan_payload(
                issues=[
                    make_issue(
                        "I1",
                        title="财务确认口径后用户可申请退款",
                        issue_type="HITL",
                        decision_needed="待财务确定优惠券是否退回",
                        open_questions=["优惠券退吗？"],
                        labels=["交易"],
                    )
                ],
                version_map=[
                    {
                        "version_id": "V1",
                        "outcome": "用户可申请退款",
                        "in_scope": "I1",
                        "out_of_scope": "微信退款",
                        "dependencies": "财务口径",
                        "acceptance": "一笔订单走通退款",
                    }
                ],
            )
            md = render_issues(deps, plan)
            self.assertIn("版本地图", md)
            self.assertIn("V1", md)
            self.assertIn("微信退款", md)
            self.assertIn("HITL", md)
            self.assertIn("待财务确定优惠券是否退回", md)
            self.assertIn("优惠券退吗？", md)


# ────────────────────────── 6. 落盘 ──────────────────────────


def persist_state(issue_plan):
    return {
        "requirement_name": "demo-req",
        "prd_markdown": "# demo-req PRD\n\n正文",
        "user_insights": {},
        "red_team_review": {
            "verdict": "pass",
            "forced": False,
            "round": 0,
            "avg": 4.0,
            "minimum": 4,
            "scores": [],
            "blockers": [],
            "warnings": [],
            "hypotheses": [],
            "summary": "ok",
        },
        "issue_plan": issue_plan,
    }


class TestArtifactPersistIssues(unittest.TestCase):
    def test_issues_artifact_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(plan_payload())
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            out = make_artifact_persist(deps)(persist_state(plan))

            self.assertIn("issues", out["artifacts"])
            issues_file = Path(out["artifacts"]["issues"])
            self.assertTrue(issues_file.exists())
            # 落盘到「研发工单」中文子目录
            self.assertIn("研发工单", str(issues_file))
            content = issues_file.read_text(encoding="utf-8")
            self.assertIn("用户用手机号登录后看到首页", content)
            # 其他三份产物不受影响
            for key in ("prd", "insights", "review"):
                self.assertIn(key, out["artifacts"])

    def test_empty_issue_plan_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            out = make_artifact_persist(deps)(persist_state({}))
            self.assertNotIn("issues", out["artifacts"])
            for key in ("prd", "insights", "review"):
                self.assertIn(key, out["artifacts"])


# ────────────────────────── 7. prompt 条件块 ──────────────────────────


class TestIssuePromptConditional(unittest.TestCase):
    def test_feedback_and_review_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("issue_splitting")
            rendered = Template(raw).render(
                requirement_name="demo-req",
                prd_markdown="# demo PRD",
                red_team_review={
                    "verdict": "pass_with_warning",
                    "avg": 3.6,
                    "blockers": [],
                    "warnings": [
                        {"severity": "建议", "location": "## 退款", "issue": "退款口径模糊", "suggestion": "补例子"}
                    ],
                },
                issue_revision_feedback="请修复悬空引用 I9，并保持其他工单稳定",
            )
            self.assertIn("上一轮修改意见", rendered)
            self.assertIn("请修复悬空引用 I9", rendered)
            # 评审遗留 warnings 段也渲染
            self.assertIn("退款口径模糊", rendered)

    def test_feedback_absent_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("issue_splitting")
            rendered = Template(raw).render(
                requirement_name="demo-req",
                prd_markdown="# demo PRD",
                red_team_review={"verdict": "pass", "blockers": [], "warnings": []},
                issue_revision_feedback="",
            )
            self.assertNotIn("上一轮修改意见", rendered)
            # 关键引导仍在
            self.assertIn("纵切", rendered)
            self.assertIn("AFK", rendered)


# ────────────────────────── 9. AI 轨特殊项承接（块2 S038）──────────────────────────
# 第 7 段：仅 AI 核心需求（ai_core=True）校验
# ai_special_items（trace/fallback/eval_integration/risk_mitigation 四类齐全 +
# covered_by 引用真实工单）；普通轨不产出、不校验。全部零 API（纯函数/模板渲染）。


def ai_special_items_all():
    """构造四类齐全、均引用真实工单 I1 的合法 AI 特殊项声明。"""
    return [
        {"category": "trace", "covered_by": ["I1"], "note": ""},
        {
            "category": "fallback",
            "covered_by": ["I1"],
            "note": "对应 PRD「失败接管」：低置信度或无答案时转人工",
        },
        {"category": "eval_integration", "covered_by": ["I1"], "note": ""},
        {"category": "risk_mitigation", "covered_by": ["I1"], "note": ""},
    ]


def ai_plan(**overrides):
    """构造带 AI 特殊项声明的 AI 轨工单方案（默认四类齐全）。"""
    return plan_payload(ai_special_items=ai_special_items_all(), **overrides)


class TestJudgeAISpecialItems(unittest.TestCase):
    """judge_issue_plan(plan, ai_core=True) 的 AI 特殊项硬判。"""

    def _missing_category_errors(self, category: str):
        items = [it for it in ai_special_items_all() if it["category"] != category]
        judged = judge_issue_plan(plan_payload(ai_special_items=items), ai_core=True)
        return judged["errors"]

    def test_ai_core_missing_items_errors(self):
        # 键缺省（默认 None）：AI 轨必填，报缺失
        judged = judge_issue_plan(plan_payload(), ai_core=True)
        self.assertTrue(
            any("ai_special_items" in e and "缺失" in e for e in judged["errors"])
        )

    def test_ai_core_none_items_errors(self):
        judged = judge_issue_plan(plan_payload(ai_special_items=None), ai_core=True)
        self.assertTrue(any("ai_special_items" in e for e in judged["errors"]))

    def test_ai_core_empty_items_errors(self):
        judged = judge_issue_plan(plan_payload(ai_special_items=[]), ai_core=True)
        self.assertTrue(any("ai_special_items" in e for e in judged["errors"]))

    def test_ai_core_missing_trace_category(self):
        errs = self._missing_category_errors("trace")
        self.assertTrue(any("调用链埋点" in e for e in errs))

    def test_ai_core_missing_fallback_category(self):
        errs = self._missing_category_errors("fallback")
        self.assertTrue(any("兜底" in e for e in errs))

    def test_ai_core_missing_eval_integration_category(self):
        errs = self._missing_category_errors("eval_integration")
        self.assertTrue(any("评测接入" in e for e in errs))

    def test_ai_core_missing_risk_mitigation_category(self):
        errs = self._missing_category_errors("risk_mitigation")
        self.assertTrue(any("风险册" in e for e in errs))

    def test_ai_core_dangling_covered_by_errors(self):
        items = ai_special_items_all()
        items[1]["covered_by"] = ["I9"]  # 第 2 条 fallback 引用不存在工单
        judged = judge_issue_plan(plan_payload(ai_special_items=items), ai_core=True)
        self.assertTrue(any("I9" in e and "悬空" in e for e in judged["errors"]))
        self.assertTrue(any("第 2 条" in e for e in judged["errors"]))

    def test_ai_core_all_categories_covered_no_ai_error(self):
        judged = judge_issue_plan(ai_plan(), ai_core=True)
        self.assertEqual(judged["errors"], [])
        self.assertFalse(any("AI" in w or "特殊项" in w for w in judged["warnings"]))

    def test_ai_core_element_not_dict_treated_as_missing_category(self):
        # 元素非 dict 按"无法识别类别"处理：不抛异常，也不额外报错（四类已齐）
        items = ai_special_items_all() + ["not-a-dict"]
        judged = judge_issue_plan(plan_payload(ai_special_items=items), ai_core=True)
        self.assertEqual(judged["errors"], [])

    def test_ai_core_non_dict_plan_no_raise(self):
        # plan 非 dict 异常形态安全兜底：不抛异常，且报 ai_special_items 缺失
        judged = judge_issue_plan(None, ai_core=True)
        self.assertTrue(any("ai_special_items" in e for e in judged["errors"]))

    def test_normal_track_no_ai_errors_even_without_items(self):
        # 普通轨（默认 ai_core=False）：无 ai_special_items 也不产生任何 AI 相关 error/warning
        judged = judge_issue_plan(plan_payload())
        self.assertEqual(judged["errors"], [])
        self.assertEqual(judged["warnings"], [])
        self.assertFalse(
            any(
                "ai_special_items" in m
                for m in judged["errors"] + judged["warnings"]
            )
        )

    def test_normal_track_ignores_ai_items_even_with_dangling_ref(self):
        # 普通轨即使误带 ai_special_items（含悬空引用）也不校验、不报错
        bad = plan_payload(
            ai_special_items=[{"category": "trace", "covered_by": ["I9"], "note": ""}]
        )
        judged = judge_issue_plan(bad, ai_core=False)
        self.assertEqual(judged["errors"], [])


class TestIssueSplittingNodeAICore(unittest.TestCase):
    """节点级：ai_core 传递到 judge（AI 轨自检重调；普通轨行为不变）。"""

    def test_ai_core_self_fixes_missing_special_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = plan_payload()  # AI 轨首轮缺 ai_special_items -> 触发自检
            good = ai_plan()
            fake = FakeLLM(json_queue=[bad, good])
            deps = make_deps(Path(tmp), fake)
            out = make_issue_splitting(deps)(dict(node_state(), ai_core=True))

            self.assertEqual(len(fake.calls), 2)
            self.assertIn("工单方案结构自检未通过", fake.calls[1]["prompt"])
            self.assertIn("ai_special_items", fake.calls[1]["prompt"])
            plan = out["issue_plan"]
            self.assertEqual(plan["shape_errors"], [])
            self.assertTrue(plan["self_fixed"])

    def test_normal_track_ignores_missing_special_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(json_queue=[plan_payload()])  # 普通轨缺 ai_special_items 无妨
            deps = make_deps(Path(tmp), fake)
            out = make_issue_splitting(deps)(dict(node_state(), ai_core=False))

            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(out["issue_plan"]["shape_errors"], [])
            self.assertFalse(out["issue_plan"]["self_fixed"])


class TestIssueSchemaAISpecialItems(unittest.TestCase):
    """schema 层：ai_special_items 的 Pydantic 结构约束。"""

    def _schema(self, tmp: str):
        return make_deps(Path(tmp)).registry.load_schema("issue_splitting")

    def test_valid_ai_special_items_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self._schema(tmp).model_validate(ai_plan())
            self.assertEqual(obj.ai_special_items[0].category, "trace")
            self.assertEqual(obj.ai_special_items[0].covered_by, ["I1"])

    def test_default_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self._schema(tmp).model_validate(plan_payload())
            self.assertIsNone(obj.ai_special_items)

    def test_illegal_category_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = plan_payload(
                ai_special_items=[
                    {"category": "unknown", "covered_by": ["I1"], "note": ""}
                ]
            )
            with self.assertRaises(Exception):
                self._schema(tmp).model_validate(bad)

    def test_empty_covered_by_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = plan_payload(
                ai_special_items=[
                    {"category": "trace", "covered_by": [], "note": ""}
                ]
            )
            with self.assertRaises(Exception):
                self._schema(tmp).model_validate(bad)

    def test_dangling_id_pattern_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = plan_payload(
                ai_special_items=[
                    {"category": "trace", "covered_by": ["TICKET-1"], "note": ""}
                ]
            )
            with self.assertRaises(Exception):
                self._schema(tmp).model_validate(bad)


class TestIssuesTemplateAISpecialItems(unittest.TestCase):
    """issues.md.j2：AI 特殊项承接表容错渲染（普通轨整节不渲染）。"""

    def test_render_ai_special_items_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = ai_plan()
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_issues(deps, plan)
            self.assertIn("AI 特殊项承接表", md)
            for kw in ("调用链埋点", "兜底与转人工", "评测接入", "风险册承接"):
                self.assertIn(kw, md)
            self.assertIn("对应 PRD「失败接管」", md)

    def test_render_unknown_category_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = plan_payload(
                ai_special_items=[
                    {"category": "weird_x", "covered_by": ["I1"], "note": ""}
                ]
            )
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_issues(deps, plan)
            self.assertIn("AI 特殊项承接表", md)
            self.assertIn("weird_x", md)

    def test_normal_plan_no_ai_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = plan_payload()  # 无 ai_special_items
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_issues(deps, plan)
            self.assertNotIn("AI 特殊项承接表", md)


class TestIssuePromptAICore(unittest.TestCase):
    """issue_splitting.md：ai_core 条件块（AI 轨渲染 / 普通轨逐字不变）。"""

    def _render(self, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            raw = make_deps(Path(tmp)).registry.read_prompt("issue_splitting")
        ctx = dict(
            requirement_name="demo-req",
            prd_markdown="# demo PRD",
            red_team_review={"verdict": "pass", "blockers": [], "warnings": []},
            issue_revision_feedback="",
        )
        return Template(raw).render(**ctx, **extra)

    def test_ai_core_block_rendered(self):
        rendered = self._render(ai_core=True)
        self.assertIn("ai_special_items", rendered)
        for kw in ("调用链埋点", "兜底与转人工", "评测接入", "风险册承接"):
            self.assertIn(kw, rendered)

    def test_normal_track_block_absent_and_identical(self):
        base = self._render(ai_core=False)
        self.assertNotIn("ai_special_items", base)
        self.assertNotIn("本需求为 AI 核心需求", base)
        # 不传 ai_core 与显式 False 逐字一致（普通轨渲染零变化）
        self.assertEqual(self._render(), base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
