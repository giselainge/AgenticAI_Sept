from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility for the current local test host.
    import tomli as tomllib

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CloudFormationLoader(yaml.SafeLoader):
    pass


def _cloudformation_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    return {tag_suffix: loader.construct_mapping(node)}


CloudFormationLoader.add_multi_constructor("!", _cloudformation_tag)


def test_docker_image_has_ocr_runtime_and_non_root_user() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "AS python-dependencies" in dockerfile
    assert "tesseract-ocr-por" in dockerfile
    assert "ghostscript" in dockerfile
    assert "libgomp1" in dockerfile
    assert "USER app" in dockerfile
    assert "ENTRYPOINT [\"/usr/bin/tini\", \"--\"]" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "COPY .env" not in dockerfile
    assert "COPY --chown=${APP_UID}:${APP_GID} vector_store ./vector_store" in dockerfile
    assert "OCR_LANGUAGES=por+eng" in dockerfile
    assert "HEALTHCHECK_PATH=/ready" in dockerfile


def test_compose_runs_api_and_dashboard_with_persistent_runtime_paths() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"api", "dashboard", "gradio"}
    for service in compose["services"].values():
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert "./data:/app/data" in service["volumes"]
        assert "./runtime:/app/runtime" in service["volumes"]
        assert service["environment"]["RAG_KB_PATH"] == "/app/runtime/knowledge_base.json"
        assert "GEMINI_API_KEY" not in service["environment"]
        assert "JUDGE_API_KEY" not in service["environment"]
        assert service["environment"]["OCR_LANGUAGES"] == "${OCR_LANGUAGES:-por+eng}"
        assert service["platform"] == "linux/amd64"
    assert "127.0.0.1" in compose["services"]["api"]["ports"][0]
    assert "scripts/dashboard.py" in compose["services"]["dashboard"]["command"]
    assert "scripts/gradio_app.py" in compose["services"]["gradio"]["command"]
    assert "127.0.0.1" in compose["services"]["gradio"]["ports"][0]


def test_cloudformation_stack_is_restricted_managed_and_self_deleting() -> None:
    template_path = PROJECT_ROOT / "deploy" / "aws" / "ephemeral-stack.yaml"
    template = yaml.load(template_path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    resources = template["Resources"]

    assert resources["ApplicationInstance"]["Properties"]["MetadataOptions"]["HttpTokens"] == "required"
    assert "AmazonSSMManagedInstanceCore" in resources["InstanceRole"]["Properties"]["ManagedPolicyArns"][0]
    ingress = resources["ApplicationSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert {rule["FromPort"] for rule in ingress} == {7860, 8000, 8501}
    assert all(rule["CidrIp"] == {"Ref": "AllowedCidr"} for rule in ingress)
    assert resources["CleanupSchedule"]["Type"] == "AWS::Scheduler::Schedule"
    assert resources["CleanupSchedule"]["Properties"]["ActionAfterCompletion"] == "DELETE"
    cleanup_code = resources["CleanupFunction"]["Properties"]["Code"]["ZipFile"]
    assert "delete_stack" in cleanup_code
    assert "delete_objects" in cleanup_code


def test_deployment_transfers_the_tested_image_without_secrets_or_private_data() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    deploy_script = (PROJECT_ROOT / "deploy" / "aws" / "deploy.ps1").read_text(encoding="utf-8")
    deployment_files = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "deploy" / "aws").iterdir()
        if path.is_file()
    )

    assert ".env" in dockerignore
    assert "data" in dockerignore
    assert "data/" in gitignore
    assert "rag/knowledge_base.json" in dockerignore
    assert "docker save --output" in deploy_script
    assert "application-image.tar" in deploy_script
    assert "Get-FileHash -Algorithm SHA256" in deploy_script
    assert "docker load --input" in deploy_script
    assert "docker build" not in deploy_script
    assert "requirements.txt" not in deploy_script
    assert "GEMINI_API_KEY" not in deploy_script
    assert "JUDGE_API_KEY" not in deploy_script
    assert "iseg" not in deployment_files.lower()
    assert "ec2-108-132-55-75" not in deployment_files
    assert "HEALTHCHECK_PORT=8501" in deploy_script
    assert "HEALTHCHECK_PATH=/" in deploy_script
    assert "billing-gradio" in deploy_script
    assert "ALLOW_CONTAINER_BIND=1" in deploy_script
    assert "scripts/runtime_check.py --strict --expect-fingerprint" in deploy_script
    assert "0.0.0.0/0" in deploy_script  # explicitly rejected by the script
    assert "Refusing a public-to-everyone deployment" in deploy_script


def test_local_parity_script_builds_and_checks_the_same_image() -> None:
    script = (PROJECT_ROOT / "deploy" / "local.ps1").read_text(encoding="utf-8")

    assert "docker compose build --pull" in script
    assert "scripts/runtime_check.py --strict --fingerprint-only" in script
    assert "agentic-ai-billing-agent:local" in script
    assert "http://127.0.0.1:7860/" in script


def test_uv_metadata_is_the_only_python_dependency_source() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["requires-python"] == ">=3.13,<3.14"
    assert metadata["tool"]["uv"]["package"] is False
    assert any(item.startswith("google-genai") for item in metadata["project"]["dependencies"])
    assert any(item.startswith("gradio") for item in metadata["project"]["dependencies"])
    assert any(item.startswith("faiss-cpu") for item in metadata["project"]["dependencies"])
    assert any(item.startswith("llama-index-core") for item in metadata["project"]["dependencies"])
    assert any(item.startswith("llama-index-vector-stores-faiss") for item in metadata["project"]["dependencies"])
    assert any(item.startswith("torch") for item in metadata["project"]["dependencies"])
    assert all("hugging" not in item.lower() for item in metadata["project"]["dependencies"])
    lock_text = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8").lower()
    assert 'name = "sentence-transformers"' not in lock_text
    assert 'name = "transformers"' not in lock_text
    assert 'name = "llama-index-embeddings-huggingface"' not in lock_text
    assert "optional-dependencies" not in metadata["project"]
    assert any(item.startswith("pytest") for item in metadata["dependency-groups"]["dev"])
    assert not (PROJECT_ROOT / "requirements.txt").exists()
    assert not (PROJECT_ROOT / "requirements-dev.txt").exists()
