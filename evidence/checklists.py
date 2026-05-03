"""Reproducibility checklist auto-fill (Phase 1.2).

Produces:

* ``reforms_responses: dict[str, str]`` — 32 REFORMS items
  (`Kapoor et al., Sci Adv 2024 <https://www.science.org/doi/10.1126/sciadv.adk3452>`_)
  across 8 modules. Items the bundle's data already answers are
  filled with that data (Markdown); the rest get an explicit
  ``TODO`` stub the user fills via API.

* ``neurips_responses: dict[str, str]`` — NeurIPS Paper Checklist
  reproducibility-relevant questions Q4-Q8
  (`NeurIPS guidance <https://neurips.cc/public/guides/PaperChecklist>`_).

* ``model_cards: dict[str, str]`` — one Markdown card per LLM target
  (Mitchell et al. 2019 schema, ``arxiv:1810.03993``).

* ``datasheets: dict[str, str]`` — one per data-input identifier
  (Gebru et al. 2018 schema, ``arxiv:1803.09010``). For LLM-orchestration
  campaigns the only data input is the prompt corpus, so we emit a
  single ``prompt_corpus`` datasheet.

The bundle's authoritative claim is the typed fields (code, hardware,
runs, llm_targets); these checklists are the *narrative* layer that
satisfies academic reproducibility reviewers. The orchestrator can
honestly auto-fill ~half; the rest is on the researcher.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.evidence import (
        CalculatorResult,
        CodeFingerprint,
        HardwareFingerprint,
        LlmTarget,
        RunRecord,
    )


_TODO = "TODO (researcher to fill via POST /campaigns/{id}/evidence/checklist)"


# Canonical REFORMS items. Numbering matches the order in which the
# 8 modules are presented in the REFORMS reference table. Each tuple
# is (item_id, item_question_summary). Items not auto-fillable from
# the bundle's data get the _TODO stub on emit.
_REFORMS_ITEMS: list[tuple[str, str]] = [
    # 1. Study design
    ("1.1", "Research question and pre-registered hypothesis"),
    ("1.2", "Target population / estimand"),
    ("1.3", "Sampling frame and inclusion criteria"),
    ("1.4", "Connection between research question and ML task"),
    # 2. Computational reproducibility
    ("2.1", "Code availability and version control state"),
    ("2.2", "Computing environment and dependency lock"),
    ("2.3", "Hardware specifications used for the experiments"),
    # 3. Data quality
    ("3.1", "Data provenance and acquisition method"),
    ("3.2", "Sample selection and exclusions"),
    ("3.3", "Sampling frame coverage and known biases"),
    ("3.4", "Data licensing and consent"),
    # 4. Data preprocessing
    ("4.1", "Preprocessing pipeline ordering and operations"),
    ("4.2", "Train/validation/test splits"),
    ("4.3", "Splitting strategy (random / stratified / temporal / by-subject)"),
    ("4.4", "Leakage-prevention measures during preprocessing"),
    # 5. Modeling
    ("5.1", "Model class(es) and architecture choices"),
    ("5.2", "Hyperparameters and how they were chosen"),
    ("5.3", "Hyperparameter tuning protocol and search space"),
    ("5.4", "Random seeds and determinism notes"),
    ("5.5", "Sampling parameters used at inference time"),
    # 6. Data leakage
    ("6.1", "Train-test contamination check"),
    ("6.2", "Pre-processing applied per-split (no fit on test)"),
    ("6.3", "Temporal leakage check (no future data in features)"),
    ("6.4", "Subject/group leakage check"),
    # 7. Metrics & uncertainty quantification
    ("7.1", "Primary metric definition and motivation"),
    ("7.2", "Statistical summary (mean, sd, CI, n)"),
    ("7.3", "Baselines and competitive comparisons"),
    ("7.4", "Multiple-comparison correction if applicable"),
    # 8. Generalisability
    ("8.1", "Distribution-shift evaluation"),
    ("8.2", "Out-of-distribution behaviour"),
    ("8.3", "Subgroup performance (where applicable)"),
    ("8.4", "Known limits and caveats of the conclusion"),
]


_NEURIPS_ITEMS: list[tuple[str, str]] = [
    ("Q4", "Reproducibility steps — code, model access, instructions, or checkpoints"),
    ("Q5", "Open access to data and code"),
    ("Q6", "Experimental settings — splits, hyperparameters, and how chosen"),
    ("Q7", "Statistical significance — error bars and CIs"),
    ("Q8", "Compute resources — hardware, memory, storage, execution time"),
]


# ── public entry points ──────────────────────────────


def build_reforms_responses(
    *,
    hypothesis: str,
    code: "CodeFingerprint",
    hardware: "HardwareFingerprint",
    llm_targets: "list[LlmTarget]",
    runs: "list[RunRecord]",
    calculators: "list[CalculatorResult]",
) -> dict[str, str]:
    """Produce a REFORMS response dict, auto-filling what we can.

    Items the bundle's typed fields answer get a Markdown-formatted
    answer; everything else gets ``_TODO``.
    """
    auto = _reforms_autofills(
        hypothesis=hypothesis,
        code=code,
        hardware=hardware,
        llm_targets=llm_targets,
        runs=runs,
        calculators=calculators,
    )
    return {item_id: auto.get(item_id, _TODO) for item_id, _ in _REFORMS_ITEMS}


def build_neurips_responses(
    *,
    code: "CodeFingerprint",
    hardware: "HardwareFingerprint",
    runs: "list[RunRecord]",
    calculators: "list[CalculatorResult]",
) -> dict[str, str]:
    """Produce a NeurIPS Q4-Q8 response dict, auto-filling what we can."""
    return {
        "Q4": (
            f"Code is available at {code.git_remote} (HEAD = {code.git_sha}, "
            f"{'dirty' if code.git_dirty else 'clean'}). Per-run logs and "
            "rendered prompts are included as bundle artifacts; the campaign "
            "configuration is in this bundle's ``llm_targets`` and "
            "``runs[].parameters``."
        ),
        "Q5": (
            f"Code: {code.git_remote}@{code.git_sha}. Dependency lock "
            f"sha256={code.requirements_lock_sha256[:12]}…; full lock contents "
            "embedded in ``code.requirements_lock``."
        ),
        "Q6": _neurips_q6(runs),
        "Q7": _neurips_q7(calculators),
        "Q8": _neurips_q8(hardware, calculators),
    }


def build_model_cards(targets: "list[LlmTarget]") -> dict[str, str]:
    """One Mitchell-style model card per LLM target."""
    return {t.model_name: _model_card(t) for t in targets}


def build_datasheets(prompts: list[str]) -> dict[str, str]:
    """One Gebru-style datasheet per data input.

    For LLM-orchestration campaigns the only data input is the prompt
    corpus, so a single ``prompt_corpus`` datasheet is emitted.
    ``prompts`` is the deduped list of rendered prompts seen across the
    campaign.
    """
    return {"prompt_corpus": _prompt_corpus_datasheet(prompts)}


# ── REFORMS auto-fill rules ──────────────────────────


def _reforms_autofills(
    *,
    hypothesis: str,
    code: "CodeFingerprint",
    hardware: "HardwareFingerprint",
    llm_targets: "list[LlmTarget]",
    runs: "list[RunRecord]",
    calculators: "list[CalculatorResult]",
) -> dict[str, str]:
    """Subset of REFORMS items the orchestrator can answer from data alone."""
    auto: dict[str, str] = {}

    auto["1.1"] = f"**Hypothesis (pre-registered):** {hypothesis}"

    auto["2.1"] = (
        f"Git remote: {code.git_remote}\n"
        f"HEAD SHA: `{code.git_sha}`\n"
        f"Working tree: {'dirty' if code.git_dirty else 'clean'}"
    )
    auto["2.2"] = (
        f"Dependency lock embedded in bundle (sha256 "
        f"`{code.requirements_lock_sha256}`). OS: {hardware.os}; "
        f"kernel: {hardware.kernel}."
    )
    auto["2.3"] = (
        f"CPU: {hardware.cpu_model} ({hardware.cpu_count} cores), "
        f"RAM: {hardware.ram_gb:.1f} GB, "
        f"GPUs: {len(hardware.gpus)}, "
        f"hostname: {hardware.hostname or 'n/a'}."
    )

    if llm_targets:
        auto["5.1"] = (
            "Model class: large language models. Targets ("
            f"{len(llm_targets)}):\n"
            + "\n".join(
                f"- **{t.role}**: `{t.model_name}` @ `{t.host}` "
                f"(digest `{t.model_digest[:16]}…`)"
                for t in llm_targets
            )
        )

    sampling_lines = []
    for r in runs[:5]:  # cap so the entry stays readable
        for c in r.llm_calls[:3]:
            s = c.sampling
            sampling_lines.append(
                f"- run `{r.run_id[:8]}` / call `{c.call_id[:8]}`: "
                f"temperature={s.temperature}, top_p={s.top_p}, "
                f"top_k={s.top_k}, seed={s.seed}, num_ctx={s.num_ctx}"
            )
    if sampling_lines:
        auto["5.5"] = (
            "Sampling parameters used at inference (sample of first calls):\n"
            + "\n".join(sampling_lines)
        )

    stats = next(
        (c for c in calculators if c.kind == "statistical_summary"), None
    )
    if stats is not None:
        out = stats.output
        auto["7.2"] = (
            f"Score statistics (n={out['n']}): mean={out['mean']:.3f}, "
            f"sd={out['sd']:.3f}, median={out['median']:.3f}, "
            f"95% CI=[{out['ci95_lower']:.3f}, {out['ci95_upper']:.3f}], "
            f"success_rate={out['success_rate']:.2f}, "
            f"best run=`{out['best_run_id'][:8]}`."
        )

    # Honest determinism note for §5.4 — LLM seeds are advisory.
    auto["5.4"] = (
        "Random seeds set per-call where supported (see ``runs[].llm_calls[]"
        ".sampling.seed``). NOTE: per vLLM and Ollama documentation, "
        "Ed25519-deterministic LLM sampling does NOT imply bit-identical "
        "output across hardware — even with seed and temperature=0, "
        "results may differ between GPUs / kernel versions / CUDA "
        "releases. Honest reproducibility for this campaign is "
        "**identical-distribution**, not bit-identity."
    )

    auto["8.4"] = (
        "Known limits: this campaign was driven by an autonomous "
        "LLM-orchestration system; outputs reflect the choices of that "
        "system at the captured code SHA. Re-running with a different "
        "orchestrator version or differently-configured judges may yield "
        "different conclusions."
    )

    return auto


# ── NeurIPS auto-fill helpers ────────────────────────


def _neurips_q6(runs: "list[RunRecord]") -> str:
    n_runs = len(runs)
    if n_runs == 0:
        return "No runs in this campaign."
    sampling_seen = {
        (c.sampling.temperature, c.sampling.top_p, c.sampling.seed)
        for r in runs
        for c in r.llm_calls
    }
    return (
        f"This campaign expanded {n_runs} run(s). Per-run parameters are in "
        f"``runs[].parameters``; sampling settings appear in "
        f"``runs[].llm_calls[].sampling``. {len(sampling_seen)} distinct "
        "sampling tuples were used."
    )


def _neurips_q7(calculators: "list[CalculatorResult]") -> str:
    stats = next((c for c in calculators if c.kind == "statistical_summary"), None)
    if stats is None:
        return _TODO
    out = stats.output
    return (
        f"Reported as 95% normal-approximation CI: mean={out['mean']:.3f}, "
        f"95% CI=[{out['ci95_lower']:.3f}, {out['ci95_upper']:.3f}], "
        f"sd={out['sd']:.3f}, n={out['n']}. "
        "(Calculator: ``ai_orchestrator.builtin.stats:v1``.)"
    )


def _neurips_q8(
    hardware: "HardwareFingerprint", calculators: "list[CalculatorResult]"
) -> str:
    compute = next((c for c in calculators if c.kind == "compute_resources"), None)
    summary = (
        f"Hardware: {hardware.cpu_model}, {hardware.cpu_count} cores, "
        f"{hardware.ram_gb:.1f} GB RAM, {len(hardware.gpus)} GPU(s), "
        f"{hardware.os} kernel {hardware.kernel}."
    )
    if compute is None:
        return summary
    out = compute.output
    return (
        summary
        + f"\n\nWall-clock total: {out['total_wall_clock_seconds']:.1f} s "
        f"across {out['n_runs']} runs. "
        f"LLM calls: {out['llm_call_count']} "
        f"(mean latency {out['mean_llm_latency_ms']:.1f} ms). "
        f"Code executions: {out['code_execution_count']}. "
        f"Response tokens (lower bound): {out['response_tokens_lower_bound']}."
    )


# ── per-target Markdown templates ────────────────────


def _model_card(t: "LlmTarget") -> str:
    return f"""# Model card: `{t.model_name}`

Schema: `Mitchell et al. 2019 — Model Cards for Model Reporting (arXiv:1810.03993)`

## Model details
- **Name**: `{t.model_name}`
- **Digest (Ollama sha256)**: `{t.model_digest}`
- **Size**: {t.model_size_bytes:,} bytes
- **Hosted at**: `{t.host}`
- **Role in this campaign**: {t.role}

## Intended use
- Used as the **{t.role}** in an LLM-orchestration campaign. See the
  bundle's ``runs[].llm_calls[]`` for exact prompts and sampling
  parameters used.

## Factors / Metrics / Eval data / Training data / Ethical considerations / Caveats
- {_TODO}
"""


def _prompt_corpus_datasheet(prompts: list[str]) -> str:
    n = len(prompts)
    lengths = [len(p) for p in prompts]
    avg_len = sum(lengths) / n if n else 0
    return f"""# Datasheet: prompt_corpus

Schema: `Gebru et al. 2018 — Datasheets for Datasets (arXiv:1803.09010)`

## Motivation
- The prompt corpus is the input data for an LLM-orchestration
  campaign. Each prompt is a single user-issued instruction whose
  output the orchestrator generates, judges, and (optionally) deploys.
- {_TODO}

## Composition
- {n} distinct rendered prompt(s).
- Average length: {avg_len:.0f} chars; min {min(lengths) if lengths else 0},
  max {max(lengths) if lengths else 0}.

## Collection process
- Prompts derive from the campaign template's ``prompt`` field, with
  per-combo ``{{param}}`` substitutions filled at run time. The exact
  rendered text appears in ``runs[].llm_calls[].rendered_messages``.

## Preprocessing / Uses / Distribution / Maintenance
- {_TODO}
"""
