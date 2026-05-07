"""Execution layer — SSH primitives, sandbox runners, language handlers,
verification, dependency detection.

Single-file for the initial split (commit 0.g.4). The plan calls for further
splitting into ssh.py / verify.py / language.py / sandbox.py / deps.py /
inspector.py — defer that to a follow-up; this commit keeps app.py shrinking
without churning import paths twice in one phase.
"""
from __future__ import annotations

import ast
import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from core.config import (
    CONFIG, SSH_TARGETS, SSH_TIMEOUT, DEPLOY_BASE,
)
from core.paths import (
    LOG_DIR, SANDBOX_DIR, SANDBOX_VENV,
)
from core.runtime import log, _update_run_status
from llm.extract import extract_code, extract_files, format_files_for_prompt

# Used by sanitize_packages (kept local — only execution touches it)
SAFE_PKG_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.\[\]<>=!,]+$")


def verify_local(code, command_args, tmp_path):
    """Try to verify locally. Raises FileNotFoundError if tool missing."""
    with open(tmp_path, "w") as f:
        f.write(code)
    try:
        r = subprocess.run(command_args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "verification timed out after 30s"
    if r.returncode != 0:
        return False, r.stderr
    return True, ""


def verify_remote(code, target, remote_cmd, run_id):
    """Verify on the target Pi via SSH."""
    remote_tmp = "/tmp/ai_verify_tmp"
    fd, local_tmp = tempfile.mkstemp(prefix="ai_verify_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        try:
            deploy_file(local_tmp, remote_tmp, target)
        except RuntimeError as e:
            return False, f"Could not deploy for verification: {e}"
        result = ssh_command(target, remote_cmd.replace("{file}", remote_tmp))
        if result["returncode"] != 0:
            return False, result["stderr"]
        return True, ""
    finally:
        try:
            os.remove(local_tmp)
        except OSError:
            pass


VERIFY_CONFIG = {
    "python": {
        "local_args": lambda p: ["python3", "-m", "py_compile", p],
        "local_tmp": "/tmp/verify_script.py",
        "remote_cmd": "python3 -m py_compile {file}",
    },
    "bash": {
        "local_args": lambda p: ["bash", "-n", p],
        "local_tmp": "/tmp/verify_script.sh",
        "remote_cmd": "bash -n {file}",
    },
    "javascript": {
        "local_args": lambda p: ["node", "--check", p],
        "local_tmp": "/tmp/verify_script.js",
        "remote_cmd": "node --check {file}",
    },
}


def verify_code(code, language, target, run_id):
    """Verify a single file. Tries local first, falls back to SSH."""
    lang_key = language.lower().strip()
    # resolve aliases to primary names
    alias_map = {"node": "javascript", "nodejs": "javascript",
                 "node.js": "javascript", "js": "javascript",
                 "sh": "bash", "shell": "bash"}
    lang_key = alias_map.get(lang_key, lang_key)
    if lang_key not in VERIFY_CONFIG:
        lang_key = "python"

    cfg = VERIFY_CONFIG[lang_key]
    tmp_path = cfg["local_tmp"]

    # try local
    try:
        return verify_local(code, cfg["local_args"](tmp_path), tmp_path)
    except FileNotFoundError:
        pass

    # fallback: SSH to target
    if target:
        return verify_remote(code, target, cfg["remote_cmd"], run_id)

    return False, f"Cannot verify {language}: tool not installed locally and no target"


# -- Python dependency detection --

PYTHON_STDLIB = {
    "sys", "os", "math", "json", "time", "random", "subprocess", "threading",
    "multiprocessing", "urllib", "http", "pathlib", "itertools", "collections",
    "functools", "re", "datetime", "hashlib", "base64", "socket", "shutil",
    "tempfile", "argparse", "logging", "asyncio", "typing", "dataclasses",
    "enum", "abc", "io", "struct", "csv", "sqlite3", "unittest", "textwrap",
    "string", "copy", "pprint", "traceback", "signal", "ctypes", "array",
    "bisect", "heapq", "operator", "contextlib", "weakref", "inspect",
    "importlib", "pkgutil", "zipfile", "tarfile", "gzip", "bz2", "lzma",
    "configparser", "secrets", "hmac", "ssl", "select", "selectors",
    "statistics", "decimal", "fractions", "numbers", "cmath", "glob",
    "fnmatch", "xml", "html", "email", "mailbox", "mimetypes", "codecs",
    "unicodedata", "locale", "gettext", "platform", "errno", "warnings",
    "atexit", "sched", "queue", "concurrent", "venv", "distutils",
    "compileall", "dis", "token", "tokenize", "symtable", "keyword"
}


def detect_python_deps(code):

    packages = set()

    try:
        tree = ast.parse(code)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    pkg = name.name.split(".")[0]
                    if pkg not in PYTHON_STDLIB:
                        packages.add(pkg)

            if isinstance(node, ast.ImportFrom):
                if node.module:
                    pkg = node.module.split(".")[0]
                    if pkg not in PYTHON_STDLIB:
                        packages.add(pkg)

    except SyntaxError as e:
        print(f"python dependency detection: could not parse code: {e}")

    return packages


# -- JavaScript dependency detection --

def detect_node_deps(code):
    """Detect npm require() and import calls."""

    NODE_BUILTINS = {
        "fs", "path", "os", "http", "https", "url", "util", "events",
        "stream", "buffer", "crypto", "zlib", "child_process", "cluster",
        "net", "dns", "tls", "readline", "repl", "vm", "assert",
        "querystring", "string_decoder", "timers", "console", "process",
        "worker_threads", "perf_hooks", "async_hooks", "inspector",
        "node:fs", "node:path", "node:os", "node:http", "node:https",
        "node:url", "node:util", "node:events", "node:stream",
        "node:crypto", "node:child_process", "node:net"
    }

    packages = set()

    # require('package') or require("package")
    for match in re.findall(r"""require\s*\(\s*['"]([^'"./][^'"]*)['"]\s*\)""", code):
        pkg = match.split("/")[0]
        if pkg not in NODE_BUILTINS:
            packages.add(pkg)

    # import ... from 'package' or import 'package'
    for match in re.findall(r"""(?:from|import)\s+['"]([^'"./][^'"]*)['"]\s*""", code):
        pkg = match.split("/")[0]
        if pkg not in NODE_BUILTINS:
            packages.add(pkg)

    return packages


# -- Bash dependency detection --

def detect_bash_deps(code):
    """Bash scripts don't have pip-style deps. Return empty."""
    return set()


# -- Language handler registry --

LANG_HANDLERS = {
    "python": {
        "detect_deps": detect_python_deps,
        "file_extensions": {".py"},
        "default_entrypoint": "main.py",
        "run_command": "python3",
        "env_type": "venv",
        "dep_installer": "pip",
    },
    "bash": {
        "detect_deps": detect_bash_deps,
        "file_extensions": {".sh"},
        "default_entrypoint": "main.sh",
        "run_command": "bash",
        "env_type": "none",
        "dep_installer": "none",
    },
    "javascript": {
        "detect_deps": detect_node_deps,
        "file_extensions": {".js", ".mjs"},
        "default_entrypoint": "index.js",
        "run_command": "node",
        "env_type": "npm",
        "dep_installer": "npm",
    },
    "node": None,       # alias
    "nodejs": None,     # alias
    "node.js": None,    # alias
    "js": None,         # alias
    "sh": None,         # alias
    "shell": None,      # alias
}

# resolve aliases
LANG_HANDLERS["node"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["nodejs"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["node.js"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["js"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["sh"] = LANG_HANDLERS["bash"]
LANG_HANDLERS["shell"] = LANG_HANDLERS["bash"]


def get_lang_handler(language):
    """Get the handler for a language. Falls back to python."""
    lang = language.lower().strip() if language else "python"
    handler = LANG_HANDLERS.get(lang)
    if handler is None:
        return LANG_HANDLERS["python"]
    return handler


def get_verifiable_extensions(language):
    """Get file extensions that should be verified for a language."""
    handler = get_lang_handler(language)
    return handler["file_extensions"]


# ------------------------------------------------
# MULTI-LANGUAGE VERIFICATION
# ------------------------------------------------

def verify_files(files, language, target, run_id):
    """Verify all source files. Tries local, falls back to SSH on target."""

    handler = get_lang_handler(language)
    extensions = handler["file_extensions"]

    log(run_id, f"verifying {len(files)} file(s) [{language}]")

    errors = {}
    all_passed = True

    for filename, content in files.items():

        ext = os.path.splitext(filename)[1]

        if ext not in extensions:
            continue

        passed, error = verify_code(content, language, target, run_id)

        if not passed:
            log(run_id, f"verification failed: {filename}: {error[:200]}")
            errors[filename] = error
            all_passed = False
        else:
            log(run_id, f"verification passed: {filename}")

    return all_passed, errors


# ------------------------------------------------
# MULTI-LANGUAGE DEPENDENCY DETECTION
# ------------------------------------------------

def detect_all_dependencies(files, language, project_modules):
    """Detect dependencies across all files using the right language handler."""

    handler = get_lang_handler(language)
    detect_fn = handler["detect_deps"]
    extensions = handler["file_extensions"]

    all_deps = set()

    for filename, content in files.items():
        ext = os.path.splitext(filename)[1]
        if ext in extensions:
            deps = detect_fn(content)
            all_deps.update(deps)

    # remove local project module names
    all_deps -= project_modules

    return list(all_deps)


def get_project_modules(files):

    modules = set()

    for filename in files:
        # handle .py modules
        if filename.endswith(".py"):
            base = filename.replace(".py", "").replace("/", ".")
            top_level = base.split(".")[0]
            modules.add(top_level)
        # handle .js local modules
        elif filename.endswith(".js"):
            base = filename.replace(".js", "").replace("/", ".")
            top_level = base.split(".")[0]
            modules.add(top_level)
            modules.add(f"./{filename}")
            modules.add(f"./{filename.replace('.js', '')}")

    return modules


# ------------------------------------------------
# SANITIZE PACKAGE NAMES
# ------------------------------------------------

def sanitize_packages(packages):
    safe = []
    for pkg in packages:
        pkg = pkg.strip()
        if SAFE_PKG_PATTERN.match(pkg) and len(pkg) < 100:
            safe.append(pkg)
        else:
            print(f"WARNING: rejected suspicious package name: {repr(pkg)}")
    return safe


# ------------------------------------------------
# SSH
# ------------------------------------------------

def validate_target(target):
    if target not in SSH_TARGETS:
        raise ValueError(
            f"Unknown deploy target '{target}'. "
            f"Available: {list(SSH_TARGETS.keys())}"
        )


def ssh_command(target, command, *, quote_command=False):

    validate_target(target)

    cfg = SSH_TARGETS[target]

    # quote_command=True for user-supplied input; False for internal shell pipelines
    remote_cmd = shlex.quote(command) if quote_command else command

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        "-i",
        cfg["key_path"],
        f"{cfg['username']}@{cfg['host']}",
        remote_cmd
    ]

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT + 30
        )

        return {
            "stdout": r.stdout,
            "stderr": r.stderr,
            "returncode": r.returncode
        }

    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"SSH command timed out after {SSH_TIMEOUT + 30}s",
            "returncode": -1
        }


def deploy_file(local, remote, target):

    validate_target(target)

    cfg = SSH_TARGETS[target]

    try:
        r = subprocess.run(
            [
                "scp",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={SSH_TIMEOUT}",
                "-i",
                cfg["key_path"],
                local,
                f"{cfg['username']}@{cfg['host']}:{remote}"
            ],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT + 30
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SCP timed out after {SSH_TIMEOUT + 30}s")

    if r.returncode != 0:
        raise RuntimeError(f"SCP failed ({r.returncode}): {r.stderr.strip()}")


def deploy_project(files, target, remote_dir, run_id):
    """Deploy files to a specific directory on the target."""

    log(run_id, f"deploying {len(files)} file(s) to {target}:{remote_dir}")

    q_dir = shlex.quote(remote_dir)
    ssh_command(target, f"rm -rf {q_dir} && mkdir -p {q_dir}")

    for filename, content in files.items():

        remote_path = f"{remote_dir}/{filename}"
        file_remote_dir = os.path.dirname(remote_path)

        if file_remote_dir and file_remote_dir != remote_dir:
            ssh_command(target, f"mkdir -p {shlex.quote(file_remote_dir)}")

        fd, local_path = tempfile.mkstemp(prefix="ai_deploy_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            deploy_file(local_path, remote_path, target)
            log(run_id, f"deployed: {filename}")
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

    log(run_id, "all files deployed")


# ------------------------------------------------
# ENVIRONMENT INSPECTOR
# ------------------------------------------------

def environment_inspector(target, run_id):

    log(run_id, "deep environment inspection")

    cmds = {
        "os": "cat /etc/os-release",
        "arch": "uname -m",
        "kernel": "uname -r",
        "python": "python3 --version",
        "pip_packages": "python3 -m pip list --format=json",
        "node": "node --version",
        "npm": "npm --version",
        "memory": "free -h",
        "disk": "df -h",
        "cpu": "nproc",
        "docker": "docker --version",
        "usb": "lsusb"
    }

    env = {}

    for k, c in cmds.items():
        r = ssh_command(target, c)
        env[k] = r["stdout"].strip()

    return env


# ------------------------------------------------
# VENV MANAGEMENT
# ------------------------------------------------

def ensure_venv(target, venv_path, run_id):
    """Create venv on target only if it doesn't already exist."""

    check = ssh_command(target, f"test -f {venv_path}/bin/python3 && echo EXISTS")

    if "EXISTS" in check["stdout"]:
        log(run_id, f"reusing existing venv: {venv_path}")
        return

    log(run_id, f"creating new venv: {venv_path}")
    result = ssh_command(target, f"python3 -m venv {venv_path}")

    if result["returncode"] != 0:
        log(run_id, f"venv creation failed: {result['stderr'][:300]}")


def install_dependencies(target, deps, venv_path, run_id):
    """Install dependencies into a specific venv. Returns True if successful."""

    if not deps:
        return True

    dep_str = " ".join(deps)

    log(run_id, f"installing dependencies: {deps}")

    install_result = ssh_command(
        target,
        f"source {venv_path}/bin/activate && pip install --disable-pip-version-check {dep_str}"
    )

    if install_result["returncode"] != 0:
        log(run_id, f"pip install failed: {install_result['stderr'][:500]}")
        return False

    log(run_id, "dependencies installed successfully")
    return True


# ------------------------------------------------
# SANDBOX EXECUTION (uses /tmp, disposable)
# ------------------------------------------------

def sandbox_execute(target, files, entrypoint, language, plan, run_id):
    """Deploy and test-run in the sandbox. Language and project-type aware."""

    handler = get_lang_handler(language)
    env_type = handler["env_type"]
    run_command = handler["run_command"]

    project_type = plan.get("project_type", "script")
    port = plan.get("port", 0)

    # check system-level dependencies (auto-install if sudo enabled)
    missing = ensure_system_dependencies(target, language, plan, run_id)
    if missing:
        log(run_id, f"WARNING: missing system deps {missing}, execution may fail")

    project_modules = get_project_modules(files)
    deps = detect_all_dependencies(files, language, project_modules)
    deps = sanitize_packages(deps)

    # set up environment based on language
    if env_type == "venv":
        ensure_venv(target, SANDBOX_VENV, run_id)
        pip_ok = install_dependencies(target, deps, SANDBOX_VENV, run_id)
        if not pip_ok and deps:
            log(run_id, "WARNING: proceeding despite pip failure")

    elif env_type == "npm" and deps:
        deploy_project(files, target, SANDBOX_DIR, run_id)
        pkg_json = json.dumps({
            "name": "ai-sandbox",
            "version": "1.0.0",
            "dependencies": {d: "*" for d in deps}
        }, indent=2)
        fd, local_pkg = tempfile.mkstemp(prefix="ai_sandbox_pkg_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pkg_json)
            deploy_file(local_pkg, f"{SANDBOX_DIR}/package.json", target)
        finally:
            try:
                os.remove(local_pkg)
            except OSError:
                pass
        log(run_id, f"npm installing: {deps}")
        npm_result = ssh_command(target, f"cd {SANDBOX_DIR} && npm install --no-audit --no-fund")
        if npm_result["returncode"] != 0:
            log(run_id, f"npm install failed: {npm_result['stderr'][:300]} {npm_result['stdout'][:200]}")

    # deploy files (skip if already deployed for npm)
    if env_type != "npm" or not deps:
        deploy_project(files, target, SANDBOX_DIR, run_id)

    # build the run command prefix
    if env_type == "venv":
        cmd_prefix = f"source {SANDBOX_VENV}/bin/activate && cd {SANDBOX_DIR}"
    else:
        cmd_prefix = f"cd {SANDBOX_DIR}"

    # --- SERVER EXECUTION ---
    if project_type == "server" and port > 0:
        return sandbox_execute_server(
            target, run_command, entrypoint, cmd_prefix, port, files, run_id
        )

    # --- SCRIPT EXECUTION ---
    log(run_id, f"executing entrypoint: {run_command} {entrypoint}")

    run_cmd = f"{cmd_prefix} && {run_command} {entrypoint}"

    return ssh_command(target, run_cmd)


# ------------------------------------------------
# PORT AWARENESS
# ------------------------------------------------

def check_port_available(target, port, run_id):
    """Check if a port is available on the target."""
    result = ssh_command(target, f"ss -tlnp 2>/dev/null | grep ':{port} ' || echo FREE")
    return "FREE" in result["stdout"]


def find_available_port(target, preferred_port, run_id):
    """
    Check if the preferred port is free. If not, scan nearby ports.
    Returns (port, was_changed).
    """

    if check_port_available(target, preferred_port, run_id):
        return preferred_port, False

    log(run_id, f"port {preferred_port} is in use, scanning for alternatives")

    # try nearby ports: preferred+1 through preferred+20
    for offset in range(1, 21):
        candidate = preferred_port + offset
        if candidate > 65535:
            break
        if check_port_available(target, candidate, run_id):
            log(run_id, f"found available port: {candidate}")
            return candidate, True

    # try common alternative ports
    for candidate in [8080, 8000, 8888, 9000, 5000, 4000]:
        if candidate != preferred_port and check_port_available(target, candidate, run_id):
            log(run_id, f"found available port: {candidate}")
            return candidate, True

    log(run_id, f"WARNING: could not find available port near {preferred_port}, using it anyway")
    return preferred_port, False


def patch_port_in_files(files, old_port, new_port):
    """Replace port references in generated code when we need to use a different port."""

    patched = {}

    for filename, content in files.items():
        patched[filename] = content.replace(str(old_port), str(new_port))

    return patched


# ------------------------------------------------
# SYSTEM DEPENDENCY INSTALLER
# ------------------------------------------------

# sudo allowlist: only these commands can be run with sudo
SUDO_ALLOWED = CONFIG.get("sudo", {}).get("allowed_commands", [
    "apt-get install -y",
    "apt-get update",
    "npm install -g",
    "systemctl start",
    "systemctl stop",
    "systemctl restart",
    "systemctl enable",
    "systemctl status",
])

SUDO_ENABLED = CONFIG.get("sudo", {}).get("enabled", False)


def sudo_command(target, command, run_id):
    """
    Run a command with sudo on the target, but only if:
    1. Sudo is enabled in config
    2. The command matches the allowlist
    """

    if not SUDO_ENABLED:
        log(run_id, f"sudo disabled, skipping: {command}")
        return ssh_command(target, command)

    # check against allowlist
    allowed = False
    for pattern in SUDO_ALLOWED:
        if command.strip().startswith(pattern):
            allowed = True
            break

    if not allowed:
        log(run_id, f"sudo command not in allowlist, running without sudo: {command}")
        return ssh_command(target, command)

    log(run_id, f"sudo: {command}")
    return ssh_command(target, f"sudo {command}")


# map of language -> required system packages
SYSTEM_DEPS = {
    "python": {
        "checks": ["python3 --version"],
        "packages_apt": ["python3", "python3-venv", "python3-pip"],
    },
    "bash": {
        "checks": ["bash --version"],
        "packages_apt": ["bash"],
    },
    "javascript": {
        "checks": ["node --version", "npm --version"],
        "packages_apt": ["nodejs", "npm"],
    },
}


def ensure_system_dependencies(target, language, plan, run_id):
    """
    Check that the target has the required system-level tools for the language.
    Auto-installs via apt if sudo is enabled and tools are missing.
    Returns list of missing tools (empty if all good).
    """

    handler = get_lang_handler(language)
    lang_key = None
    for key in SYSTEM_DEPS:
        if get_lang_handler(key) == handler:
            lang_key = key
            break

    if not lang_key or lang_key not in SYSTEM_DEPS:
        return []

    dep_info = SYSTEM_DEPS[lang_key]

    # check all required tools
    all_present = True
    for check_cmd in dep_info["checks"]:
        check_result = ssh_command(target, f"{check_cmd} 2>/dev/null")
        if check_result["returncode"] != 0:
            log(run_id, f"system dependency missing: '{check_cmd}' failed on {target}")
            all_present = False

    if all_present:
        return []

    if not SUDO_ENABLED:
        log(run_id, f"sudo not enabled, cannot auto-install. Missing: {dep_info['packages_apt']}")
        return dep_info["packages_apt"]

    log(run_id, f"auto-installing system dependencies: {dep_info['packages_apt']}")

    sudo_command(target, "apt-get update", run_id)

    pkg_str = " ".join(dep_info["packages_apt"])
    install_result = sudo_command(target, f"apt-get install -y {pkg_str}", run_id)

    if install_result["returncode"] != 0:
        log(run_id, f"apt install failed: {install_result['stderr'][:300]}")
        return dep_info["packages_apt"]

    log(run_id, "system dependencies installed successfully")
    return []


# ------------------------------------------------
# SERVER-AWARE SANDBOX EXECUTION
# ------------------------------------------------

def sandbox_execute_server(target, run_command, entrypoint, cmd_prefix, port, files, run_id):
    """
    Execute a server in the sandbox:
    1. Check port availability, find alternative if needed
    2. Kill anything on the chosen port
    3. Start the server in the background
    4. Wait for the port to become available
    5. Curl the server to test it
    6. Kill the server
    7. Return the curl result as execution output
    """

    # port awareness: find an available port
    actual_port, port_changed = find_available_port(target, port, run_id)

    if port_changed:
        log(run_id, f"port {port} in use, switched to {actual_port}")
        # patch port references in the deployed files
        patched_files = patch_port_in_files(files, port, actual_port)
        deploy_project(patched_files, target, SANDBOX_DIR, run_id)

    log(run_id, f"executing server: {run_command} {entrypoint} (port {actual_port})")

    # kill any leftover process on this port
    ssh_command(target, f"fuser -k {int(actual_port)}/tcp 2>/dev/null || true")

    time.sleep(1)

    # start server in background, capture PID
    q_entry = shlex.quote(entrypoint)
    start_cmd = (
        f"{cmd_prefix} && "
        f"nohup {run_command} {q_entry} < /dev/null > /tmp/ai_server.log 2>&1 & "
        f"echo $!"
    )

    start_result = ssh_command(target, start_cmd)
    server_pid = start_result["stdout"].strip().splitlines()[-1] if start_result["stdout"] else ""

    if not server_pid or not server_pid.isdigit():
        log(run_id, f"failed to start server, could not capture PID: {start_result['stderr']}")
        return {
            "stdout": start_result["stdout"],
            "stderr": f"Failed to start server: {start_result['stderr']}",
            "returncode": 1
        }

    log(run_id, f"server started with PID {server_pid}")

    # wait for port to become available (up to 15 seconds)
    port_ready = False
    for attempt in range(15):
        time.sleep(1)
        check = ssh_command(target, f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{actual_port}/ 2>/dev/null || echo FAIL")
        response = check["stdout"].strip()
        if response and response != "FAIL" and response != "000":
            port_ready = True
            log(run_id, f"server responding on port {actual_port} (attempt {attempt + 1})")
            break

    if not port_ready:
        alive = ssh_command(target, f"kill -0 {server_pid} 2>/dev/null && echo ALIVE || echo DEAD")
        server_log = ssh_command(target, "cat /tmp/ai_server.log 2>/dev/null")

        ssh_command(target, f"kill {server_pid} 2>/dev/null || true")
        ssh_command(target, f"fuser -k {actual_port}/tcp 2>/dev/null || true")

        log(run_id, f"server did not respond on port {actual_port} within 15s (process: {alive['stdout'].strip()})")

        return {
            "stdout": "",
            "stderr": f"Server did not respond on port {actual_port}. "
                      f"Process: {alive['stdout'].strip()}. "
                      f"Log: {server_log['stdout'][:500]}",
            "returncode": 1
        }

    # server is up — test it
    log(run_id, f"testing server at http://localhost:{actual_port}/")

    test_result = ssh_command(target, f"curl -s --max-time 10 http://localhost:{actual_port}/")

    server_log = ssh_command(target, "cat /tmp/ai_server.log 2>/dev/null")

    # cleanup: kill the server
    ssh_command(target, f"kill {server_pid} 2>/dev/null || true")
    ssh_command(target, f"fuser -k {actual_port}/tcp 2>/dev/null || true")

    log(run_id, "server tested and stopped")

    return {
        "stdout": test_result["stdout"],
        "stderr": server_log["stdout"][:500] if server_log["stdout"] else "",
        "returncode": test_result["returncode"]
    }


# ------------------------------------------------
# PERSISTENT DEPLOYMENT
# ------------------------------------------------

def persistent_deploy(target, project_name, files, entrypoint, language, plan, deps,
                      run_id, score, prompt, run_id_str):
    """
    Promote a successful sandbox run to a persistent project directory.
    Language-aware: sets up the appropriate environment per language.
    """

    handler = get_lang_handler(language)
    env_type = handler["env_type"]
    run_command = handler["run_command"]

    # resolve the actual deploy base path on the target
    resolve = ssh_command(target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    if not base:
        base = "/tmp/ai-projects"
        log(run_id_str, f"WARNING: could not resolve deploy base, using {base}")

    project_dir = f"{base}/{project_name}"
    src_dir = f"{project_dir}/src"

    log(run_id_str, f"persistent deploy [{language}] -> {target}:{project_dir}")

    # create project structure
    ssh_command(target, f"mkdir -p {shlex.quote(src_dir)}")

    # deploy source files
    for filename, content in files.items():

        remote_path = f"{src_dir}/{filename}"
        file_dir = os.path.dirname(remote_path)

        if file_dir and file_dir != src_dir:
            ssh_command(target, f"mkdir -p {shlex.quote(file_dir)}")

        fd, local_path = tempfile.mkstemp(prefix="ai_persist_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            deploy_file(local_path, remote_path, target)
            log(run_id_str, f"persisted: {filename}")
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

    deps_sanitized = sanitize_packages(deps)

    # set up environment based on language
    if env_type == "venv":
        venv_dir = f"{project_dir}/.venv"
        ensure_venv(target, venv_dir, run_id_str)
        if deps_sanitized:
            install_dependencies(target, deps_sanitized, venv_dir, run_id_str)

    elif env_type == "npm" and deps_sanitized:
        pkg_json = json.dumps({
            "name": project_name,
            "version": "1.0.0",
            "dependencies": {d: "*" for d in deps_sanitized}
        }, indent=2)
        fd, local_pkg = tempfile.mkstemp(prefix="ai_persist_pkg_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pkg_json)
            deploy_file(local_pkg, f"{src_dir}/package.json", target)
        finally:
            try:
                os.remove(local_pkg)
            except OSError:
                pass
        log(run_id_str, f"npm installing: {deps_sanitized}")
        ssh_command(target, f"cd {src_dir} && npm install --no-audit --no-fund")

    # generate language-specific run.sh
    if env_type == "venv":
        run_script = f"""#!/bin/bash
# Auto-generated by AI Orchestrator
# Project: {project_name} | Language: {language}
# Run ID:  {run_id} | Score: {score}

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
cd "$SCRIPT_DIR/src"
{run_command} {entrypoint} "$@"
"""
    else:
        run_script = f"""#!/bin/bash
# Auto-generated by AI Orchestrator
# Project: {project_name} | Language: {language}
# Run ID:  {run_id} | Score: {score}

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$SCRIPT_DIR/src"
{run_command} {entrypoint} "$@"
"""

    fd, local_run = tempfile.mkstemp(prefix="ai_persist_run_", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(run_script)
        deploy_file(local_run, f"{project_dir}/run.sh", target)
    finally:
        try:
            os.remove(local_run)
        except OSError:
            pass
    ssh_command(target, f"chmod +x {shlex.quote(project_dir)}/run.sh")

    # make bash/shell entrypoints executable too
    if language.lower() in ("bash", "sh", "shell"):
        ssh_command(target, f"chmod +x {shlex.quote(src_dir)}/{shlex.quote(entrypoint)}")

    # create project.json metadata
    metadata = {
        "project_name": project_name,
        "language": language,
        "run_id": run_id,
        "score": score,
        "prompt": prompt,
        "entrypoint": entrypoint,
        "files": list(files.keys()),
        "dependencies": deps_sanitized,
        "target": target,
        "deploy_path": project_dir,
        "deployed_at": datetime.utcnow().isoformat(),
        "plan": plan
    }

    local_meta = "/tmp/ai_persist_meta.json"
    with open(local_meta, "w") as f:
        json.dump(metadata, f, indent=2)

    deploy_file(local_meta, f"{project_dir}/project.json", target)

    log(run_id_str, f"persistent deploy complete: {project_dir}")

    return project_dir

