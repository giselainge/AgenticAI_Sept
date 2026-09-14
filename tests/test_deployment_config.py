from pathlib import Path

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
    assert "USER app" in dockerfile
    assert "ENTRYPOINT [\"/usr/bin/tini\", \"--\"]" in dockerfile
    assert "COPY .env" not in dockerfile


def test_compose_runs_api_and_dashboard_with_persistent_runtime_paths() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"api", "dashboard"}
    for service in compose["services"].values():
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert "./data:/app/data" in service["volumes"]
        assert "./runtime:/app/runtime" in service["volumes"]
        assert service["environment"]["RAG_KB_PATH"] == "/app/runtime/knowledge_base.json"
    assert "127.0.0.1" in compose["services"]["api"]["ports"][0]
    assert "scripts/dashboard.py" in compose["services"]["dashboard"]["command"]


def test_cloudformation_stack_is_restricted_managed_and_self_deleting() -> None:
    template_path = PROJECT_ROOT / "deploy" / "aws" / "ephemeral-stack.yaml"
    template = yaml.load(template_path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    resources = template["Resources"]

    assert resources["ApplicationInstance"]["Properties"]["MetadataOptions"]["HttpTokens"] == "required"
    assert "AmazonSSMManagedInstanceCore" in resources["InstanceRole"]["Properties"]["ManagedPolicyArns"][0]
    ingress = resources["ApplicationSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert {rule["FromPort"] for rule in ingress} == {8000, 8501}
    assert all(rule["CidrIp"] == {"Ref": "AllowedCidr"} for rule in ingress)
    assert resources["CleanupSchedule"]["Type"] == "AWS::Scheduler::Schedule"
    assert resources["CleanupSchedule"]["Properties"]["ActionAfterCompletion"] == "DELETE"
    cleanup_code = resources["CleanupFunction"]["Properties"]["Code"]["ZipFile"]
    assert "delete_stack" in cleanup_code
    assert "delete_objects" in cleanup_code


def test_deployment_bundle_excludes_secrets_private_data_and_local_memory() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    deploy_script = (PROJECT_ROOT / "deploy" / "aws" / "deploy.ps1").read_text(encoding="utf-8")

    assert ".env" in dockerignore
    assert "data" in dockerignore
    assert "rag/knowledge_base.json" in dockerignore
    assert '--exclude="rag/knowledge_base.json"' in deploy_script
    assert "GEMINI_API_KEY" not in deploy_script
    assert "0.0.0.0/0" in deploy_script  # explicitly rejected by the script
    assert "Refusing a public-to-everyone deployment" in deploy_script
