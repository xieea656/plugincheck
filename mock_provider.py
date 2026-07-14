"""Mock LLM + Embedding Provider — 零 token 消耗，零网络请求。

所有 LLM 调用返回固定的合法 JSON，所有 embedding 调用返回确定性向量。
用于安全地进行大规模压力测试。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import numpy as np

# ---- 固定 Mock 响应 ----

MOCK_SUMMARY_JSON = json.dumps({
    "summary": "群聊中讨论了 Python 异步编程和 AstrBot 插件开发。用户分享了 asyncio 的使用经验，"
              "并询问了关于消息处理的性能优化建议。",
    "entities": [
        {"name": "Python", "type": "technology"},
        {"name": "asyncio", "type": "library"},
        {"name": "AstrBot", "type": "framework"},
    ],
    "knowledge_type": "narrative",
    "importance": 7,
}, ensure_ascii=False)

MOCK_PROFILE_JSON = json.dumps({
    "profile_text": "该用户是一名对操作系统和容器技术有深入理解的开发者。"
                    "偏好使用 Arch Linux 作为开发环境，倾向简洁高效的解决方案。"
                    "对 Docker 和 Podman 有实际使用经验。",
    "traits": ["技术导向", "偏好简洁", "动手能力强"],
    "confidence": 0.78,
}, ensure_ascii=False)

MOCK_STRUCTURED_JSON = json.dumps({
    "facts": [
        {"subject": "用户", "predicate": "使用", "object": "Arch Linux"},
        {"subject": "用户", "predicate": "偏好", "object": "Podman over Docker"},
    ],
    "knowledge_type": "structured",
}, ensure_ascii=False)


class MockLLMClient:
    """Mock LLM — 根据 prompt 内容返回合理的假响应。"""

    def __init__(self, **kwargs):
        self.complete_call_count = 0

    async def complete(self, prompt: str, *, temperature: float = 0.2, max_tokens: int = 1200) -> str:
        self.complete_call_count += 1
        p = str(prompt or "").lower()

        if "profile" in p or "画像" in p or "personality" in p or "特征" in p:
            return MOCK_PROFILE_JSON
        if "structured" in p or "structured" in p or "facts" in p:
            return MOCK_STRUCTURED_JSON
        if "entity" in p or "实体" in p:
            return json.dumps({"entities": [
                {"name": "MockEntity", "type": "test"}
            ]}, ensure_ascii=False)
        return MOCK_SUMMARY_JSON

    async def complete_json(self, prompt: str, **kwargs):
        text = await self.complete(prompt, **kwargs)
        return True, json.loads(text), text


class MockEmbeddingAdapter:
    """Mock Embedding — 返回确定性向量（不会真的调用 API）。"""

    def __init__(self, dimension: int = 1024, **kwargs):
        self.dimension = int(dimension)
        self.encode_count = 0

    async def _detect_dimension(self) -> int:
        return self.dimension

    async def encode(self, texts, **kwargs) -> np.ndarray:
        """每次编码返回确定性向量（基于文本 hash）。"""
        if isinstance(texts, str):
            vec = self._hash_vec(texts)
            self.encode_count += 1
            return vec

        vectors = []
        for t in texts:
            vectors.append(self._hash_vec(str(t)))
            self.encode_count += 1
        if not vectors:
            return np.zeros((0, self.dimension), dtype=np.float32)
        return np.vstack(vectors).astype(np.float32)

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def _hash_vec(self, text: str) -> np.ndarray:
        """生成确定性单位向量（永不相同，永不相近）。"""
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(self.dimension, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)

    async def encode_batch(self, texts, **kwargs) -> np.ndarray:
        return await self.encode(texts, **kwargs)


class MockLLMGenerate:
    """Mock context.llm_generate — 可被注入到目标插件的 context 中。"""

    def __init__(self, **kwargs):
        self.call_count = 0

    async def __call__(self, **kwargs) -> Any:
        self.call_count += 1
        prompt = str(kwargs.get("prompt", "") or "").lower()

        class FakeResp:
            completion_text = MOCK_SUMMARY_JSON
        return FakeResp()


class MockProvider:
    """聚合所有 Mock 组件。"""

    def __init__(self, embedding_dim: int = 1024):
        self.llm = MockLLMClient()
        self.embedding = MockEmbeddingAdapter(dimension=embedding_dim)
        self.llm_generate = MockLLMGenerate()

    @property
    def llm_calls(self) -> int:
        return self.llm.complete_call_count

    @property
    def embedding_calls(self) -> int:
        return self.embedding.encode_count
