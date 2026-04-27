"""Reference document ingestion (PDF / text / images).

The data directory `/opt/ai-orchestrator/references/` lives under the same
repo root, so this Python package is named `references_pkg` to avoid the
namespace clash. (`from references_pkg import convert_pdf_to_markdown`.)

Routes (`POST /references/upload`, etc.) stay in app.py until commit 0.g.8;
this module owns the conversion + image-description helpers and the few
constants they share.
"""
from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from core.config import OLLAMA_MAIN_URL
from core.paths import REFERENCE_DIR
from core.runtime import log
from llm.ollama import (
    query_ollama_api,
    _refresh_url_cache,
    _URL_CACHE_TTL,
)
import llm.ollama as _llm_ollama


def _url_cache_state():
    """Lazy accessor: keep references_pkg's URL cache view in sync with llm.ollama."""
    return _llm_ollama._url_cache, _llm_ollama._url_cache_ts

# text-native extensions — read as-is, no conversion needed
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml",
                   ".py", ".js", ".sh", ".bash", ".html", ".css", ".xml",
                   ".ini", ".cfg", ".conf", ".log", ".env", ".sql"}

MAX_REFERENCE_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB per file
MAX_REFERENCE_CONTENT_CHARS = 120_000            # ~30k tokens, safe for most models


def _detect_vision_model():
    """Check if any vision-capable model is available on Ollama."""
    try:
        cache, cache_ts = _url_cache_state()
        if time.time() - cache_ts > _URL_CACHE_TTL:
            _refresh_url_cache()
            cache, _ = _url_cache_state()
        vision_keywords = ["llava", "minicpm-v", "bakllava", "moondream", "vision"]
        for model_name, base_url in cache.items():
            if any(kw in model_name.lower() for kw in vision_keywords):
                return model_name, base_url
    except Exception:
        pass
    return None, None


def _describe_image_with_vision(image_bytes, model, base_url, context=""):
    """Use a vision model to describe an image. Returns description string."""
    import base64
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = "Describe this image concisely. Focus on data, diagrams, charts, or technical content. "
    if context:
        prompt += f"Context: this image is from a document about {context}. "
    prompt += "Describe what information this image conveys."

    try:
        r = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
            },
            timeout=60
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"[image description unavailable: {e}]"


def convert_pdf_to_markdown(pdf_path, run_id="convert"):
    """
    Convert a PDF to LLM-optimized markdown using pymupdf4llm.
    Also extracts images and optionally describes them with a vision model.
    Returns (markdown_text, image_count, images_described).
    """

    try:
        import pymupdf4llm
        import pymupdf
    except ImportError:
        # fallback: basic text extraction
        return _convert_pdf_basic(pdf_path, run_id)

    log(run_id, f"converting PDF: {pdf_path}")

    # pymupdf4llm does the heavy lifting: tables, headings, lists → markdown
    try:
        md_text = pymupdf4llm.to_markdown(str(pdf_path))
    except Exception as e:
        log(run_id, f"pymupdf4llm failed, falling back to basic extraction: {e}")
        return _convert_pdf_basic(pdf_path, run_id)

    # extract images
    image_count = 0
    images_described = 0
    vision_model, vision_url = _detect_vision_model()

    doc = None
    try:
        doc = pymupdf.open(str(pdf_path))
        pdf_stem = Path(pdf_path).stem
        img_dir = REFERENCE_DIR / f"{pdf_stem}_images"

        for page_num in range(len(doc)):
            page = doc[page_num]
            images = page.get_images(full=True)

            for img_idx, img_info in enumerate(images):
                xref = img_info[0]
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.n > 4:  # CMYK → RGB
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

                    image_count += 1
                    img_filename = f"page{page_num+1}_img{img_idx+1}.png"

                    # save image file
                    img_dir.mkdir(parents=True, exist_ok=True)
                    img_path = img_dir / img_filename
                    pix.save(str(img_path))

                    # describe with vision model if available
                    description = ""
                    if vision_model and vision_url:
                        img_bytes = pix.tobytes("png")
                        description = _describe_image_with_vision(
                            img_bytes, vision_model, vision_url,
                            context=Path(pdf_path).stem
                        )
                        images_described += 1

                    # inject image reference into markdown
                    img_marker = f"\n\n**[Image: page {page_num+1}, figure {img_idx+1}]**"
                    if description:
                        img_marker += f"\n> {description}"
                    else:
                        img_marker += f"\n> *(visual content — no vision model available for description)*"
                    img_marker += f"\n> *Saved: {img_path}*\n"

                    # append to end of markdown (images are supplementary)
                    md_text += img_marker

                    pix = None  # free memory
                except Exception as e:
                    log(run_id, f"image extraction failed (page {page_num+1}, img {img_idx+1}): {e}")
    except Exception as e:
        log(run_id, f"image extraction phase failed (non-fatal): {e}")
    finally:
        if doc:
            doc.close()

    log(run_id, f"PDF converted: {len(md_text)} chars, {image_count} images, {images_described} described")
    return md_text, image_count, images_described


def _convert_pdf_basic(pdf_path, run_id="convert"):
    """Fallback: basic page-by-page text extraction."""
    doc = None
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text("text").strip()
            if text:
                pages.append(f"## Page {i+1}\n\n{text}")
        md = "\n\n---\n\n".join(pages) if pages else "[empty PDF]"
        return md, 0, 0
    except Exception as e:
        log(run_id, f"basic PDF extraction failed: {e}")
        return f"[PDF extraction failed: {e}]", 0, 0
    finally:
        if doc:
            doc.close()


def convert_file_to_markdown(file_path, run_id="convert"):
    """
    Convert any supported file to markdown for LLM consumption.
    PDFs get full conversion. Text files get wrapped in code fences.
    Returns (markdown_text, metadata_dict).
    """

    path = Path(file_path)
    ext = path.suffix.lower()
    name = path.name

    if ext == ".pdf":
        md_text, img_count, img_described = convert_pdf_to_markdown(file_path, run_id)
        return md_text, {"type": "pdf", "images": img_count, "images_described": img_described}

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(errors="replace")
            # wrap code files in fences for clarity
            if ext in {".py", ".js", ".sh", ".bash", ".html", ".css", ".sql", ".xml"}:
                lang = ext.lstrip(".")
                md_text = f"# {name}\n\n```{lang}\n{content}\n```"
            elif ext in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}:
                md_text = f"# {name}\n\n```{ext.lstrip('.')}\n{content}\n```"
            elif ext == ".csv":
                # convert CSV to markdown table (first 50 rows)
                lines = content.strip().splitlines()[:51]
                if lines:
                    header = lines[0]
                    md_text = f"# {name}\n\n| {header.replace(',', ' | ')} |\n"
                    md_text += "| " + " | ".join(["---"] * len(header.split(","))) + " |\n"
                    for line in lines[1:50]:
                        md_text += f"| {line.replace(',', ' | ')} |\n"
                    if len(lines) > 50:
                        md_text += f"\n*({len(content.splitlines())} total rows, showing first 50)*\n"
                else:
                    md_text = f"# {name}\n\n[empty CSV]"
            else:
                md_text = f"# {name}\n\n{content}"

            return md_text, {"type": ext.lstrip(".")}
        except Exception as e:
            return f"[file read failed: {e}]", {"type": ext.lstrip("."), "error": str(e)}

    # unsupported extension — try reading as text
    try:
        content = path.read_text(errors="replace")
        return f"# {name}\n\n{content}", {"type": "text"}
    except Exception:
        return f"[unsupported file type: {ext}]", {"type": "unsupported"}


def load_reference_content(filename):
    """Load the markdown version of a reference file. Returns content string."""

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)

    # check for pre-converted markdown version first
    md_path = REFERENCE_DIR / f"{Path(safe_name).stem}.md"
    if md_path.exists() and safe_name != md_path.name:
        return md_path.read_text(errors="replace")

    # load original
    orig_path = REFERENCE_DIR / safe_name
    if not orig_path.exists():
        return ""

    # if it's already markdown/text, read directly
    ext = orig_path.suffix.lower()
    if ext in TEXT_EXTENSIONS or ext == ".md":
        return orig_path.read_text(errors="replace")

    # for PDFs and other files, attempt on-the-fly conversion
    if ext == ".pdf" or ext in TEXT_EXTENSIONS:
        try:
            md_text, _ = convert_file_to_markdown(str(orig_path), "load-ref")
            # cache the conversion for next time
            if md_text and not md_text.startswith("["):
                md_path.write_text(md_text)
            return md_text
        except Exception:
            return ""

    return ""

