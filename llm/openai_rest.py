"""Small dependency-free OpenAI Responses API client for invoice experiments."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib import error, request


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_INLINE_PDF_BYTES = 50 * 1024 * 1024


def _response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty response.")
    return text


def _pdf_content(pdf_file: str | Path) -> dict[str, str]:
    path = Path(pdf_file)
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"PDF file does not exist: {path}")
    data = path.read_bytes()
    if len(data) > MAX_INLINE_PDF_BYTES:
        raise RuntimeError("PDF is too large for the inline OpenAI request (50 MB maximum).")
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "input_file",
        "filename": path.name,
        "file_data": f"data:application/pdf;base64,{encoded}",
    }


def create_response(
    *,
    prompt: str,
    api_key: str,
    model: str,
    timeout: float = 90.0,
    pdf_file: str | Path | None = None,
    max_output_tokens: int = 4000,
) -> str:
    """Call OpenAI without persisting the response or exposing the key in the URL."""
    if not api_key.strip():
        raise ValueError("An OpenAI API key is required.")
    if not model.strip():
        raise ValueError("An OpenAI model is required.")

    content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
    if pdf_file is not None:
        content.append(_pdf_content(pdf_file))
    body = json.dumps(
        {
            "model": model.strip(),
            "store": False,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": max_output_tokens,
        }
    ).encode("utf-8")
    req = request.Request(
        OPENAI_RESPONSES_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        explanations = {
            400: "Check the selected model and invoice file.",
            401: "The key is invalid, expired, or revoked.",
            403: "The project key lacks access to this model or endpoint.",
            429: "The project quota or rate limit was reached.",
        }
        detail = explanations.get(exc.code, "The API request was rejected.")
        raise RuntimeError(f"OpenAI returned HTTP {exc.code}. {detail}") from None
    except error.URLError as exc:
        raise RuntimeError("OpenAI could not be reached from this server.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenAI returned an invalid response envelope.") from exc
    return _response_text(payload)
