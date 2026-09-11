# [C 2026-09-11] 块1 发布计划节点（launch_plan）自测
"""launch_plan 节点零成本自测：全部使用可编排 FakeLLM，不发起任何真实模型调用。

覆盖：
1. judge_launch_plan 硬判纯函数（合法 Tier1/2/3 / 9 个 errors 分支 / 4 个 warnings 分支）；
2. 节点级：首轮合法调 1 次；首轮字段缺失二轮合法调 2 次 self_fixed=True；
   两轮均错调 2 次不抛异常、errors 随产物展示；
3. schema：registry 约定加载、tier 非法值被拒、risks 超 3 条被拒、Tier1 扩展合法通过；
4. build_graph 图接线编译（10 节点集合含 launch_plan，确认分支经 launch_plan 到 artifact_persist）；
5. launch_plan.md.j2 模板三场景（合法 Tier2 / Tier1 含扩展 / shape 错误醒目展示）；
6. artifact_persist 发布计划落盘到「发布计划」中文子目录 + 空 plan 容错不影响其他四份产物；
7. launch_plan.md prompt 条件块（launch_revision_feedback 非空注入/恒空不渲染/
   评审 warnings 消化 / issue_plan 工单清单渲染）。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  C:\\Users\\A\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe tests/test_launch_plan.py
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
from nodes.launch_plan import (  # noqa: E402
    TOO_MANY_WORKSTREAMS,
    judge_launch_plan,
    make_launch_plan,
)

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


def valid_tier1_extension():
    """合法的 Tier1 扩展（滩头/ICP/分受众/渠道排序四件齐）。"""
    return {
        "beachhead": {
            "segment": "10-50 人小团队 PM",
            "rationale": "痛点够痛、愿付费、打得赢、会转介绍",
            "adjacent_expansion": "50-200 人中型团队",
        },
        "icp": {
            "attributes": "10-50 人 SaaS 团队",
            "decision_maker": "产品负责人",
            "jtbd": "把需求拆成可验收工单",
            "current_alternative": "飞书表格",
            "qualifying_signal": "团队有专职 PM 且工单散乱",
        },
        "audience_messages": [
            {"role": "购买者", "message": "减少返工与漏测", "proof_point": "案例 A 返工降 40%"}
        ],
        "channel_ranking": [
            {
                "channel": "产品社区",
                "reach": "5000",
                "cost": "低",
                "priority": "P0",
                "pre_launch_action": "等待名单",
            }
        ],
    }


def valid_plan(tier="2", **overrides):
    """构造默认符合 LaunchPlanSchema 的完整发布计划。"""
    base = {
        "tier": tier,
        "tier_rationale": "面向已知细分人群的重要功能，定向宣告即可",
        "positioning": "对于小团队 PM 饱受工单散乱困扰，本工具带来结构化拆单，与表格不同的是可追溯",
        "success_metrics": {"d7": "日活破 100", "d30": "留存 30%"},
        "workstreams": [
            {
                "workstream": "产品就绪度",
                "owner": "张三",
                "deliverable": "发版候选包通过验收",
                "deadline": "T-7",
                "status": "进行中",
            }
        ],
        "timeline": [
            {
                "t_minus": "T-7",
                "milestone": "发布演练/Bug Bash",
                "owner": "张三",
                "is_critical_path": True,
            },
            {
                "t_minus": "T0",
                "milestone": "灰度开闸",
                "owner": "王五",
                "is_critical_path": False,
            },
        ],
        "rollback": {
            "stages": [
                {
                    "stage": "internal",
                    "timing": "T-3 全员内测",
                    "metric": "P0 bug 数",
                    "threshold": "0 个",
                },
                {
                    "stage": "GA",
                    "timing": "T+3 全量",
                    "metric": "错误率",
                    "threshold": "<2%",
                },
            ],
            "rollback_trigger": "错误率 > 2% 或核心流程转化率下降 > 10%",
            "rollback_steps": ["第一步：值班人确认触发条件达成", "第二步：执行开关回退"],
            "rollback_owner": "王五",
        },
        "go_no_go_checklist": [
            {"item": "帮助文章已发布并完成评审", "owner": "赵六"}
        ],
        "on_call": {
            "dashboard_owner": "王五",
            "feedback_channels": ["应用内反馈入口汇至 #release 渠道，王五分流"],
            "oncall_roster": [
                {"day": "Day1", "owner": "王五", "focus": "盯错误率与核心转化"}
            ],
            "first_retro_date": "T+7（对照 D7 目标做首次复盘）",
        },
        "risks": [
            {
                "risk": "灰度阶段错误率超阈值",
                "mitigation": "每阶段带阈值，达标才进下一阶段",
                "early_warning": "错误率连续 15 分钟 >1%",
            }
        ],
        "tier1_extension": None,
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
        "issue_plan": {
            "readiness": "pass",
            "summary": "1 张 AFK，整体可开工",
            "issues": [
                {"id": "I1", "title": "用户用手机号登录后看到首页", "priority": "P0"}
            ],
        },
        "launch_revision_feedback": "",
    }


# ────────────────────────── 1. judge 纯函数 ──────────────────────────


class TestJudgeLaunchPlan(unittest.TestCase):
    def test_legal_tier2_no_errors_no_warnings(self):
        errors, warnings = judge_launch_plan(valid_plan())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_legal_tier1_with_extension_no_errors(self):
        errors, warnings = judge_launch_plan(
            valid_plan(tier="1", tier1_extension=valid_tier1_extension())
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_legal_tier3_no_errors(self):
        errors, warnings = judge_launch_plan(valid_plan(tier="3"))
        self.assertEqual(errors, [])

    def test_empty_tier(self):
        errors, _ = judge_launch_plan(valid_plan(tier=""))
        self.assertTrue(any("tier 为空" in e for e in errors))

    def test_empty_positioning(self):
        errors, _ = judge_launch_plan(valid_plan(positioning=""))
        self.assertTrue(any("positioning 为空" in e for e in errors))

    def test_empty_workstreams(self):
        errors, _ = judge_launch_plan(valid_plan(workstreams=[]))
        self.assertTrue(any("workstreams 为空" in e for e in errors))

    def test_empty_timeline(self):
        errors, _ = judge_launch_plan(valid_plan(timeline=[]))
        self.assertTrue(any("timeline 为空" in e for e in errors))

    def test_empty_rollback(self):
        errors, _ = judge_launch_plan(valid_plan(rollback={}))
        self.assertTrue(any("rollback 为空" in e for e in errors))

    def test_empty_go_no_go_checklist(self):
        errors, _ = judge_launch_plan(valid_plan(go_no_go_checklist=[]))
        self.assertTrue(any("go_no_go_checklist 为空" in e for e in errors))

    def test_empty_on_call(self):
        errors, _ = judge_launch_plan(valid_plan(on_call={}))
        self.assertTrue(any("on_call 为空" in e for e in errors))

    def test_empty_risks(self):
        errors, _ = judge_launch_plan(valid_plan(risks=[]))
        self.assertTrue(any("risks 为空" in e for e in errors))

    def test_tier1_without_extension_errors(self):
        # Tier1 时 tier1_extension 必填；None/空都判错
        errors, _ = judge_launch_plan(valid_plan(tier="1", tier1_extension=None))
        self.assertTrue(any("tier1_extension 为空" in e for e in errors))

    def test_too_many_workstreams_warning(self):
        ws = [
            {
                "workstream": f"线{i}",
                "owner": "张三",
                "deliverable": "x",
                "deadline": "T-7",
                "status": "进行中",
            }
            for i in range(TOO_MANY_WORKSTREAMS + 1)
        ]
        _, warnings = judge_launch_plan(valid_plan(workstreams=ws))
        self.assertTrue(any("场面过重" in w for w in warnings))

    def test_timeline_no_critical_path_warning(self):
        # 所有行 is_critical_path=false -> 告警
        timeline = [
            {"t_minus": "T-7", "milestone": "演练", "owner": "张三", "is_critical_path": False},
            {"t_minus": "T0", "milestone": "开闸", "owner": "王五", "is_critical_path": False},
        ]
        _, warnings = judge_launch_plan(valid_plan(timeline=timeline))
        self.assertTrue(any("无关键路径标注" in w for w in warnings))

    def test_rollback_trigger_no_digit_warning(self):
        # 回滚触发条件无数字 -> 告警（launch-plan 技能硬要求：数字不是心情）
        rb = valid_plan()["rollback"]
        rb["rollback_trigger"] = "感觉不对就回滚"
        _, warnings = judge_launch_plan(valid_plan(rollback=rb))
        self.assertTrue(any("未含数字" in w for w in warnings))

    def test_go_no_go_vague_word_warning(self):
        # go/no-go 项含"基本/差不多/大概"等模糊词 -> 告警
        checklist = [{"item": "文档基本好了", "owner": "赵六"}]
        _, warnings = judge_launch_plan(valid_plan(go_no_go_checklist=checklist))
        self.assertTrue(any("模糊词" in w for w in warnings))

    def test_rollback_trigger_with_digit_no_warning(self):
        # 回滚触发条件含数字不告警
        rb = valid_plan()["rollback"]
        rb["rollback_trigger"] = "错误率 > 2%"
        _, warnings = judge_launch_plan(valid_plan(rollback=rb))
        self.assertFalse(any("未含数字" in w for w in warnings))


# ────────────────────────── 2. 节点级行为 ──────────────────────────


class TestLaunchPlanNode(unittest.TestCase):
    def test_first_round_legal_calls_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(json_queue=[valid_plan()])
            deps = make_deps(Path(tmp), fake)
            out = make_launch_plan(deps)(node_state())

            self.assertEqual(len(fake.calls), 1)
            plan = out["launch_plan"]
            self.assertEqual(plan["shape_errors"], [])
            self.assertFalse(plan["self_fixed"])
            self.assertEqual(plan["tier"], "2")
            # [C 2026-09-11] 块2 补丁：launch_plan 节点返回时清零 launch_revision_feedback
            # （消费即清零，确认门条件边才不会把已消化的意见再次路由回 launch_plan；
            #   同构 issue_splitting 返回时清零 issue_revision_feedback 的模式）
            self.assertEqual(out["launch_revision_feedback"], "")
            # errors/warnings 同步进 state
            self.assertEqual(out["launch_plan_errors"], [])
            self.assertEqual(out["launch_plan_warnings"], [])

    def test_first_error_second_legal_self_fixes(self):
        # positioning="" 能过 schema（str 允许空串）但 judge 报错 -> 触发自检重调
        with tempfile.TemporaryDirectory() as tmp:
            bad = valid_plan(positioning="")
            good = valid_plan()
            fake = FakeLLM(json_queue=[bad, good])
            deps = make_deps(Path(tmp), fake)
            out = make_launch_plan(deps)(node_state())

            self.assertEqual(len(fake.calls), 2)
            # 第二次调用的 prompt 注入了中文自检反馈
            self.assertIn("发布计划自检未通过", fake.calls[1]["prompt"])
            self.assertIn("positioning 为空", fake.calls[1]["prompt"])
            plan = out["launch_plan"]
            self.assertEqual(plan["shape_errors"], [])
            self.assertTrue(plan["self_fixed"])

    def test_two_rounds_both_bad_keeps_errors_no_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad1 = valid_plan(positioning="")
            bad2 = valid_plan(positioning="")
            fake = FakeLLM(json_queue=[bad1, bad2])
            deps = make_deps(Path(tmp), fake)
            out = make_launch_plan(deps)(node_state())  # 不抛异常

            self.assertEqual(len(fake.calls), 2)
            plan = out["launch_plan"]
            self.assertTrue(plan["shape_errors"])  # 终判仍有错，保留交人工
            self.assertTrue(any("positioning 为空" in e for e in plan["shape_errors"]))
            self.assertTrue(plan["self_fixed"])

    def test_tier1_missing_extension_triggers_self_fix(self):
        # Tier1 + tier1_extension=None 能过 schema（Optional）但 judge 报错
        with tempfile.TemporaryDirectory() as tmp:
            bad = valid_plan(tier="1", tier1_extension=None)
            good = valid_plan(tier="1", tier1_extension=valid_tier1_extension())
            fake = FakeLLM(json_queue=[bad, good])
            deps = make_deps(Path(tmp), fake)
            out = make_launch_plan(deps)(node_state())

            self.assertEqual(len(fake.calls), 2)
            self.assertIn("tier1_extension 为空", fake.calls[1]["prompt"])
            self.assertEqual(out["launch_plan"]["shape_errors"], [])
            self.assertTrue(out["launch_plan"]["self_fixed"])


# ────────────────────────── 3. schema 校验 ──────────────────────────


class TestLaunchSchema(unittest.TestCase):
    def test_registry_loads_and_legal_payload_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("launch_plan")
            self.assertEqual(schema_cls.__name__, "LaunchPlanSchema")
            obj = schema_cls.model_validate(valid_plan())
            self.assertEqual(obj.tier, "2")
            self.assertIsNone(obj.tier1_extension)

    def test_invalid_tier_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("launch_plan")
            with self.assertRaises(Exception):
                schema_cls.model_validate(valid_plan(tier="4"))
            with self.assertRaises(Exception):
                schema_cls.model_validate(valid_plan(tier="大"))

    def test_risks_over_three_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("launch_plan")
            risks = [
                {"risk": f"风险{i}", "mitigation": "缓解", "early_warning": "信号"}
                for i in range(4)
            ]
            with self.assertRaises(Exception):
                schema_cls.model_validate(valid_plan(risks=risks))

    def test_tier1_extension_legal_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_cls = make_deps(Path(tmp)).registry.load_schema("launch_plan")
            obj = schema_cls.model_validate(
                valid_plan(tier="1", tier1_extension=valid_tier1_extension())
            )
            self.assertEqual(obj.tier, "1")
            self.assertIsNotNone(obj.tier1_extension)
            self.assertEqual(obj.tier1_extension.beachhead.segment, "10-50 人小团队 PM")


# ────────────────────────── 4. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_has_ten_nodes_with_launch_plan(self):
        # [C 2026-09-11] 块1 后图为 10 节点：issue_confirm 确认分支经 launch_plan 到 artifact_persist
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
                "issue_confirm",
                "launch_plan",  # [C 2026-09-11] 块1 新增发布计划节点（第 10 个）
                "artifact_persist",
            ]:
                self.assertIn(n, names)
            drawn = graph.get_graph().draw_mermaid()
            self.assertIn("launch_plan", drawn)
            self.assertIn("artifact_persist", drawn)
            self.assertIn("issue_confirm", drawn)


# ────────────────────────── 5. 模板渲染（三场景）──────────────────────────


def render_launch(deps, plan):
    return deps.artifacts.render(
        "launch_plan.md.j2",
        {"requirement_name": "demo-req", "generated_at": "2026-09-11", "plan": plan},
    )


class TestLaunchPlanTemplate(unittest.TestCase):
    def test_render_legal_tier2(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(valid_plan())
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_launch(deps, plan)
            self.assertIn("发布计划 — demo-req", md)
            self.assertIn("Tier 2（中等发布）", md)
            self.assertIn("定位一句话", md)
            self.assertIn("工作流矩阵", md)
            self.assertIn("倒排时间线", md)
            self.assertIn("灰度与回滚", md)
            self.assertIn("Go/No-Go", md)
            self.assertIn("Day1-7 值班", md)
            self.assertIn("Top", md)
            self.assertIn("错误率 > 2%", md)
            self.assertIn("【CP】是", md)  # 关键路径标注
            self.assertIn("prd.md", md)  # 来源标注
            # Tier2 不渲染 Tier1 扩展段（用段标题与滩头市场判断，避免误命中署名注释）
            self.assertNotIn("## Tier1 扩展", md)
            self.assertNotIn("滩头市场", md)
            self.assertNotIn("ICP（理想客户画像）", md)

    def test_render_tier1_with_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(valid_plan(tier="1", tier1_extension=valid_tier1_extension()))
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            md = render_launch(deps, plan)
            self.assertIn("Tier 1（大发布）", md)
            self.assertIn("Tier1 扩展", md)
            self.assertIn("滩头市场", md)
            self.assertIn("ICP", md)
            self.assertIn("分受众信息", md)
            self.assertIn("渠道排序", md)
            self.assertIn("10-50 人小团队 PM", md)

    def test_render_shape_errors_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(valid_plan())
            plan.update(
                {
                    "shape_errors": ["positioning 为空：定位一句话必须先于一切"],
                    "shape_warnings": ["timeline 无关键路径标注"],
                    "self_fixed": True,
                }
            )
            md = render_launch(deps, plan)
            self.assertIn("字段错误", md)
            self.assertIn("positioning 为空", md)
            self.assertIn("字段警告", md)
            self.assertIn("无关键路径标注", md)
            self.assertIn("已带反馈重生成", md)


# ────────────────────────── 6. 落盘 ──────────────────────────


def persist_state(launch_plan):
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
        "issue_plan": {
            "readiness": "pass",
            "issues": [{"id": "I1", "title": "x"}],
            "coverage": [],
        },
        "launch_plan": launch_plan,
    }


class TestArtifactPersistLaunchPlan(unittest.TestCase):
    def test_launch_plan_artifact_saved_to_chinese_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            plan = dict(valid_plan())
            plan.update({"shape_errors": [], "shape_warnings": [], "self_fixed": False})
            out = make_artifact_persist(deps)(persist_state(plan))

            self.assertIn("launch_plan", out["artifacts"])
            launch_file = Path(out["artifacts"]["launch_plan"])
            self.assertTrue(launch_file.exists())
            # 落盘到「发布计划」中文子目录
            self.assertIn("发布计划", str(launch_file))
            content = launch_file.read_text(encoding="utf-8")
            self.assertIn("发布计划 — demo-req", content)
            self.assertIn("错误率 > 2%", content)
            # 其他四份产物不受影响
            for key in ("prd", "insights", "review", "issues"):
                self.assertIn(key, out["artifacts"])

    def test_empty_launch_plan_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            out = make_artifact_persist(deps)(persist_state({}))
            self.assertNotIn("launch_plan", out["artifacts"])
            # 其他四份产物仍正常落盘
            for key in ("prd", "insights", "review", "issues"):
                self.assertIn(key, out["artifacts"])


# ────────────────────────── 7. prompt 条件块 ──────────────────────────


class TestLaunchPromptConditional(unittest.TestCase):
    def test_feedback_review_and_issues_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("launch_plan")
            rendered = Template(raw).render(
                requirement_name="demo-req",
                prd_markdown="# demo PRD",
                red_team_review={
                    "verdict": "pass_with_warning",
                    "blockers": [],
                    "warnings": [
                        {
                            "severity": "建议",
                            "location": "## 退款",
                            "issue": "退款口径模糊",
                            "suggestion": "补例子",
                        }
                    ],
                },
                issue_plan={
                    "readiness": "pass",
                    "summary": "2 张 AFK",
                    "issues": [
                        {"id": "I1", "title": "用户登录", "priority": "P0", "blocked_by": []},
                        {"id": "I2", "title": "用户退款", "priority": "P1", "blocked_by": ["I1"]},
                    ],
                },
                launch_revision_feedback="请修复 positioning 为空，并保持其他章节稳定",
            )
            self.assertIn("上一轮自检反馈", rendered)
            self.assertIn("positioning 为空", rendered)
            # 评审遗留 warnings 段也渲染
            self.assertIn("退款口径模糊", rendered)
            # 工单清单渲染
            self.assertIn("用户登录", rendered)
            self.assertIn("用户退款", rendered)
            self.assertIn("I1", rendered)

    def test_feedback_absent_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("launch_plan")
            rendered = Template(raw).render(
                requirement_name="demo-req",
                prd_markdown="# demo PRD",
                red_team_review={"verdict": "pass", "blockers": [], "warnings": []},
                issue_plan={"readiness": "pass", "issues": []},
                launch_revision_feedback="",
            )
            self.assertNotIn("上一轮自检反馈", rendered)
            # 关键引导仍在
            self.assertIn("分层", rendered)
            self.assertIn("定位一句话", rendered)
            self.assertIn("go/no-go", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# [C 2026-09-11] tests/test_launch_plan.py 块1 新增完成（28 条假 LLM 自测）
