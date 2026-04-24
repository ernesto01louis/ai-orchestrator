"""
Enhanced Notification System for AI Orchestrator
=================================================

Drop-in replacement for the notification functions in app.py.
Adds actionable buttons to ntfy notifications so you can check status,
view results, re-run projects, and more directly from your phone.

ntfy supports action buttons natively via the "Actions" header:
  - "view"  → opens a URL in the browser
  - "http"  → fires an HTTP request directly from the notification

SETUP:
  1. Replace the notification functions in app.py with these
  2. Add ORCHESTRATOR_PUBLIC_URL to your config.json:
     "notifications": {
       ...existing config...,
       "orchestrator_url": "http://100.84.63.13:8000"  ← Tailscale IP so it works remotely
     }
  3. That's it — ntfy handles the rest

The orchestrator_url should be reachable from your phone.
If you're on Tailscale, use the Tailscale IP of the orchestrator LXC.
If not, use your local IP (only works on same network).

ALTERNATIVE: Telegram Bot
  If you want richer interactions (inline keyboards, conversation),
  see the Telegram section at the bottom of this file.
"""

import requests
import json


# ------------------------------------------------
# CONFIG (pulled from your existing CONFIG dict)
# ------------------------------------------------

# Add this to your config.json under "notifications":
#   "orchestrator_url": "http://100.84.63.13:8000"
# This is the URL your phone can reach (Tailscale IP recommended)

def get_orchestrator_url(config):
    """Get the publicly-reachable orchestrator URL for action buttons."""
    return config.get("notifications", {}).get(
        "orchestrator_url",
        "http://192.168.2.216:8000"  # fallback to local
    )


# ------------------------------------------------
# ENHANCED NTFY WITH ACTION BUTTONS
# ------------------------------------------------

def _send_ntfy_enhanced(title, message, priority=None, tags=None,
                         actions=None, click_url=None, ntfy_url=None,
                         ntfy_topic=None, ntfy_priority=None):
    """
    Send via ntfy with optional action buttons.

    actions: list of action dicts, each with:
      - "type": "view" or "http"
      - "label": button text
      - "url": target URL
      - "method": (for http) "GET" or "POST"
      - "body": (for http) request body string
      - "headers": (for http) dict of headers
      - "clear": (optional) True to dismiss notification on tap

    click_url: URL opened when tapping the notification body itself
    """
    url = f"{ntfy_url}/{ntfy_topic}"

    headers = {
        "Title": title,
        "Priority": priority or ntfy_priority or "default",
    }

    if tags:
        headers["Tags"] = ",".join(tags) if isinstance(tags, list) else tags

    if click_url:
        headers["Click"] = click_url

    # Build Actions header
    # Format: "action_type, label, url[, param=value...]"
    # Multiple actions separated by "; "
    if actions:
        action_parts = []
        for a in actions:
            atype = a.get("type", "view")
            label = a.get("label", "Open")
            aurl = a.get("url", "")

            if atype == "view":
                part = f"view, {label}, {aurl}"
                if a.get("clear"):
                    part += ", clear=true"
                action_parts.append(part)

            elif atype == "http":
                part = f"http, {label}, {aurl}"
                if a.get("method"):
                    part += f", method={a['method']}"
                if a.get("body"):
                    part += f", body={a['body']}"
                if a.get("headers"):
                    for hk, hv in a["headers"].items():
                        part += f", headers.{hk}={hv}"
                if a.get("clear"):
                    part += ", clear=true"
                action_parts.append(part)

        if action_parts:
            headers["Actions"] = "; ".join(action_parts)

    try:
        r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        if r.status_code not in (200, 204):
            print(f"ntfy returned status {r.status_code}: {r.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"ntfy send failed: {e}")


# ------------------------------------------------
# NOTIFICATION BUILDERS WITH ACTIONS
# ------------------------------------------------

def notify_run_complete_enhanced(run_id, project_name, score, success, language,
                                  deploy_path, target, winning_model,
                                  troubleshoot_attempts, elapsed_seconds=0,
                                  config=None):
    """
    Send a run-complete notification with actionable buttons.

    Buttons added:
      - "📊 Status"     → GET /status/{run_id} (opens in browser)
      - "📁 Files"      → GET /files/{run_id}  (opens in browser)
      - "▶ Re-run"      → POST /run-deployed    (fires HTTP request)
    """
    notify_cfg = config.get("notifications", {}) if config else {}
    if not notify_cfg.get("enabled", False):
        return

    orch_url = get_orchestrator_url(config or {})
    ntfy_url = notify_cfg.get("ntfy_url", "http://100.84.63.13:8090")
    ntfy_topic = notify_cfg.get("ntfy_topic", "ai-orchestrator")
    ntfy_priority = notify_cfg.get("ntfy_priority", "default")

    # respect on_success / on_failure
    if success and not notify_cfg.get("on_success", True):
        return
    if not success and not notify_cfg.get("on_failure", True):
        return

    status = "PASS" if success else "FAIL"
    emoji_tags = ["white_check_mark", "robot"] if success else ["x", "warning"]

    mins = elapsed_seconds // 60
    secs = elapsed_seconds % 60
    time_str = f"{mins}m{secs:02d}s" if mins > 0 else f"{secs}s"

    title = f"[{status}] {project_name} — {score}/10"

    lines = [
        f"Project: {project_name}",
        f"Score: {score}/10 ({language})",
        f"Model: {winning_model}",
        f"Target: {target}",
        f"Time: {time_str}",
    ]
    if troubleshoot_attempts > 0:
        lines.append(f"Troubleshoot: {troubleshoot_attempts}×")
    if deploy_path:
        lines.append(f"Deployed: {deploy_path}")
    if not success:
        lines.append("⚠ Execution failed")

    message = "\n".join(lines)

    # Build action buttons
    actions = [
        {
            "type": "view",
            "label": "📊 Status",
            "url": f"{orch_url}/status/{run_id}",
        },
        {
            "type": "view",
            "label": "📁 Files",
            "url": f"{orch_url}/files/{run_id}",
        },
    ]

    # Add re-run button only for successful deploys
    if success and deploy_path:
        actions.append({
            "type": "http",
            "label": "▶ Re-run",
            "url": f"{orch_url}/run-deployed",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "project_name": project_name,
                "target": target,
            }),
        })

    priority = "default" if success else "high"

    # Click on notification body → open full status
    click_url = f"{orch_url}/status/{run_id}"

    _send_ntfy_enhanced(
        title=title,
        message=message,
        priority=priority,
        tags=emoji_tags,
        actions=actions,
        click_url=click_url,
        ntfy_url=ntfy_url,
        ntfy_topic=ntfy_topic,
        ntfy_priority=ntfy_priority,
    )


def notify_run_started(run_id, project_name, target, prompt,
                        config=None):
    """
    Notify when a run starts. Includes a button to check live status.
    """
    notify_cfg = config.get("notifications", {}) if config else {}
    if not notify_cfg.get("enabled", False):
        return

    orch_url = get_orchestrator_url(config or {})
    ntfy_url = notify_cfg.get("ntfy_url", "http://100.84.63.13:8090")
    ntfy_topic = notify_cfg.get("ntfy_topic", "ai-orchestrator")

    title = f"▶ Started: {project_name}"
    message = f"Target: {target}\nPrompt: {prompt[:120]}"

    actions = [
        {
            "type": "view",
            "label": "📊 Track Progress",
            "url": f"{orch_url}/status/{run_id}",
        },
        {
            "type": "view",
            "label": "📋 All Runs",
            "url": f"{orch_url}/runs",
        },
    ]

    _send_ntfy_enhanced(
        title=title,
        message=message,
        priority="low",
        tags=["arrow_forward", "gear"],
        actions=actions,
        click_url=f"{orch_url}/status/{run_id}",
        ntfy_url=ntfy_url,
        ntfy_topic=ntfy_topic,
    )


def send_daily_digest_notification(config=None):
    """
    Send a daily digest notification with quick-access buttons.
    Call this from a cron job or scheduler.
    """
    notify_cfg = config.get("notifications", {}) if config else {}
    if not notify_cfg.get("enabled", False):
        return

    orch_url = get_orchestrator_url(config or {})
    ntfy_url = notify_cfg.get("ntfy_url", "http://100.84.63.13:8090")
    ntfy_topic = notify_cfg.get("ntfy_topic", "ai-orchestrator")

    title = "📊 Orchestrator Daily Digest"
    message = "Tap to view today's briefing and system status."

    actions = [
        {"type": "view", "label": "📋 Briefing", "url": f"{orch_url}/briefing"},
        {"type": "view", "label": "🏥 Health", "url": f"{orch_url}/health"},
        {"type": "view", "label": "📈 Models", "url": f"{orch_url}/model-stats"},
    ]

    _send_ntfy_enhanced(
        title=title,
        message=message,
        priority="low",
        tags=["bar_chart", "calendar"],
        actions=actions,
        click_url=f"{orch_url}/briefing",
        ntfy_url=ntfy_url,
        ntfy_topic=ntfy_topic,
    )


# ------------------------------------------------
# QUICK-ACTION NOTIFICATION (manual trigger)
# ------------------------------------------------

def send_quick_actions_notification(config=None):
    """
    Send a notification with common API shortcuts.
    Useful as a "remote control" when you're away from your desk.

    Call via: POST /notifications/quick-actions
    """
    notify_cfg = config.get("notifications", {}) if config else {}
    if not notify_cfg.get("enabled", False):
        return

    orch_url = get_orchestrator_url(config or {})
    ntfy_url = notify_cfg.get("ntfy_url", "http://100.84.63.13:8090")
    ntfy_topic = notify_cfg.get("ntfy_topic", "ai-orchestrator")

    title = "🎛 Orchestrator Remote Control"
    message = (
        "Quick actions for your orchestrator.\n"
        "Tap a button below to check status or trigger actions."
    )

    actions = [
        {"type": "view", "label": "📊 Briefing", "url": f"{orch_url}/briefing"},
        {"type": "view", "label": "🏥 Health", "url": f"{orch_url}/health"},
        {"type": "view", "label": "📋 Runs", "url": f"{orch_url}/runs"},
    ]

    _send_ntfy_enhanced(
        title=title,
        message=message,
        priority="default",
        tags=["joystick", "satellite"],
        actions=actions,
        click_url=f"{orch_url}/briefing",
        ntfy_url=ntfy_url,
        ntfy_topic=ntfy_topic,
    )


# ------------------------------------------------
# RECOMMENDED API REQUESTS (for ntfy message body)
# ------------------------------------------------

def build_api_cheatsheet(orch_url, run_id=None, project_name=None, target=None):
    """
    Build a plain-text cheatsheet of useful curl commands.
    Can be included in notification bodies or sent as a separate notification.
    """
    lines = [
        "── ORCHESTRATOR API CHEATSHEET ──",
        "",
        "# System status",
        f"curl {orch_url}/health",
        f"curl {orch_url}/briefing",
        f"curl {orch_url}/models/loaded",
        "",
        "# Recent runs",
        f"curl {orch_url}/runs",
    ]

    if run_id:
        lines.extend([
            "",
            f"# This run ({run_id[:8]})",
            f"curl {orch_url}/status/{run_id}",
            f"curl {orch_url}/result/{run_id}",
            f"curl {orch_url}/files/{run_id}",
        ])

    if project_name and target:
        lines.extend([
            "",
            f"# Re-run {project_name}",
            f"curl -X POST {orch_url}/run-deployed \\",
            f'  -H "Content-Type: application/json" \\',
            f'  -d \'{{"project_name":"{project_name}","target":"{target}"}}\'',
        ])

    if target:
        lines.extend([
            "",
            f"# Deployed projects on {target}",
            f"curl {orch_url}/deployed/{target}",
            f"curl {orch_url}/environment/{target}",
        ])

    lines.extend([
        "",
        "# Memory & stats",
        f"curl {orch_url}/memory",
        f"curl {orch_url}/memory/negative",
        f"curl {orch_url}/model-stats",
        f"curl {orch_url}/sessions",
    ])

    return "\n".join(lines)


def send_api_cheatsheet_notification(run_id=None, project_name=None,
                                       target=None, config=None):
    """
    Send a notification containing useful API requests.
    The message body contains curl commands you can copy.
    """
    notify_cfg = config.get("notifications", {}) if config else {}
    if not notify_cfg.get("enabled", False):
        return

    orch_url = get_orchestrator_url(config or {})
    ntfy_url = notify_cfg.get("ntfy_url", "http://100.84.63.13:8090")
    ntfy_topic = notify_cfg.get("ntfy_topic", "ai-orchestrator")

    cheatsheet = build_api_cheatsheet(orch_url, run_id, project_name, target)

    title = "📖 API Quick Reference"

    actions = [
        {"type": "view", "label": "📊 Briefing", "url": f"{orch_url}/briefing"},
        {"type": "view", "label": "📋 Runs", "url": f"{orch_url}/runs"},
    ]

    if run_id:
        actions.insert(0, {
            "type": "view",
            "label": "📊 This Run",
            "url": f"{orch_url}/status/{run_id}",
        })

    _send_ntfy_enhanced(
        title=title,
        message=cheatsheet,
        priority="low",
        tags=["book", "link"],
        actions=actions,
        click_url=f"{orch_url}/briefing",
        ntfy_url=ntfy_url,
        ntfy_topic=ntfy_topic,
    )


# ------------------------------------------------
# NEW API ENDPOINTS TO ADD TO app.py
# ------------------------------------------------

"""
Add these endpoints to your app.py:

@app.post("/notifications/quick-actions")
def send_quick_actions():
    '''Send a remote-control notification with quick action buttons.'''
    send_quick_actions_notification(config=CONFIG)
    return {"status": "sent", "service": NOTIFY_SERVICE}


@app.post("/notifications/cheatsheet")
def send_cheatsheet(run_id: str = None, project_name: str = None, target: str = None):
    '''Send an API cheatsheet notification with curl commands.'''
    send_api_cheatsheet_notification(
        run_id=run_id,
        project_name=project_name,
        target=target,
        config=CONFIG
    )
    return {"status": "sent"}


@app.post("/notifications/digest")
def send_digest():
    '''Send a daily digest notification.'''
    send_daily_digest_notification(config=CONFIG)
    return {"status": "sent"}
"""


# ------------------------------------------------
# CONFIG.JSON ADDITION
# ------------------------------------------------

"""
Add "orchestrator_url" to your notifications config — this is the URL
your phone can reach. Since you're on Tailscale, use the Tailscale IP
of your orchestrator LXC:

  "notifications": {
    "enabled": true,
    "service": "ntfy",
    "ntfy_url": "http://100.84.63.13:8090",
    "ntfy_topic": "ai-orchestrator",
    "ntfy_priority": "default",
    "orchestrator_url": "http://100.84.63.13:8000",   ← ADD THIS
    ...
  }

If the orchestrator FastAPI isn't reachable via Tailscale yet,
you'll need to either:
  a) Install Tailscale on the orchestrator LXC, or
  b) Forward port 8000 from the Docker LXC (which already has Tailscale)
     to the orchestrator LXC at 192.168.2.216:8000

Option (b) is simpler — on the Docker LXC:
  iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport 8000 \
    -j DNAT --to-destination 192.168.2.216:8000
  iptables -A FORWARD -p tcp -d 192.168.2.216 --dport 8000 -j ACCEPT

Then orchestrator_url becomes "http://100.84.63.13:8000"
"""


# ------------------------------------------------
# ALTERNATIVE: TELEGRAM BOT (richer interactions)
# ------------------------------------------------

"""
If you want even richer mobile control (inline keyboards, conversation,
file sharing), a Telegram bot is the way to go. Self-hosted options:

1. python-telegram-bot (pip install python-telegram-bot)
   - Create a bot via @BotFather on Telegram
   - Get the token
   - Add to config: "telegram": {"enabled": true, "token": "...", "chat_id": "..."}

2. Example integration (add to app.py):

   import telegram  # pip install python-telegram-bot

   TELEGRAM_TOKEN = CONFIG.get("telegram", {}).get("token", "")
   TELEGRAM_CHAT_ID = CONFIG.get("telegram", {}).get("chat_id", "")

   async def send_telegram_notification(title, message, buttons=None):
       if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
           return
       bot = telegram.Bot(token=TELEGRAM_TOKEN)
       keyboard = None
       if buttons:
           keyboard = telegram.InlineKeyboardMarkup([
               [telegram.InlineKeyboardButton(b["label"], url=b["url"])]
               for b in buttons
           ])
       await bot.send_message(
           chat_id=TELEGRAM_CHAT_ID,
           text=f"*{title}*\\n\\n{message}",
           parse_mode="Markdown",
           reply_markup=keyboard
       )

   Telegram advantages over ntfy:
   - Inline keyboards (multiple rows of buttons)
   - Two-way: bot can receive commands (/status, /runs, /rerun project)
   - File sharing (send log files, code files)
   - Works everywhere without self-hosting the notification server
   - Conversation threads

   Telegram disadvantages:
   - Requires internet (not LAN-only)
   - Depends on Telegram servers
   - Setup is slightly more complex (BotFather, chat_id discovery)

3. For a fully self-hosted alternative to both:
   - Apprise (github.com/caronc/apprise) — unified notification library
     that supports 80+ services including ntfy, Gotify, Telegram,
     Matrix, Slack, Discord, email, etc.
   - pip install apprise
   - Single API for all notification backends
"""
