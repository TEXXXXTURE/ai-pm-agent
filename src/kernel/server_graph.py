# 内核骨架 - LangGraph API Server 图工厂
"""供 langgraph dev（本地 Agent Server / LangSmith Studio）加载的图工厂。

langgraph.json 的 graphs 指向本模块的 make_graph。langgraph dev 会把项目根和
langgraph.json 里的 dependencies 目录（我们填 src）直接 sys.path.append，
所以这里能用 `from kernel... import ...`。

与 CLI 的唯一区别是持久化归属：
- CLI 自带 SqliteSaver，断点落本地 ai_pm_agent.db，配 --resume 续跑；
- Server 自己管持久化，图不能自带 BaseCheckpointSaver，否则 local_dev 阶段
  直接抛 ValueError（langgraph_api/graph.py:815-836）。
  代价是 Studio 里的线程与 CLI 的 ai_pm_agent.db 互不相通——两边各是一套断点。

不设 LANGSMITH_TRACING：只用 Studio 的图/线程/时间旅行，trace 不上传云端。
"""
from __future__ import annotations

from typing import Any


def make_graph() -> Any:
    """装配依赖并编译一张不带 checkpointer 的图。零参，langgraph dev 的约定。"""
    from kernel.deps import build_deps
    from kernel.graph import build_graph

    deps, _db_path = build_deps()
    return build_graph(deps, with_checkpointer=False)
