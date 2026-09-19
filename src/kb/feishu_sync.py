# 知识库接入 - 飞书同步占位
"""FeishuSync：飞书知识库双向同步（占位实现）。

飞书授权未配置前，拉取/推送均抛 NotImplementedError；
待 config.yaml 中 feishu_app_id / feishu_app_secret 配置后再实现真实同步。
"""
from __future__ import annotations

_NOT_CONFIGURED_MSG = (
    "飞书授权未配置：请在 config.yaml 中配置 feishu_app_id 与 feishu_app_secret"
)


class FeishuSync:
    """飞书同步客户端（占位）。"""

    def __init__(self, app_id: str = "", app_secret: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret

    def fetch_from_feishu(self):
        """从飞书知识库拉取档案（未实现）。"""
        raise NotImplementedError(_NOT_CONFIGURED_MSG)

    def push_to_feishu(self, archive: dict):
        """向飞书知识库推送档案（未实现）。"""
        raise NotImplementedError(_NOT_CONFIGURED_MSG)


