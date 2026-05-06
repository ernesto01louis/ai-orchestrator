"""Phase 1.2.1 — HTML viewer for evidence bundles.

Renders an ``EvidenceBundle`` as a single self-contained HTML page (CSS +
JS inlined, JSON embedded as a script tag) so a campaign's evidence can be
inspected by opening ``evidence.html`` in a browser — no server, no
network. Snakemake-style.

Pure repackaging — does not introduce any new data; the page reads the
same evidence.json the rest of the bundle is built from. The viewer is
covered by the bundle's signed manifest because ``evidence.html`` lands
in the crate dir before ``_build_statement`` walks it.
"""
from __future__ import annotations

import html
import json
from datetime import datetime

from core.evidence import EvidenceBundle


def build_html(bundle: EvidenceBundle) -> str:
    """Return a self-contained HTML page for ``bundle``.

    The bundle is dumped to JSON and embedded in a ``<script type="application/json">``
    tag so client-side JS can render it without a fetch (works under
    ``file://``).
    """
    payload = bundle.model_dump(mode="json")
    payload_json = json.dumps(payload, default=_json_default, indent=None)

    title = html.escape(f"{bundle.campaign_name} — evidence bundle")
    return _TEMPLATE.replace("__TITLE__", title).replace(
        "__BUNDLE_JSON__", payload_json
    )


def _json_default(obj: object) -> str:
    """JSON serializer for datetimes and other non-trivial Pydantic outputs."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Template
#
# Kept inline in this module on purpose — citation-grade artefacts shouldn't
# fail to render because a sibling .html template went missing during a
# partial install. JSON is embedded via a <script type="application/json">
# tag (NOT interpolated into a JS string) so any string value in the bundle
# can survive without quote-escaping; the viewer parses the JSON at load.
# ---------------------------------------------------------------------------

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #0e1117;
    --panel: #161b22;
    --panel-2: #1c2128;
    --fg: #e6edf3;
    --muted: #7d8590;
    --accent: #58a6ff;
    --ok: #3fb950;
    --warn: #d29922;
    --err: #f85149;
    --border: #30363d;
    --mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
  }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 14px; line-height: 1.5; }
  main { max-width: 1100px; margin: 2rem auto; padding: 0 1.5rem; }
  h1 { margin: 0 0 0.25rem; font-size: 1.6rem; }
  h2 { margin: 2rem 0 0.5rem; font-size: 1.15rem; border-bottom: 1px solid var(--border); padding-bottom: 0.25rem; }
  h3 { margin: 1rem 0 0.25rem; font-size: 1rem; color: var(--muted); }
  code, pre, .mono { font-family: var(--mono); font-size: 12.5px; }
  pre { background: var(--panel-2); padding: 0.6rem 0.8rem; border-radius: 4px; border: 1px solid var(--border); overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; font-size: 13px; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  tr:hover td { background: var(--panel-2); }
  .meta { color: var(--muted); font-size: 12.5px; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; border: 1px solid var(--border); margin-right: 4px; }
  .pill-ok { color: var(--ok); border-color: var(--ok); }
  .pill-warn { color: var(--warn); border-color: var(--warn); }
  .pill-err { color: var(--err); border-color: var(--err); }
  details { background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: 0.6rem 0.9rem; margin: 0.4rem 0; }
  details > summary { cursor: pointer; font-weight: 500; }
  details[open] > summary { margin-bottom: 0.5rem; color: var(--accent); }
  .kv { display: grid; grid-template-columns: 12rem 1fr; gap: 0.25rem 1rem; }
  .kv > dt { color: var(--muted); }
  .kv > dd { margin: 0; word-break: break-word; }
  .small { font-size: 12px; }
  .truncate { max-width: 32rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: bottom; }
  .footer { margin: 3rem 0 1rem; color: var(--muted); font-size: 12px; text-align: center; }
  hr { border: 0; border-top: 1px solid var(--border); margin: 2rem 0; }
  a { color: var(--accent); }
</style>
</head>
<body>
<main id="root"><p class="meta">loading…</p></main>

<script type="application/json" id="bundle-data">
__BUNDLE_JSON__
</script>

<script>
(function() {
  const raw = document.getElementById("bundle-data").textContent;
  const B = JSON.parse(raw);
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  const fmtTs = (s) => { try { return new Date(s).toISOString().replace("T", " ").slice(0,19) + "Z"; } catch { return s; } };
  const truncate = (s, n=120) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n) + "…" : s; };

  function statusPill(s) {
    if (!s) return "";
    const v = String(s).toLowerCase();
    let cls = "pill";
    if (["success","completed","ok","passed"].includes(v)) cls += " pill-ok";
    else if (["fail","failed","error","aborted"].includes(v)) cls += " pill-err";
    else if (["paused","running","scheduled","cancelling"].includes(v)) cls += " pill-warn";
    return `<span class="${cls}">${esc(s)}</span>`;
  }

  function kv(map) {
    return `<dl class="kv">${Object.entries(map).map(([k,v]) =>
      `<dt>${esc(k)}</dt><dd>${v == null ? '<span class="meta">—</span>' : esc(v)}</dd>`
    ).join("")}</dl>`;
  }

  function table(rows, cols) {
    if (!rows || rows.length === 0) return '<p class="meta small">none.</p>';
    const head = cols.map(c => `<th>${esc(c.label)}</th>`).join("");
    const body = rows.map(r => "<tr>" + cols.map(c => `<td>${c.render ? c.render(r) : esc(r[c.key] ?? "")}</td>`).join("") + "</tr>").join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  // ---- sections ---------------------------------------------------------

  function header() {
    return `
      <h1>${esc(B.campaign_name)}</h1>
      <p class="meta">
        <span class="pill">${esc(B.schema_version)}</span>
        <span class="mono">${esc(B.bundle_id)}</span> &middot;
        campaign <span class="mono">${esc(B.campaign_id)}</span> &middot;
        built ${esc(fmtTs(B.created_at))}
      </p>
      <h3>hypothesis (REFORMS §1)</h3>
      <pre>${esc(B.hypothesis)}</pre>
      <h3>abstract</h3>
      <pre>${esc(B.abstract)}</pre>
    `;
  }

  function fingerprintBlock() {
    const code = B.code || {};
    const hw = B.hardware || {};
    return `
      <h2>fingerprints</h2>
      <details open><summary>code</summary>${kv({
        "git remote": code.git_remote, "git sha": code.git_sha, "dirty": code.git_dirty,
        "requirements_lock_sha256": code.requirements_lock_sha256,
      })}</details>
      <details><summary>hardware</summary>${kv(hw)}</details>
    `;
  }

  function llmTargets() {
    return `
      <h2>LLM targets <span class="meta">(${(B.llm_targets || []).length})</span></h2>
      ${table(B.llm_targets, [
        { label: "role", key: "role" },
        { label: "model", render: r => `<span class="mono">${esc(r.model_name)}</span>` },
        { label: "host", render: r => `<span class="mono small">${esc(r.host)}</span>` },
        { label: "digest", render: r => `<span class="mono small truncate" title="${esc(r.model_digest)}">${esc(r.model_digest)}</span>` },
        { label: "size", render: r => r.model_size_bytes ? (r.model_size_bytes / 1024 / 1024 / 1024).toFixed(2) + " GB" : "—" },
      ])}
    `;
  }

  function runRecord(run) {
    const llmRows = (run.llm_calls || []).map(c => `
      <tr>
        <td><span class="mono small">${esc((c.call_id || '').slice(0,8))}</span></td>
        <td>${esc(c.role)}</td>
        <td><span class="mono small">${esc(c.target?.model_name)}</span></td>
        <td>${c.response_tokens ?? '—'}</td>
        <td>${c.latency_ms ?? '—'} ms</td>
        <td><span class="mono small truncate" title="${esc(c.response_text || '')}">${esc(truncate(c.response_text, 80))}</span></td>
      </tr>`).join("");
    const execRows = (run.code_executions || []).map(e => `
      <tr>
        <td>${esc(e.language)}</td>
        <td>${e.return_code === 0 ? statusPill('ok') : statusPill('fail')}</td>
        <td>${e.duration_ms ?? '—'} ms</td>
        <td><span class="mono small truncate" title="${esc(e.code_sha256 || '')}">${esc((e.code_sha256 || '').slice(0,16))}…</span></td>
        <td><pre style="max-height: 8em; margin: 0;">${esc(truncate(e.stdout || '', 400))}</pre></td>
      </tr>`).join("");
    const metrics = run.metrics || {};
    return `
      <details>
        <summary>${esc(run.run_id)} ${statusPill(run.status)} <span class="meta small">params: ${esc(JSON.stringify(run.parameters))}</span></summary>
        <h3>metrics</h3>${Object.keys(metrics).length ? kv(metrics) : '<p class="meta small">none.</p>'}
        <h3>LLM calls <span class="meta">(${(run.llm_calls || []).length})</span></h3>
        ${run.llm_calls && run.llm_calls.length ? `<table><thead><tr><th>call_id</th><th>role</th><th>model</th><th>tokens</th><th>latency</th><th>response</th></tr></thead><tbody>${llmRows}</tbody></table>` : '<p class="meta small">none.</p>'}
        <h3>code executions <span class="meta">(${(run.code_executions || []).length})</span></h3>
        ${run.code_executions && run.code_executions.length ? `<table><thead><tr><th>lang</th><th>status</th><th>duration</th><th>code sha256</th><th>stdout</th></tr></thead><tbody>${execRows}</tbody></table>` : '<p class="meta small">none.</p>'}
      </details>
    `;
  }

  function runs() {
    return `
      <h2>runs <span class="meta">(${(B.runs || []).length})</span></h2>
      ${(B.runs || []).map(runRecord).join("")}
    `;
  }

  function calculators() {
    if (!B.calculators || B.calculators.length === 0) return "";
    return `
      <h2>calculators <span class="meta">(${B.calculators.length})</span></h2>
      ${B.calculators.map(c => `
        <details><summary>${esc(c.name)} <span class="meta small">${esc(c.calculator)}</span></summary>
        <pre>${esc(JSON.stringify(c.results, null, 2))}</pre>
        </details>
      `).join("")}
    `;
  }

  function markdownDict(title, dict) {
    const entries = Object.entries(dict || {});
    if (entries.length === 0) return "";
    return `
      <h2>${esc(title)} <span class="meta">(${entries.length})</span></h2>
      ${entries.map(([k, v]) => `<details><summary>${esc(k)}</summary><pre>${esc(v)}</pre></details>`).join("")}
    `;
  }

  function artifacts() {
    if (!B.artifacts || B.artifacts.length === 0) return "";
    return `
      <h2>artifacts <span class="meta">(${B.artifacts.length})</span></h2>
      ${table(B.artifacts, [
        { label: "path", render: r => `<span class="mono small">${esc(r.path)}</span>` },
        { label: "role", key: "role" },
        { label: "media_type", key: "media_type" },
        { label: "sha256", render: r => `<span class="mono small truncate" title="${esc(r.sha256)}">${esc((r.sha256 || '').slice(0,12))}…</span>` },
        { label: "size", render: r => r.size_bytes ? (r.size_bytes < 1024 ? r.size_bytes + ' B' : (r.size_bytes / 1024).toFixed(1) + ' KB') : '—' },
      ])}
    `;
  }

  // ---- assemble ---------------------------------------------------------
  const root = $("#root");
  root.innerHTML = [
    header(),
    fingerprintBlock(),
    llmTargets(),
    runs(),
    calculators(),
    markdownDict("REFORMS responses", B.reforms_responses),
    markdownDict("NeurIPS responses", B.neurips_responses),
    markdownDict("model cards", B.model_cards),
    markdownDict("datasheets", B.datasheets),
    artifacts(),
    `<div class="footer">evidence.html viewer · static, self-contained · open with file:// or any web server</div>`,
  ].join("");

  document.title = `${B.campaign_name} — evidence bundle`;
})();
</script>
</body>
</html>
"""
