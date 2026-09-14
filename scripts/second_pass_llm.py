from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from llm.agent.models import (
    GeminiCaller,
    GeminiExtractionPayload,
    GeminiPromptContext,
    NormalizedInvoiceExtraction,
    RagSnippet,
    SecondPassArtifactPaths,
    SecondPassResult,
)
from llm.agent.prompts import GEMINI_EXTRACTION_PROMPT_TEMPLATE
from llm.gemini_rest import generate_content as generate_gemini_content
from llm.openai_rest import create_response as create_openai_response
from invoice_parser.paths import DEFAULT_LLM_SECOND_PASS_DIR
from invoice_parser.schema import FIELDNAMES, NULL_VALUE
from invoice_parser.text_utils import normalize_money, normalize_space


DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra").strip() or "gpt-5.6-terra"

VALID_INVOICE_TYPES = {"electricity", "water", "natural gas", "telecom", "unsupported"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONEY_FIELDS = {"subtotal_value", "total_vat", "total_value"}
DATE_FIELDS = {"invoice_date", "payment_due_date", "consumption_start_date", "consumption_end_date"}
CRITICAL_FIELDS = [
    "invoice_type",
    "invoice_number",
    "invoice_date",
    "currency",
    "provider_name",
    "provider_vat_number",
    "total_value",
]


def build_gemini_prompt(
    ocr_text: str = "",
    provider_name: str | None = None,
    invoice_type: str | None = None,
    rag_snippets: list[dict[str, Any] | RagSnippet] | None = None,
) -> str:
    context = GeminiPromptContext(
        ocr_text=ocr_text,
        provider_name=provider_name,
        invoice_type=invoice_type,
        rag_snippets=[RagSnippet.model_validate(snippet) for snippet in rag_snippets or []],
    )
    context_lines = []
    for snippet in context.rag_snippets:
        text = normalize_space(snippet.text)
        if text:
            label = normalize_space(snippet.provider or snippet.type or "context")
            context_lines.append(f"- {label}: {text}")

    return GEMINI_EXTRACTION_PROMPT_TEMPLATE.format(
        provider_name=context.provider_name or "unknown",
        invoice_type=context.invoice_type or "unknown",
        schema_fields=", ".join(FIELDNAMES),
        rag_context="\n".join(context_lines) if context_lines else "- none",
        ocr_text=context.ocr_text,
    )


def parse_model_text(response_text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    unknown_lines: list[str] = []
    allowed_fields = set(FIELDNAMES) | {"confidence", "uncertain_fields"}
    for raw_line in (response_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", "*", "|")):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$", line)
        if not match:
            unknown_lines.append(line)
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key not in allowed_fields:
            unknown_lines.append(line)
            continue
        if key == "uncertain_fields":
            payload[key] = [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
        elif key == "confidence":
            payload[key] = {}
        else:
            payload[key] = value
    if not payload:
        raise ValueError("Gemini response did not contain parseable key-value text.")
    if unknown_lines:
        payload["extraction_warnings"] = normalize_space(
            " | ".join([str(payload.get("extraction_warnings") or ""), "Ignored unparseable Gemini text lines."])
        ).strip(" |")
    return payload


def _null_if_missing(value: Any) -> str:
    if value is None:
        return NULL_VALUE
    value = normalize_space(str(value))
    return value if value else NULL_VALUE


def _normalize_field(field: str, value: Any) -> str:
    if field in MONEY_FIELDS:
        normalized = normalize_money(None if value is None else str(value))
        return normalized if normalized is not None else NULL_VALUE
    if field == "currency":
        value = _null_if_missing(value)
        return value.upper() if value != NULL_VALUE else NULL_VALUE
    if field == "valid_invoice":
        if isinstance(value, bool):
            return str(value).lower()
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"true", "false"} else "true"
    return _null_if_missing(value)


def _validate_structured(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    missing = [field for field in CRITICAL_FIELDS if data.get(field, NULL_VALUE) == NULL_VALUE]
    invoice_type = data.get("invoice_type", NULL_VALUE)
    if invoice_type != NULL_VALUE and invoice_type not in VALID_INVOICE_TYPES:
        errors.append("invoice_type must be electricity, water, natural gas, telecom, or unsupported.")
    for field in DATE_FIELDS:
        value = data.get(field, NULL_VALUE)
        if value != NULL_VALUE and not DATE_RE.fullmatch(str(value)):
            errors.append(f"{field} must use YYYY-MM-DD.")
    for field in MONEY_FIELDS:
        value = data.get(field, NULL_VALUE)
        if value != NULL_VALUE and normalize_money(str(value)) is None:
            errors.append(f"{field} must be numeric.")
    start = data.get("consumption_start_date", NULL_VALUE)
    end = data.get("consumption_end_date", NULL_VALUE)
    if start != NULL_VALUE and end != NULL_VALUE and DATE_RE.fullmatch(start) and DATE_RE.fullmatch(end) and start > end:
        errors.append("consumption_start_date cannot be after consumption_end_date.")
    return errors, missing


def normalize_structured_output(payload: dict[str, Any], source_file: str | Path | None = None) -> dict[str, Any]:
    typed_payload = GeminiExtractionPayload.model_validate(payload)
    output = {field: _normalize_field(field, value) for field, value in typed_payload.invoice_fields().items()}
    if source_file and output["source_file"] == NULL_VALUE:
        output["source_file"] = str(source_file)
    uncertain = [str(field) for field in typed_payload.uncertain_fields if str(field)]
    validation_errors, missing_fields = _validate_structured(output)
    missing_fields = sorted(set(missing_fields + uncertain))
    output["confidence"] = typed_payload.confidence
    output["uncertain_fields"] = uncertain
    output["missing_fields"] = missing_fields
    output["validation_errors"] = validation_errors
    output["review_status"] = "manual_review_required" if validation_errors or missing_fields or uncertain else "ready_for_review"
    return NormalizedInvoiceExtraction.model_validate(output).as_artifact_dict()


def _call_gemini_with_sdk(
    prompt: str,
    api_key: str,
    model: str,
    timeout: int,
    *,
    pdf_file: str | Path | None = None,
) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return generate_gemini_content(
            prompt=prompt,
            api_key=api_key,
            model=model,
            timeout=timeout,
            pdf_file=pdf_file,
            response_mime_type="text/plain",
        )

    if pdf_file is None:
        raise RuntimeError("PDF file is required for Gemini SDK extraction.")
    pdf_path = Path(pdf_file)
    if not pdf_path.exists():
        raise RuntimeError(f"PDF file does not exist: {pdf_path}")

    http_options = types.HttpOptions(timeout=max(1, int(timeout)) * 1000)
    client = genai.Client(api_key=api_key, http_options=http_options)
    response = client.models.generate_content(
        model=model,
        contents=[
            prompt,
            types.Part.from_bytes(data=pdf_path.read_bytes(), mime_type="application/pdf"),
        ],
        config=types.GenerateContentConfig(response_mime_type="text/plain"),
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return str(text)


def second_pass_extract(
    ocr_text: str = "",
    *,
    pdf_file: str | Path | None = None,
    provider_name: str | None = None,
    invoice_type: str | None = None,
    source_file: str | Path | None = None,
    api_key: str | None = None,
    model: str | None = None,
    output_dir: str | Path = DEFAULT_LLM_SECOND_PASS_DIR,
    rag_snippets: list[dict[str, Any] | RagSnippet] | None = None,
    kb_path: str | Path | None = None,
    timeout: int = 60,
    gemini_caller: GeminiCaller | None = None,
    provider: str = "gemini",
    openai_caller: GeminiCaller | None = None,
) -> SecondPassResult:
    selected_provider = "openai" if provider == "openai" else "gemini"
    default_model = DEFAULT_OPENAI_MODEL if selected_provider == "openai" else DEFAULT_MODEL
    model_environment = "OPENAI_MODEL" if selected_provider == "openai" else "GEMINI_MODEL"
    key_environment = "OPENAI_API_KEY" if selected_provider == "openai" else "GEMINI_API_KEY"
    selected_model = model or os.environ.get(model_environment) or default_model
    result = SecondPassResult(provider=selected_provider, model=selected_model)

    if pdf_file is None:
        result.errors.append(f"PDF file is required for {selected_provider.title()} second-pass extraction.")
        return result
    pdf_path = Path(pdf_file)
    if not pdf_path.exists():
        result.errors.append(f"PDF file does not exist: {pdf_path}")
        return result

    selected_api_key = api_key if api_key is not None else os.environ.get(key_environment, "")
    if not selected_api_key:
        result.errors.append(f"Missing {key_environment} for {selected_provider.title()} second-pass extraction.")
        return result

    if rag_snippets is None:
        try:
            from rag.adaptive_rag import build_llm_rag_snippets

            rag_kwargs: dict[str, Any] = {}
            if kb_path is not None:
                rag_kwargs["kb_path"] = kb_path
            rag_snippets = build_llm_rag_snippets(
                query_text=ocr_text,
                provider_hint=provider_name or "",
                invoice_type=invoice_type or "",
                **rag_kwargs,
            )
        except Exception as exc:
            result.warnings.append(f"RAG context skipped: {exc}")
            rag_snippets = []

    prompt = build_gemini_prompt(
        ocr_text=ocr_text,
        provider_name=provider_name,
        invoice_type=invoice_type,
        rag_snippets=rag_snippets,
    )
    try:
        if selected_provider == "openai" and openai_caller is not None:
            try:
                raw_response = openai_caller(prompt, selected_api_key, selected_model, timeout, pdf_file=pdf_path)
            except TypeError:
                raw_response = openai_caller(prompt, selected_api_key, selected_model, timeout)
        elif selected_provider == "openai":
            raw_response = create_openai_response(
                prompt=prompt,
                api_key=selected_api_key,
                model=selected_model,
                timeout=timeout,
                pdf_file=pdf_path,
            )
        elif gemini_caller is not None:
            try:
                raw_response = gemini_caller(prompt, selected_api_key, selected_model, timeout, pdf_file=pdf_path)
            except TypeError:
                raw_response = gemini_caller(prompt, selected_api_key, selected_model, timeout)
        else:
            raw_response = _call_gemini_with_sdk(prompt, selected_api_key, selected_model, timeout, pdf_file=pdf_path)
    except Exception as exc:
        result.errors.append(str(exc))
        return result

    result.raw_response = raw_response
    try:
        payload = parse_model_text(raw_response)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    parsed = normalize_structured_output(payload, source_file=source_file or pdf_path.name)
    result.used = True
    result.parsed = parsed
    result.text = "\n".join(f"{field}: {parsed.get(field, NULL_VALUE)}" for field in FIELDNAMES)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem
    paths = SecondPassArtifactPaths(
        raw_response_path=output_path / f"{stem}_{selected_provider}_raw.txt",
        normalized_output_path=output_path / f"{stem}_{selected_provider}_structured.json",
    )
    paths.raw_response_path.write_text(raw_response, encoding="utf-8")
    paths.normalized_output_path.write_text(_json_dumps(parsed), encoding="utf-8")
    result.raw_response_path = str(paths.raw_response_path)
    result.normalized_output_path = str(paths.normalized_output_path)
    return result


def _json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, indent=2, ensure_ascii=False)


def run_llm_second_pass(
    *,
    source_file: str | Path | None = None,
    pdf_file: str | Path | None = None,
    ocr_text: str = "",
    ocr_quality: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    kb_path: str | Path | None = None,
) -> SecondPassResult:
    _ = ocr_quality
    return second_pass_extract(
        ocr_text=ocr_text,
        pdf_file=pdf_file,
        source_file=source_file,
        api_key=api_key,
        model=model,
        kb_path=kb_path,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Gemini PDF second-pass invoice extraction.")
    parser.add_argument("--pdf-file", required=True)
    parser.add_argument("--text-file")
    parser.add_argument("--provider")
    parser.add_argument("--invoice-type")
    parser.add_argument("--output-dir", default=str(DEFAULT_LLM_SECOND_PASS_DIR))
    return parser


def main() -> SecondPassResult:
    args = build_arg_parser().parse_args()
    ocr_text = Path(args.text_file).read_text(encoding="utf-8", errors="replace") if args.text_file else ""
    result = second_pass_extract(
        ocr_text=ocr_text,
        pdf_file=args.pdf_file,
        provider_name=args.provider,
        invoice_type=args.invoice_type,
        output_dir=args.output_dir,
    )
    if result.errors:
        print("Gemini second pass failed: " + " | ".join(result.errors))
    else:
        print(f"Gemini structured output: {result.normalized_output_path}")
    return result


if __name__ == "__main__":
    main()
