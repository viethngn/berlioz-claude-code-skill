#!/usr/bin/env python3
"""Describe one image via a nano-banana-pro-compatible vision endpoint.

Uses google-genai in Vertex AI mode against a configurable base URL. The
prompt is tuned to produce a description suitable for a knowledge wiki —
identifies the type of image, transcribes any visible text, and summarizes
the concept.

Usage:
    python3 describe_image.py --wiki-root /path/to/wiki --image /path/to/img.png --output /path/to/img.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from _deps import require

require(["google.genai", "PIL"])

# The google-genai client and the nano-banana-pro upstream both like TLS knobs
# tweaked before import. Mirrors the pattern in plugins/nano-banana-pro/scripts/image.py.
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")

try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:  # pragma: no cover
    pass

from google import genai
from google.genai import types
from PIL import Image

from config import ConfigError, apply_ssl_env, load_config
from rate_limiter import get_limiter


DEFAULT_PROMPT = """You are describing an image so it can be indexed in a knowledge wiki.

Please provide:

1. **Image type** (one line) — e.g., "screenshot of a settings page",
   "architecture diagram", "sequence diagram", "table of results", "photograph".
2. **Visible text** — transcribe every word that appears in the image, preserving
   the original casing. If there is a lot of text, quote it verbatim in a fenced
   block. If there is none, say "None".
3. **Structure and content** — 3-8 sentences describing what the image shows.
   Name components, actors, arrows, decision points. Be specific.
4. **Key facts** — a bullet list of the most important claims a wiki reader
   should extract. One bullet per fact, no more than 8 bullets total.

Write in plain prose. Do not add commentary about your abilities or limitations.
Do not hedge. If the image is unclear or corrupt, say so plainly."""


def describe(
    image_path: Path,
    base_url: str,
    api_key: str,
    model_id: str,
    verify: bool,
    prompt: str,
    limiter=None,
) -> str:
    if not base_url:
        raise SystemExit("ERROR: nano_banana.base_url is empty in .wikirc.json.")
    if not api_key:
        raise SystemExit("ERROR: nano_banana.api_key is empty in .wikirc.json.")

    apply_ssl_env("nano_banana", verify)

    client = genai.Client(
        vertexai=True,
        api_key=api_key,
        http_options=types.HttpOptions(
            base_url=base_url,
            api_version="",
            headers={"Authorization": api_key},
        ),
    )

    try:
        img = Image.open(image_path)
        img.load()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"ERROR: cannot open image {image_path}: {e}")

    contents = [img, prompt]

    if limiter is not None:
        limiter.throttle()

    config = types.GenerateContentConfig(response_modalities=["TEXT"])
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=config,
        )
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            "ERROR: nano-banana-pro request failed: "
            f"{type(e).__name__}: {e}\n"
            "Check nano_banana.base_url, api_key, and network access "
            "(some corporate networks require nano_banana.verify_ssl=false)."
        )

    parts_text: list[str] = []
    for part in response.parts or []:
        if getattr(part, "text", None):
            parts_text.append(part.text)

    text = "\n".join(parts_text).strip()
    if not text:
        raise SystemExit(
            "ERROR: nano-banana-pro returned no text. "
            "Check that vision_model supports image inputs "
            "(gemini-3-pro or similar, NOT -image-preview)."
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Describe an image via nano-banana-pro")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Where to write the .md description")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Override the description prompt (defaults to the wiki-friendly template)",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.wiki_root)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    image_path = args.image.expanduser().resolve()
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 1

    text = describe(
        image_path,
        base_url=cfg.nano_banana_base_url(),
        api_key=cfg.nano_banana_key(),
        model_id=cfg.nano_banana_model(),
        verify=cfg.nano_banana_verify_ssl(),
        prompt=args.prompt or DEFAULT_PROMPT,
        limiter=get_limiter("nano_banana", cfg.nano_banana),
    )

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"# Image description — {image_path.name}\n\n{text}\n",
        encoding="utf-8",
    )
    print(str(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
