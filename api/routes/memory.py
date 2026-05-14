"""Memory layers (positive/negative/search) + model-stats + Hindsight passthrough routes (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.

Imports are inherited verbatim from the pre-split preamble — some are
unused in this sub-module; a follow-up commit can trim.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import requests
from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel

from core.config import (
    HINDSIGHT_BANK,
    HINDSIGHT_ENABLED,
    HINDSIGHT_URL,
)
from core.paths import (
    REFERENCE_DIR,
)
from memory_pkg import (
    find_negative_matches,
    find_similar,
    hindsight_get_mental_models,
    hindsight_recall,
    hindsight_reflect,
    hindsight_request,
    hindsight_retain,
    load_model_stats,
    load_negative_memory,
    load_prompt_index,
)
from references_pkg import (
    MAX_REFERENCE_UPLOAD_BYTES,
    convert_file_to_markdown,
)

# Filename safety regex (was at app.py:181 originally)
# UI assets directory served by /ui routes
from ._common import SAFE_FILENAME, UI_DIR  # noqa: F401

router = APIRouter()


@router.get("/memory")
def get_memory():
    """View positive memory (past runs with similarity data)."""

    index = load_prompt_index()

    # return without embeddings (they're huge)
    entries = []
    for entry in index:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        entries.append(e)

    entries.reverse()  # newest first

    return {
        "total": len(entries),
        "entries": entries[:50]  # last 50
    }


@router.get("/memory/negative")
def get_negative_memory():
    """View negative memory (past failures)."""

    entries = load_negative_memory()

    # return without embeddings
    clean = []
    for entry in entries:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        clean.append(e)

    clean.reverse()  # newest first

    return {
        "total": len(clean),
        "entries": clean[:50]
    }


@router.get("/memory/search")
def search_memory(q: str):
    """Search memory by semantic similarity to a query string."""

    positive = find_similar(q)
    negative = find_negative_matches(q)

    pos_results = []
    for sim_score, entry in positive:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        e["similarity"] = round(sim_score, 4)
        pos_results.append(e)

    neg_results = []
    for sim_score, entry in negative:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        e["similarity"] = round(sim_score, 4)
        neg_results.append(e)

    return {
        "query": q,
        "positive_matches": pos_results,
        "negative_matches": neg_results
    }


@router.get("/model-stats")
def get_model_stats_endpoint():
    """View model performance statistics."""

    stats = load_model_stats()

    # compute derived metrics
    summary = {}
    for model, s in stats.items():
        total = s["total_runs"]
        avg_score = s["total_score"] / total if total > 0 else 0
        win_rate = (s["wins"] / total * 100) if total > 0 else 0
        fail_rate = (s["failures"] / total * 100) if total > 0 else 0

        summary[model] = {
            "total_runs": total,
            "avg_score": round(avg_score, 2),
            "win_rate": round(win_rate, 1),
            "fail_rate": round(fail_rate, 1),
            "wins": s["wins"],
            "failures": s["failures"],
            "by_language": s["by_language"],
            "by_role": s["by_role"],
            "by_project_type": s["by_project_type"],
            "recent_trend": [r["score"] for r in s.get("recent_scores", [])]
        }

    return {"models": summary}


@router.get("/model-stats/{model_name}")
def get_single_model_stats(model_name: str):
    """View detailed stats for a specific model."""

    stats = load_model_stats()

    # url-decode the model name (e.g. qwen2.5-coder%3A32b -> qwen2.5-coder:32b)
    import urllib.parse
    model_name = urllib.parse.unquote(model_name)

    if model_name not in stats:
        raise HTTPException(status_code=404, detail=f"No stats for model '{model_name}'")

    s = stats[model_name]
    total = s["total_runs"]

    return {
        "model": model_name,
        "total_runs": total,
        "avg_score": round(s["total_score"] / total, 2) if total > 0 else 0,
        "win_rate": round((s["wins"] / total * 100), 1) if total > 0 else 0,
        "fail_rate": round((s["failures"] / total * 100), 1) if total > 0 else 0,
        "by_language": s["by_language"],
        "by_role": s["by_role"],
        "by_project_type": s["by_project_type"],
        "recent_scores": s.get("recent_scores", [])
    }


@router.get("/hindsight/status")
def hindsight_status():
    """Check if Hindsight is reachable and get bank info."""

    if not HINDSIGHT_ENABLED:
        return {"enabled": False, "status": "disabled in config"}

    try:
        r = requests.get(f"{HINDSIGHT_URL}/v1/default/banks", timeout=10)
        r.raise_for_status()
        banks = r.json()

        return {
            "enabled": True,
            "url": HINDSIGHT_URL,
            "bank_id": HINDSIGHT_BANK,
            "status": "online",
            "banks": banks
        }
    except requests.exceptions.RequestException as e:
        return {
            "enabled": True,
            "url": HINDSIGHT_URL,
            "bank_id": HINDSIGHT_BANK,
            "status": f"offline ({type(e).__name__})"
        }


class HindsightRecallRequest(BaseModel):
    query: str
    max_tokens: int = 2000


@router.post("/hindsight/recall")
def api_hindsight_recall(req: HindsightRecallRequest):
    """Recall memories from Hindsight for a given query."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_recall(req.query, "api-recall", max_tokens=req.max_tokens)

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight recall failed")

    return result


class HindsightRetainRequest(BaseModel):
    content: str


@router.post("/hindsight/retain")
def api_hindsight_retain(req: HindsightRetainRequest):
    """Manually store a memory in Hindsight."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_retain(req.content, "api-retain")

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight retain failed")

    return result


class HindsightReflectRequest(BaseModel):
    query: str


@router.post("/hindsight/reflect")
def api_hindsight_reflect(req: HindsightReflectRequest):
    """
    Ask Hindsight to reflect on accumulated memories.
    This synthesizes observations and opinions from past experiences.
    Can take 1-5 minutes depending on memory volume and model speed.
    """

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_reflect(req.query, "api-reflect")

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight reflect failed")

    return result


# ── Mental Models API ─────────────────────────────


@router.get("/hindsight/mental-models")
def api_mental_models():
    """List all Hindsight mental models with their content."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    models = hindsight_get_mental_models("api")
    return {"models": models}


@router.post("/hindsight/mental-models/{model_id}/refresh")
def api_refresh_mental_model(model_id: str):
    """Trigger a refresh of a specific mental model."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_request(
        "POST",
        f"/v1/default/banks/{HINDSIGHT_BANK}/mental-models/{model_id}/refresh",
        timeout=30
    )

    if result is None:
        raise HTTPException(status_code=502, detail="Mental model refresh failed")

    return result



@router.post("/references/upload")
async def upload_reference(file: UploadFile = File(...)):
    """
    Upload a file as a reference document.
    PDFs are auto-converted to markdown. Text files are stored as-is.
    Everything gets ingested into Hindsight for RAG.
    """

    filename = file.filename or f"ref_{uuid.uuid4().hex[:8]}"
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    local_path = REFERENCE_DIR / safe_name

    # save original file with size limit
    content = await file.read()
    if len(content) > MAX_REFERENCE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large ({len(content)//1024//1024}MB). Max {MAX_REFERENCE_UPLOAD_BYTES//1024//1024}MB.")
    with open(local_path, "wb") as f:
        f.write(content)

    # convert to markdown
    md_text, meta = convert_file_to_markdown(str(local_path), "api-upload")

    # save markdown version alongside original (for PDF → .md)
    md_filename = f"{Path(safe_name).stem}.md"
    md_path = REFERENCE_DIR / md_filename
    if local_path.suffix.lower() != ".md":
        md_path.write_text(md_text)

    # ingest markdown into Hindsight
    hindsight_result = None
    if HINDSIGHT_ENABLED:
        hindsight_result = hindsight_retain(
            f"Reference document '{filename}' uploaded.\n\n{md_text[:3000]}",
            "api-upload"
        )

    return {
        "filename": safe_name,
        "markdown_filename": md_filename,
        "path": str(local_path),
        "size": len(content),
        "markdown_size": len(md_text),
        "conversion": meta,
        "hindsight_ingested": hindsight_result is not None,
    }


@router.post("/hindsight/reflect/auto")
def api_hindsight_auto_reflect():
    """
    Trigger automatic reflection on key orchestrator topics.
    Asks Hindsight to synthesize insights about model performance,
    common failure patterns, and language/task-type trends.
    """

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    queries = [
        "Which models perform best for which programming languages and task types?",
        "What are the most common failure patterns and how can they be avoided?",
        "What deployment and execution patterns have been most reliable?",
    ]

    results = []
    for q in queries:
        result = hindsight_reflect(q, "auto-reflect")
        results.append({
            "query": q,
            "result": result
        })

    return {"reflections": results}
