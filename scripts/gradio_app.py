"""Local-only Gradio interface for the Plan A/Plan B invoice experiment."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_parser.paths import (
    DEFAULT_AGENTIC_AB_DIR,
    DEFAULT_RAW_DIR,
    DEFAULT_VECTOR_STORE_DIR,
)
from llm.agent.workflow import judge_ab_artifact, record_ab_verdict, run_agentic_ab_test
from rag.adaptive_rag import DEFAULT_KB, load_kb
from scripts.ocr_text_extraction import SUPPORTED_EXTENSIONS, process_file


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _safe_stem(filename: str) -> str:
    clean = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in filename)
    return clean.strip("_")[:80] or "invoice"


def _uploaded_path(uploaded_file: Any) -> Path | None:
    if uploaded_file is None:
        return None
    if isinstance(uploaded_file, (str, Path)):
        return Path(uploaded_file)
    name = getattr(uploaded_file, "name", None)
    return Path(name) if name else None


def _copy_local(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{_safe_stem(source.stem)}_{uuid.uuid4().hex[:8]}{source.suffix.lower()}"
    shutil.copy2(source, destination)
    return destination


def _prepare_inputs(uploaded_file: Any) -> tuple[Path, Path | None, str]:
    source = _uploaded_path(uploaded_file)
    if source is None:
        raise ValueError("Upload an invoice PDF or image.")
    if source is not None and (not source.exists() or not source.is_file()):
        raise ValueError("The uploaded file is unavailable.")

    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("Use a PDF or supported invoice image.")
    local_source = _copy_local(source, DEFAULT_RAW_DIR)
    ocr_result = process_file(local_source, ocr_quality_threshold=0.0)
    if ocr_result.errors or not ocr_result.selected_text_file:
        details = "; ".join(ocr_result.errors or ["No usable OCR text was produced."])
        raise ValueError(details)
    return (
        Path(ocr_result.selected_text_file),
        Path(ocr_result.searchable_pdf) if ocr_result.searchable_pdf else None,
        source.name,
    )


def run_local_ab(
    uploaded_file: Any,
    enable_plan_b_llm: bool = False,
    plan_b_model: str = "",
    plan_b_api_key: str = "",
) -> tuple[Any, ...]:
    """Run both plans; Plan B model use requires an explicit UI opt-in."""
    try:
        text_path, pdf_path, source_label = _prepare_inputs(uploaded_file)
        selected_key = (plan_b_api_key or os.getenv("GEMINI_API_KEY", "")) if enable_plan_b_llm else ""
        selected_model = (plan_b_model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")).strip()
        result = run_agentic_ab_test(
            text_path,
            pdf_file=pdf_path,
            kb_path=DEFAULT_KB,
            output_dir=DEFAULT_AGENTIC_AB_DIR,
            api_key=selected_key,
            model=selected_model,
        )
    except Exception as exc:
        status = f"Local A/B test failed: {exc}"
        return status, {}, {}, {}, [], "", ""

    plan_a = result.plan_a.model_dump()
    plan_b = result.plan_b.model_dump()
    trace = [event.model_dump() for event in result.plan_b_trace]
    status = (
        f"Completed locally for {source_label}. "
        f"Plan A route: {result.plan_a.route}; Plan B route: {result.plan_b.route}; "
        f"external LLM used: {str(result.plan_b.llm_used).lower()}."
    )
    return status, plan_a, plan_b, result.comparison, trace, result.artifact_path or "", result.artifact_path or ""


def save_verdict(artifact_path: str, preferred_plan: str, note: str) -> str:
    if not artifact_path:
        return "Run an A/B test before saving a verdict."
    try:
        result = record_ab_verdict(artifact_path, preferred_plan, note)
    except Exception as exc:
        return f"Could not save the local verdict: {exc}"
    return f"Saved verdict '{result.evaluation['preferred_plan']}' in {artifact_path}."


def run_judge(artifact_path: str, base_url: str, model: str, api_key: str) -> tuple[str, dict[str, Any]]:
    """Run the optional judge only after an explicit local UI action."""
    if not artifact_path:
        return "Run an A/B test before invoking the judge.", {}
    selected_url = (base_url or os.getenv("JUDGE_BASE_URL", "")).strip()
    selected_model = (model or os.getenv("JUDGE_MODEL", "")).strip()
    selected_key = api_key or os.getenv("JUDGE_API_KEY", "")
    try:
        result = judge_ab_artifact(
            artifact_path,
            base_url=selected_url,
            api_key=selected_key,
            model=selected_model,
        )
    except Exception as exc:
        return f"LLM judge could not run: {exc}", {}
    judge = result.llm_judge
    if judge is None:
        return "LLM judge produced no result.", {}
    if judge.status != "completed":
        return f"LLM judge status: {judge.status}. {judge.summary}", judge.model_dump()
    return (
        f"LLM judge recommends {judge.preferred_plan} with confidence {judge.confidence}. "
        "A human verdict is still required.",
        judge.model_dump(),
    )


def knowledge_base_view() -> tuple[list[list[Any]], dict[str, Any]]:
    kb = load_kb(DEFAULT_KB)
    rows: list[list[Any]] = []
    for provider_id, provider in sorted(kb.get("providers", {}).items()):
        rows.append(
            [
                provider_id,
                provider.get("provider_name", provider_id),
                len(provider.get("provider_specific_extraction_tips", [])),
                len(provider.get("human_reviewer_feedback", [])),
                len(provider.get("previously_validated_invoices", [])),
            ]
        )
    return rows, kb


def rebuild_vector_index() -> str:
    try:
        from vector_store.base import build_index

        build_index(kb_path=DEFAULT_KB, index_path=DEFAULT_VECTOR_STORE_DIR, force_rebuild=True)
    except Exception as exc:
        return f"Vector index was not built: {exc}"
    return f"FAISS/LlamaIndex provider index rebuilt locally at {DEFAULT_VECTOR_STORE_DIR}."


def build_demo() -> Any:
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/billing-matplotlib")
    import gradio as gr

    with gr.Blocks(title="Agentic Invoice A/B Lab") as demo:
        gr.Markdown(
            "# Agentic Invoice A/B Lab\n"
            "Runs on this computer. Plan A uses OCR and deterministic extraction. "
            "Plan B uses the five-agent supervisor workflow and provider knowledge base. "
            "The supported categories are electricity, water, natural gas, and telecom; "
            "other documents are rejected. Plan B extraction and the independent judge call a model only "
            "when you explicitly enable the corresponding action."
        )
        with gr.Tab("Plan A / Plan B"):
            invoice = gr.File(
                label="Invoice PDF or image",
                file_types=[".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"],
                type="filepath",
            )
            with gr.Accordion("Optional Plan B Gemini extraction", open=False):
                gr.Markdown(
                    "When enabled, Plan B sends the invoice PDF, OCR evidence, and provider RAG context to "
                    "Gemini. Leave it disabled for a fully offline OCR + agent-rules comparison."
                )
                enable_plan_b_llm = gr.Checkbox(label="Enable Gemini for Plan B", value=False)
                plan_b_model = gr.Textbox(
                    value=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
                    label="Plan B Gemini model",
                )
                plan_b_api_key = gr.Textbox(label="Gemini API key", type="password")
            run_button = gr.Button("Run local A/B test", variant="primary")
            status = gr.Markdown()
            with gr.Row():
                plan_a = gr.JSON(label="Plan A")
                plan_b = gr.JSON(label="Plan B")
            comparison = gr.JSON(label="Comparison")
            trace = gr.JSON(label="Plan B agent trace")
            artifact = gr.Textbox(label="Local result artifact", interactive=False)
            artifact_state = gr.State("")

            run_button.click(
                run_local_ab,
                inputs=[invoice, enable_plan_b_llm, plan_b_model, plan_b_api_key],
                outputs=[status, plan_a, plan_b, comparison, trace, artifact, artifact_state],
            )

            gr.Markdown("### Human accuracy verdict")
            verdict = gr.Dropdown(
                choices=["plan_a", "plan_b", "tie", "inconclusive"],
                value="inconclusive",
                label="Preferred result",
            )
            verdict_note = gr.Textbox(label="Reviewer note")
            verdict_button = gr.Button("Save verdict locally")
            verdict_status = gr.Markdown()
            verdict_button.click(
                save_verdict,
                inputs=[artifact_state, verdict, verdict_note],
                outputs=verdict_status,
            )

            with gr.Accordion("Optional independent LLM judge", open=False):
                gr.Markdown(
                    "The judge compares both outputs only with OCR evidence. The invoice text is sent to the "
                    "configured endpoint only when you click **Run LLM judge**. Its recommendation does not "
                    "replace the human verdict."
                )
                judge_base_url = gr.Textbox(
                    value=os.getenv("JUDGE_BASE_URL", ""),
                    label="OpenAI-compatible base URL",
                    placeholder="http://127.0.0.1:8000/v1",
                )
                judge_model = gr.Textbox(
                    value=os.getenv("JUDGE_MODEL", ""),
                    label="Judge model",
                    placeholder="your-served-model-name",
                )
                judge_api_key = gr.Textbox(label="Judge API key (if required)", type="password")
                judge_button = gr.Button("Run LLM judge")
                judge_status = gr.Markdown()
                judge_result = gr.JSON(label="Advisory judge result")
                judge_button.click(
                    run_judge,
                    inputs=[artifact_state, judge_base_url, judge_model, judge_api_key],
                    outputs=[judge_status, judge_result],
                )

        with gr.Tab("Provider knowledge base"):
            gr.Markdown(
                "Provider memories remain separate. Unknown suppliers may be added through reviewed invoice feedback."
            )
            refresh_button = gr.Button("Refresh knowledge base")
            provider_table = gr.Dataframe(
                headers=["Provider ID", "Provider", "Tips", "Feedback", "Validated invoices"],
                datatype=["str", "str", "number", "number", "number"],
                interactive=False,
            )
            kb_json = gr.JSON(label="Local provider memory")
            index_button = gr.Button("Rebuild local FAISS index")
            index_status = gr.Markdown()
            refresh_button.click(knowledge_base_view, outputs=[provider_table, kb_json])
            index_button.click(rebuild_vector_index, outputs=index_status)
            demo.load(knowledge_base_view, outputs=[provider_table, kb_json])

    return demo


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Gradio A/B invoice lab.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--inbrowser", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    container_bind_allowed = os.getenv("ALLOW_CONTAINER_BIND") == "1" and args.host == "0.0.0.0"
    if args.host not in LOCAL_HOSTS and not container_bind_allowed:
        raise SystemExit("Local-only mode requires --host 127.0.0.1, localhost, or ::1.")
    print(f"Local Gradio server: http://{args.host}:{args.port}")
    build_demo().launch(
        server_name=args.host,
        server_port=args.port,
        share=False,
        inbrowser=args.inbrowser,
    )


if __name__ == "__main__":
    main()
