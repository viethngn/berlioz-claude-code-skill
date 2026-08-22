#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai",
#     "pillow",
#     "httpx",
# ]
# ///
"""
Generate images using Google's Gemini image models.

Usage:
    uv run image.py --prompt "A colorful abstract pattern" --output "./hero.png"
    uv run image.py --prompt "Minimalist icon" --output "./icon.png" --aspect "16:9"
    uv run image.py --prompt "Similar style image" --output "./new.png" --reference "./existing.png"
    uv run image.py --prompt "High quality art" --output "./art.png" --size 1K
"""

import argparse
import os
import sys
from typing import Optional

from google import genai
from google.genai import types
from PIL import Image

# TLS verification is on by default. Some corporate networks intercept TLS
# with their own CA; rather than disabling verification for the whole
# process (which would affect every TLS connection this interpreter makes,
# not just Gemini's), set NANO_BANANA_INSECURE_SSL=1 to scope the bypass to
# this script's own HTTP client only.
INSECURE_SSL = os.environ.get("NANO_BANANA_INSECURE_SSL", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _http_options(base_url: str, api_key: str) -> types.HttpOptions:
    kwargs = dict(
        base_url=base_url,
        api_version="",  # Keep it empty so that SDK doesn't overwrite
        headers={
            "Authorization": api_key,
        },
    )
    if INSECURE_SSL:
        import httpx
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["httpx_client"] = httpx.Client(verify=False)
    return types.HttpOptions(**kwargs)


def generate_image(
    prompt: str,
    output_path: str,
    aspect: str = "16:9",
    reference: Optional[str] = None,
    size: str = "1K",
) -> None:
    """Generate an image using Gemini and save to output_path."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(
        vertexai=True,
        api_key=api_key,
        http_options=_http_options(
            "https://api.ai.public.rakuten-it.com/google-vertexai/v1/", api_key
        ),
    )

    full_prompt = f"{prompt}"

    # Build contents with optional reference image
    contents: list = []
    if reference:
        if not os.path.exists(reference):
            print(f"Error: Reference image not found: {reference}", file=sys.stderr)
            sys.exit(1)
        ref_image = Image.open(reference)
        contents.append(ref_image)
        full_prompt = f"{full_prompt} Use the provided image as a reference for style, composition, or content."
    contents.append(full_prompt)

    model_id = "gemini-3-pro-image-preview"

    # Pro model supports additional config for resolution
    config = types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=aspect,
            image_size=size,
        ),
    )
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=config,
        )
    except Exception as e:  # noqa: BLE001 — surface a clean error, not a traceback
        print(f"Error: image generation request failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Extract image from response
    for part in response.parts:
        if part.text is not None:
            print(f"Model response: {part.text}")
        elif part.inline_data is not None:
            image = part.as_image()
            image.save(output_path)
            print(f"Image saved to: {output_path}")
            return

    print("Error: No image data in response", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Generate images using Gemini 3 Pro"
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="Description of the image to generate",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output file path (PNG format)",
    )
    parser.add_argument(
        "--aspect",
        default="16:9",
        help="Aspect ratio (default: 16:9)",
    )
    parser.add_argument(
        "--reference",
        help="Path to a reference image for style/composition guidance (optional)",
    )
    parser.add_argument(
        "--size",
        choices=["1K", "2K", "4K"],
        default="1K",
        help="Image resolution (default: 1K)",
    )

    args = parser.parse_args()
    generate_image(args.prompt, args.output, args.aspect, args.reference, args.size)


if __name__ == "__main__":
    main()
