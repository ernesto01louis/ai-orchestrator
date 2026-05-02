"""Notification senders — Gotify (primary) with ntfy fallback.

All public functions tolerate the orchestrator being run with notifications
disabled in config.json: they early-return without contacting any service.
"""
from __future__ import annotations

import requests

from core.config import (
    GOTIFY_PRIORITY,
    GOTIFY_TOKEN,
    GOTIFY_URL,
    NOTIFY_ENABLED,
    NOTIFY_ON_FAILURE,
    NOTIFY_ON_SUCCESS,
    NOTIFY_SERVICE,
    NTFY_PRIORITY,
    NTFY_TOPIC,
    NTFY_URL,
    ORCHESTRATOR_URL,
)


def send_notification(title, message, priority=None, tags=None, actions=None, click_url=None):
    """Send via Gotify (primary) with ntfy fallback.

    Gotify gets Markdown links; ntfy gets action buttons.
    """
    if not NOTIFY_ENABLED:
        return

    md_links = ""
    if actions:
        parts = [f"[{a['label']}]({a['url']})" for a in actions if a.get("type") == "view" and a.get("url")]
        if parts:
            md_links = "\n\n---\n" + "  ·  ".join(parts)
    elif click_url:
        md_links = f"\n\n---\n[Open]({click_url})"

    try:
        if NOTIFY_SERVICE == "gotify":
            ok = _send_gotify(title, message + md_links, priority)
            if not ok:
                _send_ntfy(title, message, priority, tags, actions, click_url)
        elif NOTIFY_SERVICE == "ntfy":
            ok = _send_ntfy(title, message, priority, tags, actions, click_url)
            if not ok:
                _send_gotify(title, message + md_links, priority)
        else:
            _send_gotify(title, message + md_links, priority)
    except Exception as e:
        print(f"Notification failed (non-fatal): {e}")


def _send_gotify(title, message, priority=None):
    """Send via Gotify with Markdown support."""
    if not GOTIFY_URL or not GOTIFY_TOKEN:
        return False
    payload = {
        "title": title,
        "message": message,
        "priority": priority if priority is not None else GOTIFY_PRIORITY,
        "extras": {"client::display": {"contentType": "text/markdown"}},
    }
    try:
        r = requests.post(
            f"{GOTIFY_URL}/message",
            json=payload,
            headers={"X-Gotify-Key": GOTIFY_TOKEN},
            timeout=10,
        )
        if r.status_code in (200, 201):
            return True
        print(f"Gotify returned {r.status_code}: {r.text[:200]}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"Gotify failed: {e}")
        return False


def _send_ntfy(title, message, priority=None, tags=None, actions=None, click_url=None):
    """Send via ntfy (fallback)."""
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {"Title": title, "Priority": priority or NTFY_PRIORITY}
    if tags:
        headers["Tags"] = ",".join(tags) if isinstance(tags, list) else tags
    if click_url:
        headers["Click"] = click_url
    if actions:
        parts = []
        for a in actions:
            if a.get("type") == "view":
                parts.append(f"view, {a['label']}, {a['url']}")
            elif a.get("type") == "http":
                p = f"http, {a['label']}, {a['url']}"
                if a.get("method"):
                    p += f", method={a['method']}"
                if a.get("body"):
                    p += f", body={a['body']}"
                parts.append(p)
        if parts:
            headers["Actions"] = "; ".join(parts)
    try:
        r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        return r.status_code in (200, 204)
    except requests.exceptions.RequestException as e:
        print(f"ntfy failed: {e}")
        return False


def notify_run_complete(run_id, project_name, score, success, language,
                        deploy_path, target, winning_model, troubleshoot_attempts,
                        elapsed_seconds=0):
    """Run-complete notification with API links."""
    if not NOTIFY_ENABLED:
        return
    if success and not NOTIFY_ON_SUCCESS:
        return
    if not success and not NOTIFY_ON_FAILURE:
        return

    status = "PASS" if success else "FAIL"
    tags = ["white_check_mark", "robot"] if success else ["x", "warning"]
    mins = elapsed_seconds // 60
    secs = elapsed_seconds % 60
    time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"

    title = f"[{status}] {project_name} — {score}/10"
    lines = [
        f"**Project:** {project_name}",
        f"**Score:** {score}/10 ({language})",
        f"**Model:** {winning_model}",
        f"**Target:** {target}",
        f"**Time:** {time_str}",
    ]
    if troubleshoot_attempts > 0:
        lines.append(f"**Troubleshoot:** {troubleshoot_attempts}x")
    if deploy_path:
        lines.append(f"**Deployed:** `{deploy_path}`")
    if not success:
        lines.append("**Status:** execution failed")
    message = "\n".join(lines)

    actions = [
        {"type": "view", "label": "Status", "url": f"{ORCHESTRATOR_URL}/status/{run_id}"},
        {"type": "view", "label": "Files", "url": f"{ORCHESTRATOR_URL}/files/{run_id}"},
        {"type": "view", "label": "Briefing", "url": f"{ORCHESTRATOR_URL}/briefing"},
        {"type": "view", "label": "Graph", "url": f"{ORCHESTRATOR_URL}/ui"},
    ]
    send_notification(title, message, priority=5 if success else 8, tags=tags,
                      actions=actions, click_url=f"{ORCHESTRATOR_URL}/status/{run_id}")


def notify_run_started(run_id, project_name, target, prompt):
    """Start notification."""
    if not NOTIFY_ENABLED:
        return
    title = f"▶ Started: {project_name}"
    message = f"**Target:** {target}\n**Prompt:** {prompt[:120]}"
    actions = [
        {"type": "view", "label": "Track", "url": f"{ORCHESTRATOR_URL}/status/{run_id}"},
        {"type": "view", "label": "Runs", "url": f"{ORCHESTRATOR_URL}/runs"},
    ]
    send_notification(title, message, priority=3, tags=["arrow_forward"],
                      actions=actions, click_url=f"{ORCHESTRATOR_URL}/status/{run_id}")


def send_quick_actions_notification():
    """Remote control with all useful API links."""
    if not NOTIFY_ENABLED:
        return
    title = "🎛 Orchestrator Remote"
    message = (
        f"[📊 Briefing]({ORCHESTRATOR_URL}/briefing)  ·  "
        f"[🏥 Health]({ORCHESTRATOR_URL}/health)  ·  "
        f"[📋 Runs]({ORCHESTRATOR_URL}/runs)\n\n"
        f"[📈 Models]({ORCHESTRATOR_URL}/model-stats)  ·  "
        f"[🧠 Memory]({ORCHESTRATOR_URL}/memory)  ·  "
        f"[🌐 Graph]({ORCHESTRATOR_URL}/ui)\n\n"
        f"[📦 pi-1]({ORCHESTRATOR_URL}/deployed/pi-1)  ·  "
        f"[📦 pi-2]({ORCHESTRATOR_URL}/deployed/pi-2)  ·  "
        f"[📦 Rak]({ORCHESTRATOR_URL}/deployed/Rak)"
    )
    send_notification(title, message, priority=5)


def send_api_cheatsheet_notification(run_id=None, project_name=None, target=None):
    """Curl cheatsheet notification."""
    if not NOTIFY_ENABLED:
        return
    lines = [
        "```",
        f"curl {ORCHESTRATOR_URL}/health",
        f"curl {ORCHESTRATOR_URL}/briefing",
        f"curl {ORCHESTRATOR_URL}/runs",
        f"curl {ORCHESTRATOR_URL}/model-stats",
        "```",
    ]
    if run_id:
        lines.extend(["```", f"curl {ORCHESTRATOR_URL}/status/{run_id}", f"curl {ORCHESTRATOR_URL}/files/{run_id}", "```"])
    send_notification("📖 API Cheatsheet", "\n".join(lines), priority=3)
