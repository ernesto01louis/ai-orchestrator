"""Per-run-id buffer for LlmCall records.

State hooks (prefect_io.state_hooks) append records when an `llm-call`-tagged
task completes; the evidence builder (evidence.builder) drains the buffer at
flow completion to populate the `llm_calls[]` array on the EvidenceBundle.

Thread-safe: appends and drains may interleave from worker threads + the
Prefect event loop.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class LlmCallRecord:
    run_id: str
    model: str
    rendered_messages: list[dict[str, Any]]
    sampling: dict[str, Any]
    response_tokens: int
    duration_ms: int
    # Phase J β citation-grade fields. Defaults keep legacy constructors valid.
    call_id: str = ""
    agent_role: str = ""
    server_url: str = ""
    model_digest: str = ""
    model_size_bytes: int = 0
    response_text: str = ""
    started_at: datetime | None = None


class LlmCallLogger:
    def __init__(self) -> None:
        self._buf: dict[str, list[LlmCallRecord]] = defaultdict(list)
        self._lock = threading.Lock()

    def append(self, record: LlmCallRecord) -> None:
        with self._lock:
            self._buf[record.run_id].append(record)

    def drain(self, run_id: str) -> list[LlmCallRecord]:
        with self._lock:
            records = self._buf.pop(run_id, [])
        return records


# Module-level singleton; imported by state_hooks and evidence.builder.
LLM_CALL_LOG = LlmCallLogger()
