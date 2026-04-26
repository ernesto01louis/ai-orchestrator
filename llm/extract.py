"""Code/file extraction from LLM textual output.

LLMs return either a single code block (extract_code) or multiple files
delimited by `# === FILE: <name> ===` markers (extract_files). The
extractors strip common LLM artifacts (special tokens, fences) and
sanitize filenames against path traversal.
"""
from __future__ import annotations

import re

LLM_ARTIFACTS = re.compile(
    r"\[/?PYTHON\]|\[/?CODE\]|\[/?INST\]|\[/?OUTPUT\]|"
    r"<\|endoftext\|>|<\|im_end\|>|<\|im_start\|>.*?\n",
    re.IGNORECASE,
)

FILE_MARKER = re.compile(
    r"^#\s*={2,}\s*FILE:\s*(.+?)\s*={2,}\s*$",
    re.MULTILINE,
)

# kept local to extract; broader filename validation is in app.SAFE_FILENAME
_INNER_SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")


def extract_code(text: str) -> str:
    """Extract code from LLM output, stripping markdown fences and artifacts."""
    cleaned = LLM_ARTIFACTS.sub("", text)

    blocks = re.findall(r"```(?:\w+)?\s*\n(.*?)```", cleaned, re.DOTALL)
    if blocks:
        best = max(blocks, key=len)
        return best.strip()

    # fallback: strip any remaining markdown fences as raw lines
    lines = cleaned.strip().splitlines()
    if lines and re.match(r"^```\w*\s*$", lines[0]):
        lines = lines[1:]
    if lines and re.match(r"^```\s*$", lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_files(text: str, plan: dict) -> dict:
    """Pull individual files from LLM output that uses `# === FILE: ===` markers.

    Falls back to a single-file `{entrypoint: code}` dict if no markers found.
    """
    cleaned = LLM_ARTIFACTS.sub("", text)
    markers = list(FILE_MARKER.finditer(cleaned))

    if len(markers) >= 2:
        files = {}
        for i, match in enumerate(markers):
            filename = match.group(1).strip()
            if not _INNER_SAFE_FILENAME.match(filename):
                filename = re.sub(r"[^a-zA-Z0-9_\-\./]", "_", filename)
            filename = filename.lstrip("/").replace("..", "")

            start = match.end()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(cleaned)
            content = cleaned[start:end].strip()

            inner_blocks = re.findall(r"```(?:\w+)?\s*\n(.*?)```", content, re.DOTALL)
            if inner_blocks:
                content = max(inner_blocks, key=len).strip()
            if content:
                files[filename] = content

        if files:
            return files

    code = extract_code(text)
    if not code:
        return {}

    entrypoint = plan.get("entrypoint", "main.py")
    if not entrypoint or not isinstance(entrypoint, str):
        entrypoint = "main.py"
    return {entrypoint: code}


def format_files_for_prompt(files: dict) -> str:
    """Render a {filename: content} dict back into the LLM's expected format."""
    if len(files) == 1:
        filename = list(files.keys())[0]
        return f"# === FILE: {filename} ===\n{files[filename]}"
    parts = []
    for filename, content in files.items():
        parts.append(f"# === FILE: {filename} ===\n{content}")
    return "\n\n".join(parts)
