#!/usr/bin/env python3
import json, shutil, sys
from pathlib import Path

CFG = Path("/opt/ai-orchestrator/config.json")
BACKUP = CFG.with_suffix(".json.bak.mcp")

MCP_SLOT = {
    "blender": {
        "transport": "stdio",
        "command": "/opt/ai-orchestrator/venv/bin/blender-mcp",
        "args": [],
        "env": {"BLENDER_HOST": "100.95.33.64", "BLENDER_PORT": "9876"},
        "notes": "Windows VM 111 (Tailscale). Start blender-mcp addon inside Blender on the VM before first use."
    },
    "playwright": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "env": {},
        "notes": "Headless Chromium bundled. First call downloads browser (~150 MB)."
    },
    "ffmpeg": {
        "transport": "shell",
        "command": "/bin/ffmpeg",
        "args": [],
        "env": {},
        "notes": "Direct shell invocation, no MCP wrapper. Present as affordance for downstream tooling."
    }
}

def main():
    cfg = json.loads(CFG.read_text())
    if "mcp_servers" in cfg:
        existing = cfg["mcp_servers"]
        missing = [k for k in MCP_SLOT if k not in existing]
        if not missing:
            print("\u00b7 mcp_servers slot already complete ({} entries)".format(len(existing)))
            return 0
        for k in missing:
            existing[k] = MCP_SLOT[k]
        print("\u2713 mcp_servers slot extended with: " + ", ".join(missing))
    else:
        shutil.copy2(CFG, BACKUP)
        cfg["mcp_servers"] = MCP_SLOT
        print("\u00b7 backup at " + str(BACKUP))
        print("\u2713 mcp_servers slot added (3 entries)")
    CFG.write_text(json.dumps(cfg, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
