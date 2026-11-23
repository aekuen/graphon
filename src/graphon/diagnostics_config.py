"""Environment-backed configuration for graph execution diagnostics."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class DiagnosticsConfig(BaseModel):
    """Diagnostics switches read from the process environment."""

    model_config = ConfigDict(frozen=True)

    GRAPHON_TRACE_FILE: str = Field(
        default="",
        description="JSONL event output path; empty disables diagnostics",
    )
    GRAPHON_TRACE_SIGNAL: str = Field(
        default="",
        description="Optional full-thread stack dump signal name, e.g. SIGUSR1",
    )
    GRAPHON_TRACE_QUEUE_SIZE: int = Field(
        default=10_000,
        gt=0,
        description="Bounded diagnostic event queue capacity; drops are counted",
    )
    GRAPHON_TRACE_FLUSH_TIMEOUT: float = Field(
        default=0.5,
        gt=0,
        description="退出时等待诊断写入线程的最长秒数",
    )

    @classmethod
    def from_env(cls) -> DiagnosticsConfig:
        return cls.model_validate(
            {
                "GRAPHON_TRACE_FILE": os.getenv("GRAPHON_TRACE_FILE", ""),
                "GRAPHON_TRACE_SIGNAL": os.getenv("GRAPHON_TRACE_SIGNAL", ""),
                "GRAPHON_TRACE_QUEUE_SIZE": os.getenv(
                    "GRAPHON_TRACE_QUEUE_SIZE", "10000"
                ),
                "GRAPHON_TRACE_FLUSH_TIMEOUT": os.getenv(
                    "GRAPHON_TRACE_FLUSH_TIMEOUT",
                    "0.5",
                ),
            },
        )
