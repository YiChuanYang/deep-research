"""可验证设备选择的向量客户端；模型会话按进程复用。"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from openai import OpenAI

from deep_researcher.config import Configuration
from deep_researcher.rag.store import digest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalEmbeddingRuntime:
    """保存 FastEmbed 模型以及 ONNX 会话实际启用的执行器。"""

    engine: Any
    device: str
    providers: tuple[str, ...]


@lru_cache(maxsize=2)
def local_embedding(
    model: str, cache_dir: str, device: Literal["auto", "cpu", "cuda"], threads: int,
) -> LocalEmbeddingRuntime:
    """auto 可回退；显式 cuda 失败时抛错，不把 CPU 执行冒充 GPU。"""
    import onnxruntime as ort
    from fastembed import TextEmbedding

    started = perf_counter()
    available = ort.get_available_providers()
    use_cuda = device != "cpu" and "CUDAExecutionProvider" in available
    if device == "cuda" and not use_cuda:
        raise RuntimeError("当前 ONNX Runtime 没有 CUDA 执行器，请同步 GPU 依赖")

    def create(cuda: bool) -> LocalEmbeddingRuntime:
        if cuda:
            # 在当前 Python 环境的 nvidia 包中加载 CUDA/cuDNN DLL，无需修改系统 PATH。
            ort.preload_dlls(directory="")
        providers: list[Any] = (
            [("CUDAExecutionProvider", {"device_id": 0, "arena_extend_strategy": "kSameAsRequested"}),
             "CPUExecutionProvider"] if cuda else ["CPUExecutionProvider"]
        )
        engine = TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads, providers=providers)
        # FastEmbed 0.9 的两层 model 分别是编码器和底层 ONNX InferenceSession。
        session = getattr(engine.model, "model", None)
        if session is None:
            raise RuntimeError("无法验证向量模型的 ONNX 会话")
        actual = tuple(session.get_providers())
        if cuda and "CUDAExecutionProvider" not in actual:
            raise RuntimeError("CUDA 会话初始化失败，运行时实际回退到了 CPU")
        return LocalEmbeddingRuntime(engine, "cuda" if cuda else "cpu", actual)

    try:
        runtime = create(use_cuda)
    except Exception:
        if device != "auto" or not use_cuda:
            raise
        logger.warning("GPU 向量会话不可用 · 本次改用 CPU", exc_info=True,
                       extra={"event": "web_embedding_device_fallback"})
        runtime = create(False)
    if device == "auto" and not use_cuda:
        logger.info("当前运行时没有 CUDA 执行器 · 向量编码使用 CPU")
    logger.info("向量模型就绪 · device=%s · threads=%d · 加载=%.2fs",
                runtime.device, threads, perf_counter() - started,
                extra={"event": "web_embedding_model_loaded", "device": runtime.device,
                       "providers": list(runtime.providers), "model": model,
                       "duration_s": perf_counter() - started})
    return runtime


class EmbeddingClient:
    def __init__(self, config: Configuration) -> None:
        self.config = config
        self.client: OpenAI | None = None
        self.device = "api"
        if config.embedding_backend == "local":
            self.signature = digest(f"local|{config.embedding_model}|{config.embedding_dimensions}")
            runtime = local_embedding(config.embedding_model, str(Path(config.embedding_cache_dir).resolve()),
                                      config.embedding_device, config.embedding_threads)
            self.local = runtime.engine
            self.device = runtime.device
            self.providers = runtime.providers
            return
        endpoint = config.embedding_base_url or config.llm_base_url
        key = config.embedding_api_key
        if key is None and (not config.embedding_base_url or endpoint == config.llm_base_url):
            key = config.llm_api_key
        if not endpoint or key is None:
            raise ValueError("请配置 Embedding 服务地址和密钥")
        self.signature = digest(f"{endpoint.rstrip('/')}|{config.embedding_model}|{config.embedding_dimensions}")
        self.client = OpenAI(base_url=endpoint, api_key=key.get_secret_value(),
                             timeout=min(config.llm_timeout, config.web_rag_timeout), max_retries=0)

    def embed(self, texts: list[str], timeout: float) -> list[list[float]]:
        if not texts:
            return []
        if timeout <= 0:
            raise TimeoutError("向量编码没有剩余时间预算")
        started = perf_counter()
        tokens: int | None = None
        if self.client is None:
            # 本地 ONNX 调用是同步计算，timeout 只能由调用层在批次之间检查。
            vectors = [row.tolist() for row in self.local.embed(texts, batch_size=self.config.embedding_batch_size)]
        else:
            response = self.client.embeddings.create(
                model=self.config.embedding_model, input=texts,
                dimensions=self.config.embedding_dimensions, encoding_format="float", timeout=timeout,
            )
            ordered = sorted(response.data, key=lambda row: row.index)
            if [row.index for row in ordered] != list(range(len(texts))):
                raise ValueError("Embedding 返回的条数或编号不完整")
            vectors = [row.embedding for row in ordered]
            tokens = getattr(response.usage, "total_tokens", None)
        if len(vectors) != len(texts) or any(
            len(v) != self.config.embedding_dimensions or not all(math.isfinite(x) for x in v) or not any(v)
            for v in vectors
        ):
            raise ValueError("Embedding 返回无效向量或维度与配置不一致")
        logger.debug("向量编码完成 · device=%s · 文本=%d · 耗时=%.3fs",
                     self.device, len(texts), perf_counter() - started,
                     extra={"event": "embedding_completed", "text_count": len(texts), "tokens": tokens,
                            "device": self.device, "duration_s": perf_counter() - started})
        return vectors

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
