# 产物系统 - HTML 产物渲染与按需求名分文件夹落盘
# 产物改 Markdown 原生 - save 增加 ext 参数支持 .md 落盘；
#     render() 与 HTML 相关逻辑原样保留（prd.html.j2 / insights.html.j2 + assets 退役为演示导出器）。
"""ArtifactManager：Jinja2 模板渲染 + 按需求名分文件夹落盘（T1 起主产物为 Markdown）。

- 模板位于 artifacts/templates/，静态资源（style.css / editor.js）位于 artifacts/assets/
- render() 渲染模板：HTML 模板渲染时将 css/js 全文内联（自包含、可编辑/打印/复制）；
  T1 新增 Markdown 模板（insights.md.j2），不依赖 assets
- save() 落盘结构：<output_root>/<需求名>/<中文子目录>/<需求名>-<doc_type><ext>
  ext 默认 ".html"；T1 起 PRD/洞察主产物传 ext=".md"
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Union

from jinja2 import Environment, FileSystemLoader

# doc_type 英文标识 → 中文子目录名（未知 doc_type 直接用其本身作为目录名）
_DOC_DIR_MAP = {
    "prd": "需求文档",
    "insights": "需求洞察",
    # 研发工单清单落「研发工单」子目录；review 目录维持英文原样不动
    "issues": "研发工单",
    # 发布计划落「发布计划」子目录
    "launch_plan": "发布计划",
    # 第 8 段评测产物统一落「评测」子目录
    "eval_config": "评测",
    "eval_results": "评测",
    "eval_report": "评测",
    # 第 6 段对比选型产物统一落「评测」子目录
    "bakeoff_config": "评测",
    "bakeoff_results": "评测",
    "bakeoff_report": "评测",
}

# 第 6 段逐模型产物 doc_type 带 provider 后缀
# （bakeoff-<safe_id>-eval_config / bakeoff-<safe_id>-results），前缀命中即归「评测」子目录
_BAKEOFF_DOC_PREFIXES = ("bakeoff-", "bakeoff_")

# 文件系统非法字符（Windows 全量，跨平台保守处理）
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_name(name: str) -> str:
    """将需求名中的文件/目录非法字符替换为下划线。"""
    return _ILLEGAL_CHARS.sub("_", str(name)).strip()


# 成本列渲染过滤：Python 对小于 1e-4 的浮点用科学计数法（1.2e-05），
#     直接塞进模板不美观。按数量级选小数位、去掉末尾多余的零、至少保留 2 位小数。
def format_cost(value: object) -> str:
    """把成本数值渲染成固定小数文本，避免浮点科学计数法（如 1.2e-05）。

    Args:
        value: 成本数值（float/int，或 None/字符串等非数值）。

    Returns:
        固定小数文本（如 "0.000012" "0.015" "12.34"）；非数值原样转字符串返回。
    """
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if num == 0:
        return "0.00"
    digits = 4 if abs(num) >= 1 else (6 if abs(num) >= 0.01 else 8)
    int_part, _, frac = f"{num:.{digits}f}".partition(".")
    return f"{int_part}.{frac.rstrip('0').ljust(2, '0')}"


class ArtifactManager:
    """渲染模板并保存产物（T1 起主产物为 Markdown .md；HTML 通道保留为演示导出器）。"""

    def __init__(
        self,
        output_root: str = "./output",
        template_dir: str = "./artifacts/templates",
        assets_dir: str = "./artifacts/assets",
    ) -> None:
        self.output_root = Path(output_root)
        self.template_dir = Path(template_dir)
        self.assets_dir = Path(assets_dir)
        # autoescape=False：style_css / editor_js / body_html 均需原样注入
        self._env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        # 注册成本格式化过滤，模板里写 {{ x|cost }} 即可
        self._env.filters["cost"] = format_cost

    def _load_asset(self, name: str) -> str:
        """读取 assets 目录下静态资源（css/js）的全文内容。"""
        return (self.assets_dir / name).read_text(encoding="utf-8")

    def render(self, template_name: str, context: dict) -> str:
        """渲染 Jinja2 模板，自动注入 style_css / editor_js 全文。

        Args:
            template_name: 模板文件名，如 "prd.html.j2"
            context: 模板上下文（requirement_name / generated_at / sections 等）

        Returns:
            渲染后的完整 HTML 字符串（自包含，可直接落盘）
        """
        ctx = dict(context)
        ctx.setdefault("style_css", self._load_asset("style.css"))
        ctx.setdefault("editor_js", self._load_asset("editor.js"))
        template = self._env.get_template(template_name)
        return template.render(**ctx)

    def save(
        self,
        content: str,
        requirement_name: str,
        doc_type: str,
        ext: str = ".html",
    ) -> Path:
        """按 需求名/中文子目录/需求名-doc_type<ext> 结构落盘。

        例：output/ai-cs/需求文档/ai-cs-prd.md   （T1：Markdown 主产物，ext=".md"）
            output/ai-cs/需求洞察/ai-cs-insights.md
            output/ai-cs/需求文档/ai-cs-prd.html （HTML 演示导出器，ext 默认 ".html"）

        Args:
            content: 待写入的文件全文（Markdown 或 HTML）
            requirement_name: 需求名（用作文件夹与文件名，非法字符会被替换）
            doc_type: 产物类型标识（prd / insights / 其他）
            ext: 文件扩展名（含点），默认 ".html"；Markdown 产物传 ".md"

        Returns:
            写入文件的 Path
        """
        safe_name = sanitize_name(requirement_name)
        sub_dir = _DOC_DIR_MAP.get(doc_type)
        if sub_dir is None:
            # 逐模型 bakeoff 产物（doc_type 带 provider 后缀）
            # 前缀命中统一归「评测」；其余未知 doc_type 保持原行为（直接用其本身作目录名）
            if str(doc_type).startswith(_BAKEOFF_DOC_PREFIXES):
                sub_dir = "评测"
            else:
                sub_dir = doc_type
        out_dir = self.output_root / safe_name / sub_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{safe_name}-{doc_type}{ext}"
        out_path.write_text(content, encoding="utf-8")
        return out_path
    def load(self, path: Union[str, Path]) -> str:
        """读回已保存的产物文件内容。"""
        return Path(path).read_text(encoding="utf-8")


# save 支持 .md 落盘；render/HTML 逻辑原样保留
