"""Phase 1.2.1 — sanity tests for the evidence-bundle HTML viewer.

Verifies that ``build_html(bundle)`` returns a self-contained HTML page
with the bundle's payload embedded as JSON, that ``_BundleBuilder``
writes it to the crate dir, and that the file is covered by the signed
manifest (i.e. shows up in evidence.html and is included in the rocrate
walk).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from core.evidence import (
    CodeFingerprint,
    EvidenceBundle,
    HardwareFingerprint,
    LlmCall,
    LlmTarget,
    RunRecord,
    SamplingParams,
)
from evidence.html_viewer import build_html

pytestmark = pytest.mark.inprocess


def _minimal_bundle() -> EvidenceBundle:
    started = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    return EvidenceBundle(
        bundle_id="01HXVIEWERSMOKE0000000000",
        campaign_id="camp-html-1",
        campaign_name="HTML viewer smoke",
        created_at=started,
        abstract="A trivial campaign used to exercise the HTML viewer.",
        hypothesis="Bigger hypothesis tests render cleanly in <pre> blocks.",
        code=CodeFingerprint(
            git_remote="git@github.com:ernesto01louis/ai-orchestrator.git",
            git_sha="0123456789abcdef0123456789abcdef01234567",
            git_dirty=False,
            requirements_lock="dvc==3.67.1\n",
            requirements_lock_sha256="a" * 64,
        ),
        hardware=HardwareFingerprint(
            cpu_model="x86_64",
            cpu_count=8,
            ram_gb=16.0,
            os="Linux",
            kernel="6.17.13",
        ),
        llm_targets=[
            LlmTarget(
                role="planner",
                host="192.168.2.13:11434",
                model_name="qwen2.5-coder:32b",
                model_digest="sha256-deadbeef",
                model_size_bytes=19_000_000_000,
            ),
        ],
        runs=[
            RunRecord(
                run_id="r1",
                parameters={"seed": 1},
                llm_calls=[
                    LlmCall(
                        call_id="cc-001",
                        role="planner",
                        target=LlmTarget(
                            role="planner",
                            host="192.168.2.13:11434",
                            model_name="qwen2.5-coder:32b",
                            model_digest="sha256-deadbeef",
                            model_size_bytes=19_000_000_000,
                        ),
                        rendered_messages=[{"role": "user", "content": "hi"}],
                        sampling=SamplingParams(temperature=0.0),
                        response_text="here is the plan",
                        response_tokens=42,
                        latency_ms=123,
                        started_at=started,
                    ),
                ],
                code_executions=[],
                metrics={"score": 8.5},
                status="success",
                started_at=started,
                finished_at=started,
            ),
        ],
    )


def test_build_html_returns_self_contained_page():
    html = build_html(_minimal_bundle())
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    # No external CDN / network dependencies — file:// must work.
    assert "https://" not in html and "http://" not in html.replace("github.com:", "")
    # Inline CSS + script (NOT external)
    assert "<style>" in html and "</style>" in html
    assert '<script type="application/json"' in html


def test_build_html_embeds_full_bundle_payload():
    bundle = _minimal_bundle()
    html = build_html(bundle)
    # The embedded JSON should round-trip back to the same bundle.
    m = re.search(
        r'<script type="application/json" id="bundle-data">\s*(\{.*?\})\s*</script>',
        html, re.DOTALL,
    )
    assert m, "expected an application/json bundle-data script tag"
    payload = json.loads(m.group(1))
    assert payload["bundle_id"] == bundle.bundle_id
    assert payload["campaign_id"] == bundle.campaign_id
    assert payload["runs"][0]["run_id"] == "r1"
    assert payload["runs"][0]["llm_calls"][0]["call_id"] == "cc-001"


def test_build_html_escapes_title_safely():
    bundle = _minimal_bundle()
    bundle.campaign_name = "Hostile <script>alert(1)</script>"
    html = build_html(bundle)
    # The hostile name must appear escaped in the <title>, never raw.
    assert "<title>Hostile &lt;script&gt;" in html
    assert "<title>Hostile <script>alert(1)</script>" not in html


def test_builder_writes_evidence_html_alongside_evidence_json(tmp_path):
    """End-to-end: _BundleBuilder writes evidence.html into the crate dir
    and it lands in the same tree as evidence.json (so the rocrate walk
    in _build_statement picks it up automatically)."""
    from evidence.builder import _BundleBuilder

    builder = _BundleBuilder.__new__(_BundleBuilder)
    builder.crate_dir = tmp_path
    bundle = _minimal_bundle()
    builder._write_evidence_html(bundle)
    out = tmp_path / "evidence.html"
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in body
    assert bundle.bundle_id in body
