#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai",
#     "pillow",
# ]
# ///
"""
Generate images using Google's Gemini image models.

Usage:
    python generate_image.py --prompt "A colorful abstract pattern" --output "./hero.png"
    python generate_image.py --prompt "Minimalist icon" --output "./icon.png" --aspect "16:9"
    python generate_image.py --prompt "Similar style image" --output "./new.png" --reference "./existing.png"
    python generate_image.py --prompt "High quality art" --output "./art.png" --size 1K
"""

import os
os.environ['PYTHONHTTPSVERIFY'] = '0'

# Disable SSL verification warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import ssl
import httpcore
from typing import Optional

# Monkey patch httpcore to disable SSL verification
_original_map_exceptions = httpcore._sync.connection.HTTPConnection._connect

def patched_connect(self, request):
    # Override to disable SSL verification
    return _original_map_exceptions(self, request)

# Disable SSL globally for httpcore
import httpcore._backends.sync
_original_wrap_socket = httpcore._backends.sync.SyncStream.start_tls

def patched_start_tls(self, *args, **kwargs):
    kwargs['ssl_context'] = ssl._create_unverified_context()
    return _original_wrap_socket(self, *args, **kwargs)

httpcore._backends.sync.SyncStream.start_tls = patched_start_tls

import argparse
import sys

from google import genai
from google.genai import types
from PIL import Image

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
        http_options=types.HttpOptions(
            base_url="https://api.ai.public.rakuten-it.com/google-vertexai/v1/",
            api_version="", # Keep it empty so that SDK doesn't overwrite
            headers={
                "Authorization": api_key,
            },
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
        ),
    )
    response = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=config,
    )

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
        required=True,
        default="16:9",
        help="Aspect ratio (default: 16:9)",
    )
    parser.add_argument(
        "--reference",
        help="Path to a reference image for style/composition guidance (optional)",
    )
    parser.add_argument(
        "--size",
        required=True,
        choices=["1K", "2K", "4K"],
        default="1K",
        help="Image resolution (default: 1K)",
    )

    args = parser.parse_args()
    generate_image(args.prompt, args.output, args.aspect, args.reference, args.size)


if __name__ == "__main__":
    main()
