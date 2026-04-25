"""File-locked JSON read/write helpers.

All concurrent JSON access in /opt/ai-orchestrator/memory/ goes through
these. New memory files MUST use them — direct `open(..., 'r')` /
`json.load` will silently corrupt under concurrent runs.
"""
from __future__ import annotations

import fcntl
import json
from typing import Any


def locked_read_json(path, default: Any) -> Any:
    """Shared-lock read of a JSON file. Returns *default* on missing/corrupt."""
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except (json.JSONDecodeError, ValueError) as e:
        print(f"WARNING: corrupt JSON in {path}, resetting: {e}")
        return default
    except FileNotFoundError:
        return default
    except OSError as e:
        print(f"WARNING: could not read {path}: {e}")
        return default


def locked_write_json(path, data: Any) -> None:
    """Exclusive-lock write of *data* to *path* as indented JSON."""
    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(data, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        with open(path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"WARNING: could not write {path}: {e}")
