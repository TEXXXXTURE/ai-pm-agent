# 组件框架 - PRD 生成 schema
# 纵切联调 - 改为 sections: list[PrdSection] 片段结构
# 创意放松 - 删除 body_html 非空 / 章节数硬校验，schema 只保证结构形状
"""PRDOutputSchema：PRD 生成节点的输出结构（章节片段列表，HTML 外壳由模板提供）。

M7.5 起不再做机器硬校验：章节数量、章节标题、内容深浅均由模型按需求
自主决定（简单需求不硬凑章节），质量由 prompt 中的"资深 PM 质量标杆"
引导模型自检。schema 只保证输出的 JSON 结构形状（sections 列表，
每项含 title / body_html 两个字符串字段），供产物系统 artifact.py 渲染。
"""
from __future__ import annotations

from pydantic import BaseModel


class PrdSection(BaseModel):
    """PRD 单个章节。

    title: 章节标题（由模型按需求自主拟定）
    body_html: 章节正文 HTML 片段（用 <p>/<ul>/<h3>/<table> 等片段标签；
        内容是否为空、写多深由模型按需求自主决定，schema 不做非空校验）
    """

    title: str
    body_html: str


class PRDOutputSchema(BaseModel):
    """PRD 生成结果。

    sections: 章节片段列表。章节数量与内容由模型按需求自主决定
    （可为空列表、可只有 1 章，简单需求不硬凑），schema 不做数量校验。
    """

    sections: list[PrdSection]


# PRD schema 放松完成：仅保留结构形状，去除硬校验
