from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SEMANTIC_REVIEW_MAX_SECONDS = 660
MODEL_FIRST_MAX_SECONDS = 10000
SEMANTIC_REVIEW_FINISH_ALLOWANCE_SECONDS = 15


def semantic_review_required_seconds(*, model_calls, provider_timeout_seconds,
                                     tool_calls, database_timeout_ms,
                                     web_search_enabled=False, web_timeout_seconds=0):
    """Reserve time for every model/tool attempt and bounded final delivery.

    This is a finite execution deadline, independent of monetary accounting.
    A timeout still follows the normal unknown-outcome reservation policy.
    """
    tool_seconds = max(database_timeout_ms / 1000,
                       web_timeout_seconds if web_search_enabled else 0)
    return ceil(model_calls * provider_timeout_seconds + tool_calls * tool_seconds
                + SEMANTIC_REVIEW_FINISH_ALLOWANCE_SECONDS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARK_",
        env_file=(".env", ".env.deepseek.local"),
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://localhost:55432/arknights"
    snapshot_root: Path = Path(".local/snapshots")
    openai_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 20
    # Keys are loaded only by the application. Public status exposes booleans.
    deepseek_api_key: SecretStr | None = None
    credential_store_path: Path | None = None
    credential_master_key_path: Path | None = None
    deepseek_model: str | None = None
    api_protocol: Literal['deepseek', 'compatible', 'openai', 'anthropic'] = 'deepseek'
    api_base_url: str | None = None
    api_connection_id: str = 'deepseek'
    model_live_enabled: bool = False
    deepseek_timeout_seconds: float = Field(default=15, ge=1, le=60)
    model_budget_path: Path = Path(".local/model-budget.sqlite3")
    # Web failures end one task; CLI evaluation ledgers retain their own policy.
    web_task_failure_isolation: bool = True
    model_total_budget_cny: Decimal = Field(default=Decimal("18"), gt=0, le=18)
    model_budget_safety_fraction: Decimal = Field(default=Decimal("0.8"), gt=0, le=0.8)
    model_input_cny_per_million: Decimal | None = Field(default=None, gt=0)
    model_output_cny_per_million: Decimal | None = Field(default=None, gt=0)
    model_price_verified_at: datetime | None = None
    model_price_source: str = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
    agent_roguelike_delivery: Literal["verified_query", "legacy_submit"] = "verified_query"
    agent_semantic_review: bool = False
    agent_model_first: bool = False
    agent_execution_engine: Literal["legacy", "langgraph"] = "legacy"
    agent_context_tokens: int = Field(default=524288, ge=4096, le=1048576)
    agent_progress_initial_calls: int = Field(default=12, ge=6, le=64)
    agent_simple_reasoning_effort: Literal['low', 'medium', 'high'] = 'low'
    agent_complex_reasoning_effort: Literal['low', 'medium', 'high'] = 'medium'
    agent_progress_extension_calls: int = Field(default=4, ge=2, le=16)
    agent_progress_max_calls: int = Field(default=32, ge=8, le=64)
    agent_progress_max_tools: int = Field(default=96, ge=12, le=192)
    agent_revision_reserve_calls: int = Field(default=4, ge=4, le=8)
    agent_idle_repeat_limit: int = Field(default=3, ge=2, le=6)
    agent_max_model_calls: int = Field(default=3, ge=1, le=64)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=192)
    agent_large_output_enabled: bool = False
    agent_max_output_tokens: int = Field(default=1024, ge=1, le=8192)
    agent_max_total_tokens: int = Field(default=50000, ge=2048, le=34_000_000)
    agent_max_prompt_bytes: int = Field(default=200000, ge=4096, le=8 * 1024 * 1024)
    agent_max_total_seconds: float = Field(default=45, ge=1, le=MODEL_FIRST_MAX_SECONDS)
    database_tool_timeout_ms: int = Field(default=2000, ge=100, le=5000)
    web_search_enabled: bool = False
    web_search_timeout_seconds: float = Field(default=5, ge=1, le=10)
    web_search_max_bytes: int = Field(default=131072, ge=1024, le=131072)
    tool_result_max_bytes: int = Field(default=32768, ge=1024, le=1048576)

    @model_validator(mode="after")
    def review_execution_bound(self):
        if not self.agent_model_first and (
            self.agent_max_model_calls > 8 or self.agent_max_tool_calls > 12
            or self.agent_max_prompt_bytes > 300000 or self.tool_result_max_bytes > 65536
            or self.agent_max_total_seconds > 660 or self.agent_max_total_tokens > 2_000_000
        ):
            raise ValueError("expanded execution requires the model-first context contract")
        if self.agent_model_first and not (self.agent_semantic_review and self.agent_large_output_enabled):
            raise ValueError("model-first execution requires reviewed unlimited admission")
        if self.agent_progress_initial_calls > self.agent_progress_max_calls:
            raise ValueError("initial execution allocation exceeds maximum")
        if self.agent_revision_reserve_calls >= self.agent_progress_initial_calls:
            raise ValueError("execution allocation must leave room before revision reserve")
        if self.agent_max_output_tokens > 1024 and not self.agent_large_output_enabled:
            raise ValueError("expanded output requires explicit unlimited runtime admission")
        if self.agent_max_total_tokens > 1_000_000 and not self.agent_large_output_enabled:
            raise ValueError("expanded token allowance requires explicit unlimited runtime admission")
        if self.agent_semantic_review and self.agent_roguelike_delivery != "verified_query":
            raise ValueError("semantic review requires typed verified query delivery")
        if self.agent_max_model_calls > 3 and not self.agent_semantic_review:
            raise ValueError("more than three model calls requires semantic review")
        if self.agent_max_model_calls > 6 and not self.agent_large_output_enabled:
            raise ValueError("more than six model calls requires explicit unlimited runtime admission")
        if self.agent_semantic_review or self.agent_large_output_enabled:
            required = semantic_review_required_seconds(
                model_calls=self.agent_max_model_calls,
                provider_timeout_seconds=self.deepseek_timeout_seconds,
                tool_calls=self.agent_max_tool_calls,
                database_timeout_ms=self.database_tool_timeout_ms,
                web_search_enabled=self.web_search_enabled,
                web_timeout_seconds=self.web_search_timeout_seconds,
            )
            if "agent_max_total_seconds" not in self.model_fields_set:
                object.__setattr__(self, "agent_max_total_seconds", float(required))
            elif self.agent_max_total_seconds < required:
                raise ValueError("semantic review deadline must cover all model/tool attempts and final delivery")
        elif self.agent_max_total_seconds > 60:
            raise ValueError("more than sixty seconds requires semantic review")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
