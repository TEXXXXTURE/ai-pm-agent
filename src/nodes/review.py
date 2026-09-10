# [C 2026-09-10] PRD 评审门节点（prd_review）
"""PRD 评审门：模型按五维 rubric 评审 PRD，**三档结论由 Python 代码硬性判定**。

图位置：prd_generation -> prd_review ->（条件边）-> prd_generation（打回重写）
                                            └─> artifact_persist（通过/带警告通过/强制放行）

硬判规则（项目硬约束，模型不决定走向，不加 blocker 一票否决）：
- avg < 3.5                       -> "reject"（打回重写）
- avg >= 3.5 且任一维 <= 2        -> "pass_with_warning"（带警告通过）
- avg >= 3.5 且全部维 >= 3        -> "pass"（通过）

轮次（state.prd_revision_count 初值 0）：
- reject 时计数 +1；计数达到 3 仍 reject -> 强制改判 pass_with_warning（forced=True），
  报告显著标注"已达 3 轮修订上限，强制放行待人工裁决"，并照常落盘；
- 非 reject 计数不变。
"""
from __future__ import annotations

from kernel.spec import NodeSpec

# 最大修订轮数（计数达到该值后仍不达标的评审强制放行） [C 2026-09-10]
MAX_REVISIONS = 3

# 通过线：五维均分阈值
PASS_AVG = 3.5
# 带警告线：任一维不高于该分时即使均分达标也只能带警告通过
WARN_DIM_MAX = 2
# 反馈文本中列入"评分短板"的维度分数线（低于该分的维度逐条点名）
WEAK_DIM_MAX = 3


def judge_scores(scores: list[dict]) -> tuple[float, int, str]:
    """纯函数：按五维分数硬判三档结论（不看 blockers、不看模型意见）。

    Args:
        scores: 每条至少含 {"score": int 1-5}，由 PrdReviewSchema 保证恰好 5 条。

    Returns:
        (avg, minimum, verdict)：均分（保留 1 位小数）、最低分、结论
        （"reject" / "pass_with_warning" / "pass"）。

    Note:
        verdict 用未四舍五入的原始均分与阈值比较，避免边界值被舍入影响；
        返回的 avg 仅用于展示/落盘。 [C 2026-09-10]
    """
    # 生产路径分数由 PrdReviewSchema 保证为 1-5 的 int；此处不做 int() 强转，
    # 以便单测可直接用浮点构造均分边界（如恰好 3.5）。 [C 2026-09-10]
    nums = [item["score"] for item in scores]
    if not nums:
        raise ValueError("scores 为空，无法判定评审结论")
    avg_raw = sum(nums) / len(nums)
    minimum = min(nums)
    avg = round(avg_raw, 1)
    if avg_raw < PASS_AVG:
        verdict = "reject"
    elif minimum <= WARN_DIM_MAX:
        verdict = "pass_with_warning"
    else:
        verdict = "pass"
    return avg, minimum, verdict
    # [C 2026-09-10] 三档硬判规则落点，纯函数便于单测


def build_revision_feedback(review: dict, round_no: int) -> str:
    """纯函数：把模型评审内容拼成中文可行动的打回意见，供 prd_generation prompt 注入。

    内容 = 轮次说明 + blockers 逐条清单（严重程度/位置/问题/改法方向）
           + 评分短板维度（score <= 3 的维度逐条点名，覆盖"纯分数打回、blockers 为空"的情况）。
    复审约定：复审只复验这些项（修订引入的新问题除外）。 [C 2026-09-10]
    """
    lines: list[str] = [
        f"【第 {round_no} 轮修订要求】以下是独立评审门打回的问题，"
        "请逐条针对性解决后输出完整修订版 PRD；复审只复验这些项（修订引入的新问题除外）。",
    ]

    blockers = review.get("blockers") or []
    if blockers:
        lines.append("")
        lines.append(f"一、必须修复的问题（{len(blockers)} 条）：")
        for idx, item in enumerate(blockers, start=1):
            lines.append(
                f"{idx}. [{item.get('severity', '?')}] 位置：{item.get('location', '未标注')}\n"
                f"   问题：{item.get('issue', '')}\n"
                f"   改法方向：{item.get('suggestion', '')}"
            )

    # 分数短板：均分打回时 blockers 可能为空，需要把低分维度显式喂给重写节点
    weak = [
        item
        for item in (review.get("scores") or [])
        if int(item.get("score", 0)) <= WEAK_DIM_MAX
    ]
    if weak:
        lines.append("")
        lines.append(f"二、评分短板维度（{len(weak)} 个，需重点加强）：")
        for idx, item in enumerate(weak, start=1):
            lines.append(
                f"{idx}. {item.get('dimension', '未命名维度')}（{item.get('score')} 分）："
                f"{item.get('rationale', '')}"
            )

    warnings = review.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append(f"三、建议一并处理的警告（{len(warnings)} 条，不强制）：")
        for idx, item in enumerate(warnings, start=1):
            lines.append(f"{idx}. {item.get('issue', '')}（改法方向：{item.get('suggestion', '')}）")

    return "\n".join(lines).strip()


def make_prd_review(deps):
    """PRD 评审门节点工厂：返回签名 (state: dict) -> dict 的节点函数。"""

    def prd_review(state: dict) -> dict:
        # 1. 模型只产出评审事实（五维评分/findings/红队假设），走 JSON 结构化通道
        prompt = deps.registry.read_prompt("prd_review")
        schema = deps.registry.load_schema("prd_review")
        spec = NodeSpec(
            name="prd_review",
            prompt_template=prompt,
            output_schema=schema,
        )
        result = deps.runner.run_raw(spec, state)

        # 2. 代码硬判三档结论（模型不决定走向） [C 2026-09-10]
        avg, minimum, verdict = judge_scores(result["scores"])

        # 3. 轮次计数 + 3 轮上限强制放行
        count = int(state.get("prd_revision_count") or 0)
        forced = False
        new_count = count
        revision_feedback = ""
        if verdict == "reject":
            new_count = count + 1
            if new_count >= MAX_REVISIONS:
                # 第 3 轮仍不达标：强制带警告通过，交人工裁决，不再打回
                verdict = "pass_with_warning"
                forced = True
            else:
                # 真正打回：拼可行动反馈文本，供下一轮 prd_generation prompt 注入
                revision_feedback = build_revision_feedback(result, new_count)

        # 4. 完整评审 dict 写回 state（模型原始内容 + 代码判定字段）
        review = {
            **result,
            "avg": avg,
            "minimum": minimum,
            "verdict": verdict,
            "forced": forced,
            "round": new_count,
            "revision_feedback": revision_feedback,
        }
        return {
            "red_team_review": review,
            "prd_revision_count": new_count,
        }

    return prd_review


def route_after_review(state: dict) -> str:
    """条件边路由：reject 回 prd_generation 重写；其余去 issue_splitting 拆研发工单。"""
    review = state.get("red_team_review") or {}
    if review.get("verdict") == "reject":
        return "prd_generation"
    return "issue_splitting"
    # [C 2026-09-10] 评审门条件边路由函数
    # [C 2026-09-11] 块1：通过分支由 artifact_persist 改走 issue_splitting（拆单后再落盘）


# [C 2026-09-10] nodes/review.py 新增完成
