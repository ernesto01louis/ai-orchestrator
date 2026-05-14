"""Shared module-level constants (SAFE_FILENAME, UI_DIR) routes (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.

Imports are inherited verbatim from the pre-split preamble — some are
unused in this sub-module; a follow-up commit can trim.
"""
from __future__ import annotations

import re
from pathlib import Path

# Filename safety regex (mirrors server-side SAFE_FILENAME in
# app.py / api/routes/__init__.py / orchestration / llm.extract).
SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")

# UI assets directory served by /ui routes (admin sub-module).
UI_DIR = Path("/opt/ai-orchestrator/ui")
