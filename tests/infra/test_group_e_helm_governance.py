"""
Group E Helm governance regression tests.

These tests cover:
- E1 image tag fail-fast behavior: Helm values must not default to `latest`;
  templates must require explicit image tags.
- E2 optional Ollama behavior: Ollama is disabled by default and only renders
  OLLAMA_* environment variables when explicitly enabled.
- Dead Helm values cleanup: values that are not consumed by templates are not
  kept as misleading operator-facing configuration.

Most tests are static so they can run in the Python lint job. If a `helm`
binary is available, the optional tests also validate real Helm rendering.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELM_DIR = ROOT / "deploy" / "helm" / "enterprise-crm"
VALUES_FILES = [
    HELM_DIR / "values.yaml",
    HELM_DIR / "values-staging.yaml",
    HELM_DIR / "values-production.yaml",
]
TEMPLATES_DIR = HELM_DIR / "templates"
TEMPLATE_FILES = {
    "gateway": TEMPLATES_DIR / "gateway.yaml",
    "agents": TEMPLATES_DIR / "agents.yaml",
    "frontend": TEMPLATES_DIR / "frontend.yaml",
}
CI_CD_YML = ROOT / ".github" / "workflows" / "ci-cd.yml"

TAG_SET_ARGS = [
    "--set",
    "images.frontend.tag=ci-test",
    "--set",
    "images.gateway.tag=ci-test",
    "--set",
    "images.agents.tag=ci-test",
]


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: expected a YAML mapping"
    return data


class TestImageTagGovernance:
    @pytest.mark.parametrize("values_file", VALUES_FILES, ids=lambda p: p.name)
    def test_values_image_tags_are_empty_not_latest(self, values_file: Path) -> None:
        data = _load_yaml(values_file)
        images = data.get("images", {})

        for service in ("frontend", "gateway", "agents"):
            tag = images.get(service, {}).get("tag")
            assert tag == "", (
                f"{values_file.name}: images.{service}.tag must default to an "
                f"empty string and be supplied by CI/operator, got {tag!r}"
            )

    @pytest.mark.parametrize("values_file", VALUES_FILES, ids=lambda p: p.name)
    def test_values_do_not_contain_tag_latest(self, values_file: Path) -> None:
        text = values_file.read_text(encoding="utf-8")
        assert not re.search(r"(?m)^\s*tag:\s*latest\s*$", text), (
            f"{values_file.name}: must not contain tag: latest"
        )

    @pytest.mark.parametrize("service,template_file", TEMPLATE_FILES.items())
    def test_templates_require_image_tags(self, service: str, template_file: Path) -> None:
        text = template_file.read_text(encoding="utf-8")
        # G1: image reference moved to enterprise-crm.image helper which has
        # required(tag) when digest is empty.  The template itself uses
        # {{ include "enterprise-crm.image" }}, not inline required().
        assert 'include "enterprise-crm.image"' in text, (
            f"{template_file.name}: must use enterprise-crm.image helper for image reference"
        )
        # The required(tag) guard lives in _helpers.tpl.  The helper uses
        # required(printf "images.%s.tag is required..." $img.repository ...)
        # which is a generic pattern for all services (gateway/frontend/agents).
        # Verify the generic guard exists, not hardcoded service names.
        helpers = (HELM_DIR / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
        assert 'required (printf "images.%s.tag is required' in helpers, (
            "_helpers.tpl: image tag fallback must use required() with printf guard"
        )

    def test_templates_do_not_hardcode_latest(self) -> None:
        for template_file in TEMPLATE_FILES.values():
            assert ":latest" not in template_file.read_text(encoding="utf-8"), (
                f"{template_file.name}: must not hard-code :latest"
            )


class TestCiHelmStepsPassImageTags:
    def test_all_ci_helm_lint_and_template_commands_set_tags(self) -> None:
        text = CI_CD_YML.read_text(encoding="utf-8")
        helm_call_count = len(
            re.findall(r"^\s*helm (?:lint|template)\s+", text, re.MULTILINE)
        )
        assert helm_call_count > 0, "CI workflow should contain helm lint/template calls"

        # G1: digest-mode steps use env: + ${} variable substitution instead of
        # literal --set images.X.digest=sha256:... in the yaml text.  The env var
        # values contain sha256: but the --set flags use ${DIGEST_GW} etc.
        # Count tag-mode calls only (those with explicit ci-test tag).
        tag_mode_calls = len(
            re.findall(r"--set\s+images\.\w+\.tag=ci-test", text)
        )
        # 3 flags per call * 6 tag-mode helm calls = 18
        assert tag_mode_calls >= 18, (
            f"Expected >= 18 instances of --set images.X.tag=ci-test "
            f"(3 services x 6 tag-mode helm calls), found {tag_mode_calls}"
        )

        # G1: verify digest-mode helm calls exist by checking for the
        # step names (these use env: digests, not literal sha256: in yaml)
        for step_name in (
            "Lint chart (digest mode)",
            "Render default template (digest mode)",
            "Render staging template if present (digest mode)",
            "Render production template if present (digest mode)",
        ):
            assert step_name in text, (
                f"G1: helm-lint must have '{step_name}' step"
            )

    def test_deploy_jobs_still_set_commit_sha_tags(self) -> None:
        text = CI_CD_YML.read_text(encoding="utf-8")
        # G1: deploy jobs now use --set-string images.*.digest=...
        # The tag-based deploy is replaced by digest.  The immutable digest
        # is stronger than the mutable github.sha tag.
        for service in ("frontend", "gateway", "agents"):
            assert f"--set-string images.{service}.digest=" in text, (
                f"Deploy jobs must now use --set-string images.{service}.digest (digest pinning)"
            )


class TestOllamaOptional:
    def test_ollama_defaults_disabled(self) -> None:
        data = _load_yaml(HELM_DIR / "values.yaml")
        assert data.get("ollama", {}).get("enabled") is False

    def test_agents_template_wraps_ollama_env_in_enabled_guard(self) -> None:
        text = TEMPLATE_FILES["agents"].read_text(encoding="utf-8")
        start = text.find("{{- if .Values.ollama.enabled }}")
        assert start != -1, "agents.yaml must guard OLLAMA_* env vars"

        end = text.find("{{- end }}", start)
        assert end != -1, "agents.yaml must close the ollama.enabled guard"

        guarded_block = text[start:end]
        assert "OLLAMA_URL" in guarded_block
        assert "OLLAMA_MODEL" in guarded_block


class TestDeadValuesRemoved:
    def test_base_values_dead_sections_removed(self) -> None:
        data = _load_yaml(HELM_DIR / "values.yaml")
        for key in ("opa", "weaviate", "monitoring"):
            assert key not in data, f"values.yaml: dead section {key!r} must be removed"

    def test_base_values_dead_frontend_keycloak_removed(self) -> None:
        frontend = _load_yaml(HELM_DIR / "values.yaml").get("frontend", {})
        for key in ("keycloakUrl", "keycloakRealm", "keycloakClientId"):
            assert key not in frontend, f"frontend.{key} is not consumed by templates"

    def test_base_values_dead_keycloak_secret_removed(self) -> None:
        secrets = _load_yaml(HELM_DIR / "values.yaml").get("secrets", {})
        assert "keycloak" not in secrets, "secrets.keycloak is not consumed by templates"

    def test_base_values_dead_ollama_resources_removed(self) -> None:
        ollama = _load_yaml(HELM_DIR / "values.yaml").get("ollama", {})
        assert "resources" not in ollama, "ollama.resources is not consumed by templates"

    def test_production_values_dead_sections_removed(self) -> None:
        data = _load_yaml(HELM_DIR / "values-production.yaml")
        for key in ("opa", "weaviate"):
            assert key not in data, f"values-production.yaml: dead section {key!r} must be removed"

    def test_production_values_dead_frontend_keycloak_removed(self) -> None:
        frontend = _load_yaml(HELM_DIR / "values-production.yaml").get("frontend", {})
        for key in ("keycloakUrl", "keycloakRealm", "keycloakClientId"):
            assert key not in frontend, f"frontend.{key} is not consumed by templates"


class TestValuesParseable:
    @pytest.mark.parametrize("values_file", VALUES_FILES, ids=lambda p: p.name)
    def test_values_yaml_parseable(self, values_file: Path) -> None:
        _load_yaml(values_file)


class TestOptionalRealHelmRendering:
    @pytest.fixture
    def helm_binary(self) -> str:
        helm = shutil.which("helm")
        if not helm:
            pytest.skip("helm binary is not available on this host")
        return helm

    def test_helm_template_without_tags_fails_fast(self, helm_binary: str) -> None:
        result = subprocess.run(
            [helm_binary, "template", "test", str(HELM_DIR)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        combined = f"{result.stdout}\n{result.stderr}"
        assert "images." in combined and "tag is required" in combined

    def test_helm_template_with_tags_has_no_latest_or_ollama_by_default(
        self, helm_binary: str
    ) -> None:
        result = subprocess.run(
            [helm_binary, "template", "test", str(HELM_DIR), *TAG_SET_ARGS],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "latest" not in result.stdout
        assert "OLLAMA_URL" not in result.stdout
        assert "OLLAMA_MODEL" not in result.stdout

    def test_helm_template_with_ollama_enabled_renders_ollama_env(
        self, helm_binary: str
    ) -> None:
        result = subprocess.run(
            [
                helm_binary,
                "template",
                "test",
                str(HELM_DIR),
                *TAG_SET_ARGS,
                "--set",
                "ollama.enabled=true",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "OLLAMA_URL" in result.stdout
        assert "OLLAMA_MODEL" in result.stdout
