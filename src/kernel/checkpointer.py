# 内核骨架 - SQLite 断点持久化
"""LangGraph SQLite Checkpointer 封装。"""
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


def get_checkpointer(db_path: str = "./ai_pm_agent.db") -> SqliteSaver:
    """返回一个 SqliteSaver 实例（连接保持打开，供 graph 生命周期内复用）。

    注：SqliteSaver.from_conn_string() 是上下文管理器，退出即关闭连接，
    无法直接传给 compile()。因此这里手动打开持久连接再构造 SqliteSaver。

    Args:
        db_path: SQLite 数据库文件路径，默认为项目根目录下 ai_pm_agent.db

    Returns:
        SqliteSaver: 可直接传给 StateGraph.compile(checkpointer=...) 的检查点器
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return SqliteSaver(conn)


