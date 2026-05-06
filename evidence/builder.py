"""Evidence-bundle builder pipeline (Phase 1.2).

``build_bundle(campaign_id)`` reads everything Phase 1.1 already
captures, assembles an ``EvidenceBundle``, runs all registered
calculators, fills checklists, copies per-run artifacts into a
RO-Crate-shaped directory at ``campaigns/<campaign_id>/``, then signs
an in-toto Statement covering every artifact's sha256.

Pure repackaging — no new capture happens here. Phase 1.1's per-run
artifact tree under ``projects/<name>/runs/<run_id>/`` is the source.

Per-LLM-call telemetry (rendered messages, sampling, response tokens)
is captured at a coarse grain in Phase 1.2; finer per-call audit logs
land in a follow-up that touches the orchestration loop. The bundle
schema already accepts ``LlmCall`` records — the field stays empty
where the data isn't available yet rather than fabricating it.
"""
from __future__ import annotations

import json
import mimetypes
import os
import platform
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.campaign import Campaign
from core.evidence import (
    Artifact,
    CodeFingerprint,
    EvidenceBundle,
    HardwareFingerprint,
    LlmCall,
    LlmTarget,
    RunRecord,
    SamplingParams,
)
from core.llm_call_log import LLM_CALL_LOG, LlmCallRecord
from core.paths import LOG_DIR, PROJECTS_DIR, REPO_ROOT
from evidence import get_plugin_manager
from evidence.checklists import (
    build_datasheets,
    build_model_cards,
    build_neurips_responses,
    build_reforms_responses,
)
from evidence.rocrate import to_rocrate
from evidence.signing import (
    SigningKey,
    build_statement,
    canonical_json,
    make_subject,
    sha256_bytes,
    sha256_file,
    sign_statement,
)
from memory_pkg import load_campaigns

CAMPAIGNS_OUTPUT_DIR = REPO_ROOT / "campaigns"


# ── public entry point ──────────────────────────────


def build_bundle(
    campaign_id: str, *, signing_key: SigningKey | None = None
) -> EvidenceBundle:
    """Assemble + sign + write a citation-grade bundle for a campaign.

    The bundle (and all its constituent files) is materialised into
    ``campaigns/<campaign_id>/``. A signed DSSE envelope covers a
    sha256 manifest of every emitted file. Returns the bundle.

    ``signing_key`` defaults to ``SigningKey.load()`` reading from
    ``/etc/ai-orchestrator/signing/`` — the install script generates
    that on first run. Tests pass an in-memory key.
    """
    campaigns = load_campaigns()
    if campaign_id not in campaigns:
        raise KeyError(f"Unknown campaign_id: {campaign_id}")

    raw = campaigns[campaign_id]
    campaign = Campaign.model_validate(raw)
    crate_dir = CAMPAIGNS_OUTPUT_DIR / campaign_id

    builder = _BundleBuilder(
        campaign=campaign,
        crate_dir=crate_dir,
        signing_key=signing_key or SigningKey.load(),
    )
    return builder.build()


# ── internal builder ────────────────────────────────


class _BundleBuilder:
    """One-shot assembler. Single-campaign, single-build."""

    def __init__(
        self, *, campaign: Campaign, crate_dir: Path, signing_key: SigningKey
    ):
        self.campaign = campaign
        self.crate_dir = crate_dir
        self.signing_key = signing_key
        self._started = datetime.now(timezone.utc)

    # main pipeline -----------------------------------------------

    def build(self) -> EvidenceBundle:
        self._init_crate_dir()

        runs = self._build_run_records()
        code = self._build_code_fingerprint()
        hardware = self._build_hardware_fingerprint()
        targets = self._build_llm_targets()
        artifacts = self._copy_per_run_artifacts(runs)

        pm = get_plugin_manager()
        nested = pm.hook.compute_evidence(campaign=self.campaign, runs=runs)
        calculators = [r for sub in nested for r in sub]

        prompts = self._collect_rendered_prompts(runs)
        reforms = build_reforms_responses(
            hypothesis=self._hypothesis(),
            code=code,
            hardware=hardware,
            llm_targets=targets,
            runs=runs,
            calculators=calculators,
        )
        neurips = build_neurips_responses(
            code=code, hardware=hardware, runs=runs, calculators=calculators,
        )
        cards = build_model_cards(targets)
        datasheets = build_datasheets(prompts)

        bundle = EvidenceBundle(
            bundle_id=str(uuid.uuid4()),
            campaign_id=self.campaign.id,
            campaign_name=self.campaign.name,
            created_at=self._started,
            abstract=self._abstract(),
            hypothesis=self._hypothesis(),
            code=code,
            hardware=hardware,
            llm_targets=targets,
            runs=runs,
            model_cards=cards,
            datasheets=datasheets,
            reforms_responses=reforms,
            neurips_responses=neurips,
            calculators=calculators,
            artifacts=artifacts,
        )

        self._write_checklist_files(bundle)
        self._write_card_and_datasheet_files(bundle)
        # ``attestations`` stays empty in evidence.json by design — the
        # canonical signed attestation lives at ``manifest.json.dsse``
        # (covering evidence.json itself + every other crate file). If
        # we re-wrote evidence.json with the envelope attached, the
        # manifest's expected sha256 would no longer match. Keeping the
        # signed envelope in a sibling file is also how RO-Crate /
        # in-toto consumers expect to find it.
        self._write_evidence_json(bundle)
        self._write_rocrate(bundle)
        self._write_readme(bundle)
        self._write_public_key()

        statement = self._build_statement(bundle)
        envelope = sign_statement(statement, self.signing_key)
        self._write_manifest_files(statement, envelope)

        return bundle

    # crate-dir scaffolding --------------------------------------

    def _init_crate_dir(self) -> None:
        self.crate_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("artifacts", "checklists", "model_cards", "datasheets"):
            (self.crate_dir / sub).mkdir(exist_ok=True)

    # run-record assembly ----------------------------------------

    @staticmethod
    def _record_to_llm_call(rec: LlmCallRecord) -> LlmCall:
        """Phase J Scope α — best-effort mapping from runtime LlmCallRecord to bundle LlmCall.

        Placeholders used (to be replaced in Scope β):
        - ``call_id``: generated UUID (runtime does not yet assign a stable call ID).
        - ``role``: hardcoded ``"generator"`` (followup will infer from rendered_messages).
        - ``target.host``, ``target.model_digest``, ``target.model_size_bytes``:
          stable placeholder strings/ints (runtime captures model name only).
        - ``response_text``: empty string (state hook captures token count, not text body).
        - ``started_at``: approximated as ``now() − duration_ms`` (Prefect task
          start_time not yet threaded through to LlmCallRecord).

        Scope β work tracked at:
          docs/superpowers/followups/phase-j-beta-llm-call-fidelity.md
        """
        # Build SamplingParams — temperature is required; default 0.0 if absent.
        # SamplingParams.extra="allow" so unknown backend-specific keys are accepted
        # automatically; no try/except needed.
        sampling = SamplingParams(
            temperature=float(rec.sampling.get("temperature", 0.0)),
            **{k: v for k, v in rec.sampling.items() if k != "temperature"},
        )

        # Phase J α placeholder — LlmCallRecord carries model name only; host,
        # digest, and size require Scope β instrumentation in state_hooks.py.
        target = LlmTarget(
            role="generator",  # placeholder — followup will infer from message roles
            host="ollama-runtime-unknown",  # placeholder — Scope β reads from llm.ollama config
            model_name=rec.model,
            model_digest="sha256-placeholder-scope-beta",  # placeholder
            model_size_bytes=0,  # placeholder
        )

        # Approximate started_at: subtract duration from now().  Prefect task
        # start_time is available in the state hook but not yet propagated here.
        started_at = datetime.now(timezone.utc) - timedelta(milliseconds=rec.duration_ms)

        return LlmCall(
            call_id=str(uuid.uuid4()),  # Phase J α: no stable call_id yet; Scope β generates at task-start hook
            role="generator",  # Phase J α placeholder — Scope β infers from rendered_messages roles
            target=target,
            rendered_messages=rec.rendered_messages,
            sampling=sampling,
            response_text="",  # Phase J α placeholder — Scope β captures via state hook result body
            response_tokens=rec.response_tokens,
            latency_ms=rec.duration_ms,
            started_at=started_at,
        )

    def _build_run_records(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        for run in self.campaign.runs:
            project = self.campaign.template.project_name
            project_dir = Path(PROJECTS_DIR) / project / "runs" / run.run_id
            metrics: dict[str, float] = {}
            if run.score is not None:
                metrics["score"] = float(run.score)

            execution = self._read_execution(project_dir)
            code_executions = []
            if execution is not None:
                from core.evidence import CodeExecution

                code_executions.append(
                    CodeExecution(
                        language=execution.get("language", "unknown"),
                        code_sha256=execution.get("code_sha256", "0" * 64),
                        stdout=execution.get("stdout", ""),
                        stderr=execution.get("stderr", ""),
                        return_code=int(execution.get("returncode", 0)),
                        duration_ms=int(execution.get("duration_ms", 0)),
                    )
                )

            timestamps = self._read_timestamps(project_dir)
            records.append(
                RunRecord(
                    run_id=run.run_id,
                    parameters=dict(run.params),
                    llm_calls=[self._record_to_llm_call(rec) for rec in LLM_CALL_LOG.drain(run.run_id)],
                    code_executions=code_executions,
                    metrics=metrics,
                    status=_run_status_to_record(run.status),
                    started_at=timestamps[0],
                    finished_at=timestamps[1],
                )
            )
        return records

    def _read_execution(self, project_dir: Path) -> dict | None:
        exec_path = project_dir / "execution.json"
        if not exec_path.exists():
            return None
        try:
            return json.loads(exec_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _read_timestamps(self, project_dir: Path) -> tuple[datetime, datetime]:
        """Best-effort start/finish from the project dir's mtime range.

        If the dir doesn't exist (run was aborted/failed before any artifact),
        fall back to the campaign's created_at / updated_at.
        """
        if project_dir.exists():
            entries = list(project_dir.glob("*"))
            if entries:
                start = min(p.stat().st_mtime for p in entries)
                end = max(p.stat().st_mtime for p in entries)
                return (
                    datetime.fromtimestamp(start, tz=timezone.utc),
                    datetime.fromtimestamp(end, tz=timezone.utc),
                )
        created = datetime.fromisoformat(self.campaign.created_at)
        updated = datetime.fromisoformat(self.campaign.updated_at)
        return _ensure_aware(created), _ensure_aware(updated)

    # fingerprints -----------------------------------------------

    def _build_code_fingerprint(self) -> CodeFingerprint:
        git_remote = _git("config", "--get", "remote.origin.url") or "(unknown)"
        git_sha = _git("rev-parse", "HEAD") or "(unknown)"
        git_dirty_out = _git("status", "--porcelain")
        git_dirty = bool((git_dirty_out or "").strip())

        lock_path = REPO_ROOT / "requirements.txt"
        lock_text = lock_path.read_text() if lock_path.exists() else ""
        lock_sha = sha256_bytes(lock_text.encode())

        return CodeFingerprint(
            git_remote=git_remote,
            git_sha=git_sha,
            git_dirty=git_dirty,
            requirements_lock=lock_text,
            requirements_lock_sha256=lock_sha,
        )

    def _build_hardware_fingerprint(self) -> HardwareFingerprint:
        try:
            mem_kb = 0
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_kb = int(line.split()[1])
                        break
            ram_gb = mem_kb / (1024 * 1024)
        except (OSError, ValueError):
            ram_gb = 0.0

        cpu_model = "unknown"
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        _, _, cpu_model = line.partition(":")
                        cpu_model = cpu_model.strip()
                        break
        except OSError:
            pass

        return HardwareFingerprint(
            cpu_model=cpu_model,
            cpu_count=os.cpu_count() or 0,
            ram_gb=ram_gb,
            os=platform.platform(aliased=True, terse=True),
            kernel=platform.release(),
            hostname=socket.gethostname(),
        )

    def _build_llm_targets(self) -> list[LlmTarget]:
        """Targets are inferred from the campaign template.

        We can't query Ollama for real model digests / sizes here without
        network calls; for Phase 1.2 we record the model NAMES from the
        template with placeholder digests. The hardware/LLM-detail
        capture loop (1.2.x) will resolve real digests by calling
        Ollama at bundle-build time.
        """
        tpl = self.campaign.template
        targets: list[LlmTarget] = []
        seen: set[tuple[str, str]] = set()

        def add(role: str, model: str | None) -> None:
            if not model:
                return
            key = (role, model)
            if key in seen:
                return
            seen.add(key)
            targets.append(
                LlmTarget(
                    role=role,
                    host="(unresolved)",
                    model_name=model,
                    model_digest="(unresolved)",
                    model_size_bytes=0,
                )
            )

        add("planner", tpl.planner_model)
        add("judge", tpl.judge_model)
        for m in tpl.generator_models:
            add("generator", m)
        add("optimizer", tpl.optimizer_model)
        add("troubleshooter", tpl.troubleshooter_model)
        return targets

    # artifact copying -------------------------------------------

    def _copy_per_run_artifacts(self, runs: list[RunRecord]) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for r in runs:
            project = self.campaign.template.project_name
            src_dir = Path(PROJECTS_DIR) / project / "runs" / r.run_id
            dst_dir = self.crate_dir / "artifacts" / r.run_id
            dst_dir.mkdir(parents=True, exist_ok=True)

            if src_dir.exists():
                for src in src_dir.rglob("*"):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(src_dir)
                    dst = dst_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    artifacts.append(_artifact_for(self.crate_dir, dst, role=_role_of(rel.name)))

            log_src = Path(LOG_DIR) / f"{r.run_id}.log"
            if log_src.exists():
                dst = dst_dir / "log.txt"
                shutil.copy2(log_src, dst)
                artifacts.append(_artifact_for(self.crate_dir, dst, role="log"))

        return artifacts

    # checklist + card writers -----------------------------------

    def _write_checklist_files(self, bundle: EvidenceBundle) -> None:
        reforms = "\n\n".join(
            f"## {k}\n\n{v}" for k, v in bundle.reforms_responses.items()
        )
        neurips = "\n\n".join(
            f"## {k}\n\n{v}" for k, v in bundle.neurips_responses.items()
        )
        (self.crate_dir / "checklists" / "reforms.md").write_text(
            "# REFORMS responses\n\nKapoor et al., *Sci Adv* 2024.\n\n" + reforms
        )
        (self.crate_dir / "checklists" / "neurips.md").write_text(
            "# NeurIPS Paper Checklist (Q4-Q8)\n\n" + neurips
        )
        bundle.artifacts.extend(
            [
                _artifact_for(
                    self.crate_dir,
                    self.crate_dir / "checklists" / "reforms.md",
                    role="checklist",
                ),
                _artifact_for(
                    self.crate_dir,
                    self.crate_dir / "checklists" / "neurips.md",
                    role="checklist",
                ),
            ]
        )

    def _write_card_and_datasheet_files(self, bundle: EvidenceBundle) -> None:
        for model_name, body in bundle.model_cards.items():
            safe = _safe_name(model_name)
            path = self.crate_dir / "model_cards" / f"{safe}.md"
            path.write_text(body)
            bundle.artifacts.append(_artifact_for(self.crate_dir, path, role="model_card"))
        for ds_name, body in bundle.datasheets.items():
            safe = _safe_name(ds_name)
            path = self.crate_dir / "datasheets" / f"{safe}.md"
            path.write_text(body)
            bundle.artifacts.append(_artifact_for(self.crate_dir, path, role="datasheet"))

    # JSON writers ------------------------------------------------

    def _write_evidence_json(self, bundle: EvidenceBundle) -> None:
        path = self.crate_dir / "evidence.json"
        path.write_bytes(canonical_json(bundle.model_dump(by_alias=True, mode="json")))

    def _write_rocrate(self, bundle: EvidenceBundle) -> None:
        path = self.crate_dir / "ro-crate-metadata.json"
        path.write_text(json.dumps(to_rocrate(bundle), indent=2))

    def _write_readme(self, bundle: EvidenceBundle) -> None:
        readme = (self.crate_dir / "README.md")
        # HF-style YAML front-matter so this indexes cleanly on archives.
        readme.write_text(
            "---\n"
            f"campaign_id: {bundle.campaign_id}\n"
            f"campaign_name: {bundle.campaign_name}\n"
            f"bundle_id: {bundle.bundle_id}\n"
            f"created_at: {bundle.created_at.isoformat()}\n"
            f"schema_version: {bundle.schema_version}\n"
            "license: Apache-2.0\n"
            "tags:\n  - ai-orchestrator\n  - evidence-bundle\n  - ro-crate\n  - wrroc\n"
            "---\n\n"
            f"# Evidence bundle: {bundle.campaign_name}\n\n"
            f"**Hypothesis:** {bundle.hypothesis}\n\n"
            f"## Abstract\n\n{bundle.abstract}\n\n"
            "## Verification\n\n"
            "Verify this bundle's signature with the standalone verifier:\n\n"
            "    python -m evidence.verify --crate-dir .\n\n"
            "Schema: `EvidenceBundle v1.0.0`. RO-Crate profile: "
            "`Provenance Run Crate (WRROC)`. Signing: Ed25519 in a DSSE "
            "envelope (in-toto Statement v1, SLSA Provenance v1.0 predicate).\n"
        )

    def _write_public_key(self) -> None:
        (self.crate_dir / "public.key").write_text(self.signing_key.public_b64() + "\n")

    # statement / signing ----------------------------------------

    def _build_statement(self, bundle: EvidenceBundle):
        # Subject = every file currently in the crate, hashed.
        subjects = []
        for path in sorted(self.crate_dir.rglob("*")):
            if not path.is_file():
                continue
            # Don't include the manifest files themselves in the manifest.
            if path.name in {"manifest.json", "manifest.json.dsse"}:
                continue
            rel = path.relative_to(self.crate_dir).as_posix()
            subjects.append(make_subject(rel, sha256_file(path)))

        external_params = {
            "campaign_id": self.campaign.id,
            "campaign_name": self.campaign.name,
            "params_grid": self.campaign.params,
            "max_runs": self.campaign.max_runs,
            "template": self.campaign.template.model_dump(),
        }
        internal_params = {
            "schema_version": bundle.schema_version,
            "calculator_count": len(bundle.calculators),
        }

        return build_statement(
            subjects=subjects,
            builder_id="https://ai-orchestrator.io/builder/v0.1",
            builder_version={"ai-orchestrator": "0.1.2"},
            invocation_id=bundle.bundle_id,
            started=bundle.created_at,
            finished=datetime.now(timezone.utc),
            external_parameters=external_params,
            internal_parameters=internal_params,
            resolved_dependencies=[],
        )

    def _write_manifest_files(self, statement, envelope) -> None:
        (self.crate_dir / "manifest.json").write_bytes(
            canonical_json(statement.model_dump(by_alias=True, mode="json"))
        )
        (self.crate_dir / "manifest.json.dsse").write_bytes(
            canonical_json(envelope.model_dump(by_alias=True, mode="json"))
        )

    # narrative helpers ------------------------------------------

    def _hypothesis(self) -> str:
        # Phase 1.1's Campaign doesn't yet require hypothesis (Commit 7
        # adds that). Until then, take a description fallback so this
        # builder is usable on existing campaigns.
        return getattr(self.campaign, "hypothesis", None) or (
            self.campaign.description or "(no hypothesis stated)"
        )

    def _abstract(self) -> str:
        n = len(self.campaign.runs)
        return (
            f"Campaign '{self.campaign.name}' executed {n} run(s) under the "
            f"orchestrator. Status: {self.campaign.status}. See "
            "``hypothesis`` for the pre-registered question and "
            "``calculators[]`` for the statistical conclusion."
        )

    def _collect_rendered_prompts(self, runs: list[RunRecord]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for r in runs:
            for c in r.llm_calls:
                rendered = "\n".join(
                    m.get("content", "") for m in c.rendered_messages if isinstance(m, dict)
                )
                if rendered and rendered not in seen:
                    seen.add(rendered)
                    out.append(rendered)
        return out


# ── small helpers ───────────────────────────────────


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_status_to_record(s: str) -> str:
    return {
        "completed": "success",
        "failed": "fail",
        "aborted": "aborted",
        "queued": "paused",
    }.get(s, "fail")


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:64]


def _artifact_for(crate_dir: Path, path: Path, *, role: str) -> Artifact:
    rel = path.relative_to(crate_dir).as_posix()
    mime, _ = mimetypes.guess_type(path.name)
    return Artifact(
        path=rel,
        sha256=sha256_file(path),
        content_type=mime or "application/octet-stream",
        size_bytes=path.stat().st_size,
        role=role,
    )


def _role_of(filename: str) -> str:
    if filename.endswith(".log") or filename == "log.txt":
        return "log"
    if filename in {"plan.json", "files.json", "execution.json", "judge.json"}:
        return "result"
    if filename in {"environment.json", "score.txt"}:
        return "config"
    if filename == "prompt.txt":
        return "config"
    return "code"


def _build_statement_subjects(crate_dir: Path) -> list:
    """Public helper used by tests to recompute the manifest subject set."""
    out = []
    for path in sorted(crate_dir.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "manifest.json.dsse"}:
            continue
        rel = path.relative_to(crate_dir).as_posix()
        out.append(make_subject(rel, sha256_file(path)))
    return out
