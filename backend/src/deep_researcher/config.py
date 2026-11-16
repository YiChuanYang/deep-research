"""统一加载、校验和保存应用配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


# 固定读取 backend/.env，避免工作目录改变后找错文件。
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# 只有这些非敏感字段可以进入研究快照。
SNAPSHOT_FIELDS = {
    "llm_provider",
    "llm_model_id",
    "llm_base_url",
    "llm_timeout",
    "llm_max_retries",
    "llm_temperature",
    "llm_max_tokens",
    "search_api",
    "search_timeout",
    "search_max_results",
    "enable_query_rewrite",
    "enable_report_reflection",
    "report_max_revisions",
    "report_max_supplement_tasks",
    "report_review_max_chars",
    "fetch_full_page",
    "max_tokens_per_source",
    "enable_notes",
    "notes_workspace",
    "strip_thinking_tokens",
    "evidence_max_chars",
    "enable_web_rag", "web_rag_workspace", "web_rag_max_pages", "web_rag_page_chars",
    "web_rag_chunk_chars", "web_rag_overlap_chars", "web_rag_max_chunks",
    "web_rag_candidates", "web_rag_top_k", "web_rag_context_chars",
    "web_rag_per_source", "web_rag_timeout", "web_rag_cache_hours",
    "embedding_model", "embedding_base_url", "embedding_dimensions",
    "enable_embeddings", "enable_rerank", "review_output_mode",
    "embedding_backend", "embedding_cache_dir",
}


class SearchAPI(str, Enum):
    """当前实际支持的搜索后端。"""

    TAVILY = "tavily"
    DUCKDUCKGO = "duckduckgo"


class Configuration(BaseModel):
    """一次运行使用的配置。"""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    # 模型配置。
    llm_provider: str = Field(default="zhipu", min_length=1)
    llm_model_id: str | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = Field(default=None, repr=False)

    llm_timeout: float = Field(default=120, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_temperature: float | None = Field(default=0.0, ge=0, le=2)
    llm_max_tokens: int = Field(default=4080, gt=0)

    # 搜索配置。
    search_api: SearchAPI = SearchAPI.TAVILY
    search_timeout: int = Field(default=30, gt=0, le=120)
    search_max_results: int = Field(default=5, ge=1, le=20)
    # 仅控制搜索前的查询改写，搜索次数和结果数仍由原逻辑决定。
    enable_query_rewrite: bool = True
    # revision=0 是初稿；两次额外改进最多生成三版报告。
    enable_report_reflection: bool = True
    report_max_revisions: int = Field(default=2, ge=0, le=3)
    report_max_supplement_tasks: int = Field(default=2, ge=0, le=3)
    report_review_max_chars: int = Field(default=60000, ge=1000, le=200000)
    tavily_api_key: SecretStr | None = Field(default=None, repr=False)

    fetch_full_page: bool = False
    max_tokens_per_source: int = Field(default=2000, gt=0)

    # 新研究使用完整网页检索；旧快照在 for_resume 中显式回到原流程。
    enable_web_rag: bool = True
    web_rag_workspace: str = "./data/web_rag"
    web_rag_max_pages: int = Field(default=5, ge=1, le=10)
    web_rag_page_chars: int = Field(default=60000, ge=1000, le=200000)
    web_rag_chunk_chars: int = Field(default=450, ge=200, le=2000)
    web_rag_overlap_chars: int = Field(default=60, ge=0, le=300)
    web_rag_max_chunks: int = Field(default=160, ge=5, le=1000)
    web_rag_candidates: int = Field(default=16, ge=4, le=40)
    web_rag_top_k: int = Field(default=8, ge=1, le=20)
    web_rag_context_chars: int = Field(default=9000, ge=1000, le=30000)
    web_rag_per_source: int = Field(default=3, ge=1, le=10)
    web_rag_timeout: int = Field(default=90, ge=10, le=300)
    web_rag_cache_hours: int = Field(default=24, ge=0, le=168)
    # 默认复用同一智谱端点的凭证；独立端点必须显式配置独立密钥。
    embedding_backend: Literal["local", "api"] = "local"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_cache_dir: str = "./data/models"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = Field(default=None, repr=False)
    embedding_dimensions: int = Field(default=512, ge=1, le=4096)
    enable_embeddings: bool = True
    enable_rerank: bool = True
    review_output_mode: Literal["json", "tool"] = "json"

    # 笔记与摘要配置。
    enable_notes: bool = True
    notes_workspace: str = Field(default="./notes", min_length=1)
    strip_thinking_tokens: bool = True
    #证据
    evidence_max_chars: int = Field(
        default=2000,
        gt=0,
        le=20000,
    )

    @model_validator(mode="after")
    def validate_web_rag_limits(self) -> Configuration:
        """拒绝会令检索预算自相矛盾的参数组合。"""
        if self.web_rag_overlap_chars >= self.web_rag_chunk_chars:
            raise ValueError("WEB_RAG_OVERLAP_CHARS 必须小于 WEB_RAG_CHUNK_CHARS")
        if self.web_rag_top_k > self.web_rag_candidates:
            raise ValueError("WEB_RAG_TOP_K 不能大于 WEB_RAG_CANDIDATES")
        if (self.embedding_backend == "local"
                and self.embedding_model == "BAAI/bge-small-zh-v1.5"
                and self.embedding_dimensions != 512):
            raise ValueError("BAAI/bge-small-zh-v1.5 的向量维度固定为 512")
        return self

    @classmethod
    def from_env(
        cls,
        overrides: Mapping[str, Any] | None = None,
        *,
        env_file: str | Path | None = DEFAULT_ENV_FILE,
    ) -> Configuration:
        """按默认值、文件、系统环境、显式覆盖的顺序构造配置。"""
        file_values = (
            dotenv_values(env_file, interpolate=False)
            if env_file is not None
            else {}
        )

        values: dict[str, Any] = {}

        for field_name in cls.model_fields:
            env_name = field_name.upper()

            if env_name in os.environ:
                values[field_name] = os.environ[env_name]
            elif file_values.get(env_name) is not None:
                values[field_name] = file_values[env_name]

        if overrides is not None:
            values.update(overrides)

        return cls.model_validate(values)

    def require_llm(self) -> None:
        """在实际创建模型客户端时检查所需配置。"""
        if not self.llm_model_id:
            raise ValueError("请配置 LLM_MODEL_ID")

        if not self.llm_base_url:
            raise ValueError("请配置 LLM_BASE_URL")

        url = urlsplit(self.llm_base_url)

        if url.scheme not in {"http", "https"} or not url.netloc:
            raise ValueError(
                "LLM_BASE_URL 必须是完整的 HTTP/HTTPS 地址"
            )

        if url.username or url.password or url.query or url.fragment:
            raise ValueError(
                "LLM_BASE_URL 只填写服务地址，凭证放入 LLM_API_KEY"
            )

        if (
            self.llm_api_key is None
            or not self.llm_api_key.get_secret_value().strip()
        ):
            raise ValueError("请配置 LLM_API_KEY")

    def require_search(self) -> None:
        """在实际创建搜索工具时检查所需配置。"""
        if self.search_api == SearchAPI.TAVILY:
            if (
                self.tavily_api_key is None
                or not self.tavily_api_key.get_secret_value().strip()
            ):
                raise ValueError(
                    "使用 Tavily 时请配置 TAVILY_API_KEY"
                )

        if (
            self.search_api == SearchAPI.DUCKDUCKGO
            and self.fetch_full_page
        ):
            raise ValueError(
                "当前 DuckDuckGo 实现不支持 FETCH_FULL_PAGE=true"
            )

    def snapshot(self) -> dict[str, Any]:
        """生成可以写入数据库的非敏感配置快照。"""
        data = self.model_dump(
            mode="json",
            include=SNAPSHOT_FIELDS,
        )

        data["notes_workspace"] = str(
            Path(self.notes_workspace).expanduser().resolve()
        )
        data["web_rag_workspace"] = str(Path(self.web_rag_workspace).expanduser().resolve())
        data["embedding_cache_dir"] = str(Path(self.embedding_cache_dir).expanduser().resolve())

        return data

    def for_resume(
        self,
        snapshot: Mapping[str, Any],
    ) -> Configuration:
        """恢复原研究参数，凭证继续使用当前配置中的值。"""
        if set(snapshot) - SNAPSHOT_FIELDS:
            raise ValueError(
                "研究快照包含未知配置字段，请先迁移快照"
            )

        # 旧研究没有记录这个选项，应沿用当时不改写的行为。
        # 新研究在 snapshot() 中保存开关，恢复时不受当前环境开关影响。
        saved = dict(snapshot)
        saved.setdefault("enable_query_rewrite", False)
        # 旧检查点沿用原拓扑；不能在恢复时突然插入报告循环。
        saved.setdefault("enable_report_reflection", False)
        saved.setdefault("enable_web_rag", False)
        saved.setdefault("review_output_mode", "tool")
        restored = type(self).model_validate({
            **self.model_dump(),
            **saved,
        })

        current_endpoint = (
            self.llm_provider,
            (self.llm_base_url or "").rstrip("/"),
        )

        original_endpoint = (
            restored.llm_provider,
            (restored.llm_base_url or "").rstrip("/"),
        )

        if current_endpoint != original_endpoint:
            raise ValueError(
                "恢复时请使用原研究的 LLM_PROVIDER 和 LLM_BASE_URL"
            )

        if restored.enable_web_rag and restored.embedding_backend == "api" and (self.embedding_base_url or self.llm_base_url) != (
            restored.embedding_base_url or restored.llm_base_url
        ):
            raise ValueError("恢复时请使用原研究的 Embedding 服务地址")

        return restored


@lru_cache(maxsize=1)
def get_config() -> Configuration:
    """每个进程首次调用时加载配置，后续复用。"""
    return Configuration.from_env()
