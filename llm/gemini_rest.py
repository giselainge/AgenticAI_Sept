"""Small Gemini REST fallback that keeps API keys out of URLs and artifacts."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib import error, parse, request


GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_INLINE_PDF_BYTES = 50 * 1024 * 1024
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def generate_content(
    *,
    prompt: str,
    api_key: str,
    model: str,
    timeout: float = 90.0,
    pdf_file: str | Path | None = None,
    response_mime_type: str = "text/plain",
) -> str:
    """Call ``generateContent`` without requiring the Google Python SDK."""
    selected_model = model.strip()
    if not api_key:
        raise ValueError("A Gemini API key is required.")
    if not selected_model or not MODEL_NAME_RE.fullmatch(selected_model):
        raise ValueError("Gemini model name is missing or invalid.")

    parts: list[dict[str, object]] = [{"text": prompt}]
    if pdf_file is not None:
        pdf_path = Path(pdf_file)
        if not pdf_path.exists() or not pdf_path.is_file():
            raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")
        pdf_bytes = pdf_path.read_bytes()
        if len(pdf_bytes) > MAX_INLINE_PDF_BYTES:
            raise ValueError("PDF exceeds Gemini's 50 MB inline-document limit.")
        parts.append(
            {
                "inline_data": {
                    "mime_type": "application/pdf",
                    "data": base64.b64encode(pdf_bytes).decode("ascii"),
                }
            }
        )

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": response_mime_type,
        },
    }
    url = f"{GEMINI_API_ROOT}/{parse.quote(selected_model, safe='-._')}:generateContent"
    gemini_request = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with request.urlopen(gemini_request, timeout=timeout) as response:
            envelope = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"Gemini API returned HTTP {exc.code}.") from None
    except error.URLError as exc:
        raise RuntimeError("Gemini API could not be reached.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Gemini API returned an invalid response envelope.") from exc

    try:
        response_parts = envelope["candidates"][0]["content"]["parts"]
        text = "\n".join(
            str(part.get("text", ""))
            for part in response_parts
            if isinstance(part, dict) and part.get("text")
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini API response did not contain generated text.") from exc
    if not text.strip():
        raise RuntimeError("Gemini API returned an empty response.")
    return text
