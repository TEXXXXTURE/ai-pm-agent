# RAG 子库 - 嵌入模型封装（硅基流动 OpenAI 兼容 Embeddings API）
"""EmbeddingModel：把文本批量转成向量，供领域知识库建库与检索使用。

- 配置来自 config.yaml 的 domain_kb.embedding 段：provider / model / api_key_env /
  api_base / dimensions / batch_size（batch_size 缺省 32）。
- 密钥从 api_key_env 指定的环境变量读取，不落盘、不硬编码；缺失或为空时抛 RuntimeError。
- 请求走 httpx（超时 60 秒），OpenAI 兼容协议：POST {api_base}/embeddings，
  请求体 {"model": ..., "input": [...]}，请求头 Authorization: Bearer <key>。
- 按 batch_size 分批；单批失败（网络异常或非 2xx）重试 2 次，指数退避 0.5s / 1.0s，
  仍失败抛 RuntimeError（带失败批号）。
- 响应 data 数组按 index 字段排序后取 embedding，保证返回顺序与输入一致。
"""
from __future__ import annotations

import os
import time

import httpx

# 单批缺省条数（config 未给时使用）
DEFAULT_BATCH_SIZE = 32
# 单次请求超时（秒）
REQUEST_TIMEOUT = 60.0
# 失败后的额外重试次数（共尝试 MAX_RETRIES + 1 次）
MAX_RETRIES = 2
# 各次重试前的退避时长（秒）：指数退避 0.5s / 1.0s
BACKOFF_SECONDS = (0.5, 1.0)


def _extract_embeddings(payload: dict) -> list[list[float]]:
    """从 API 响应体取向量：data 按 index 排序后依次返回 embedding。"""
    data = payload.get("data") or []
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    return [item.get("embedding", []) for item in ordered]


class EmbeddingModel:
    """嵌入模型封装（批量嵌入 / 单条查询嵌入）。

    Args:
        config: config.yaml 中 domain_kb.embedding 段的 dict。
    """

    def __init__(self, config: dict):
        config = dict(config or {})
        self.provider = str(config.get("provider", ""))
        self.model = str(config.get("model", ""))
        self.api_key_env = str(config.get("api_key_env", ""))
        self.api_base = str(config.get("api_base", "")).rstrip("/")
        self.dimensions = int(config.get("dimensions") or 0)
        batch_size = int(config.get("batch_size") or DEFAULT_BATCH_SIZE)
        self.batch_size = batch_size if batch_size > 0 else DEFAULT_BATCH_SIZE

    def _resolve_api_key(self) -> str:
        """从 api_key_env 指定的环境变量取密钥；缺失或为空时抛 RuntimeError。"""
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if not key.strip():
            env_name = self.api_key_env or "（未配置 api_key_env）"
            raise RuntimeError(
                f"嵌入模型密钥缺失：环境变量 {env_name} 未设置或为空，"
                "请在项目 .env 中配置该变量后重试"
            )
        return key

    def _embed_batch(
        self,
        batch: list[str],
        api_key: str,
        batch_no: int,
        total_batches: int,
    ) -> list[list[float]]:
        """嵌入一批文本；失败重试 2 次（0.5s / 1.0s 退避），仍失败抛 RuntimeError。"""
        url = f"{self.api_base}/embeddings"
        payload = {"model": self.model, "input": list(batch)}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_reason = ""
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = httpx.post(
                    url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
                )
                if 200 <= response.status_code < 300:
                    return _extract_embeddings(response.json())
                last_reason = f"HTTP {response.status_code}"
            except Exception as exc:  # 网络异常（超时、连接失败等）
                last_reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS[attempt])

        raise RuntimeError(
            f"嵌入请求失败：第 {batch_no}/{total_batches} 批（{len(batch)} 条）"
            f"已重试 {MAX_RETRIES} 次仍失败，原因：{last_reason}"
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入，返回与输入顺序一致的向量列表。"""
        texts = list(texts or [])
        if not texts:
            return []
        api_key = self._resolve_api_key()

        batches = [
            texts[i : i + self.batch_size]
            for i in range(0, len(texts), self.batch_size)
        ]
        vectors: list[list[float]] = []
        for batch_no, batch in enumerate(batches, start=1):
            vectors.extend(
                self._embed_batch(batch, api_key, batch_no, len(batches))
            )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """嵌入单条查询，返回单条向量。"""
        vectors = self.embed([text])
        return vectors[0] if vectors else []


