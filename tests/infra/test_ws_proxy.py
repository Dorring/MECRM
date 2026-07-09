"""Regression tests for Group C / C4 same-origin WebSocket proxy.

These tests are intentionally static. The runtime 101/4401 behavior is covered
by scripts/ws-proxy-test.js and the CI ws-proxy-smoke job; this file prevents
configuration regressions before Docker is started.
"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
NGINX_CONF = REPO_ROOT / "conf" / "nginx.conf"
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "enterprise-crm"
FRONTEND_TEMPLATE = CHART_DIR / "templates" / "frontend.yaml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(_read(path))


def _env_list(service: dict) -> list[str]:
    env = service.get("environment", [])
    if isinstance(env, dict):
        return [f"{key}={value}" for key, value in env.items()]
    return list(env)


def test_compose_frontend_proxy_is_browser_entrypoint():
    compose = _load_yaml(COMPOSE_PATH)
    services = compose["services"]

    proxy = services.get("frontend-proxy")
    assert proxy is not None, "frontend-proxy service is required for same-origin /ws"
    assert proxy["image"] == "nginx:1.27-alpine"
    assert "${FRONTEND_PORT:-3000}:80" in proxy.get("ports", [])
    assert "./conf/nginx.conf:/etc/nginx/nginx.conf:ro" in proxy.get("volumes", [])

    frontend = services["frontend"]
    assert "ports" not in frontend, "frontend must not publish host port directly"
    assert "3000" in frontend.get("expose", [])


def test_compose_frontend_runtime_urls_are_same_origin():
    compose = _load_yaml(COMPOSE_PATH)
    env = _env_list(compose["services"]["frontend"])
    joined = "\n".join(env)

    assert "GATEWAY_INTERNAL_URL=http://gateway:4000" in joined
    assert "API_URL=" in joined
    assert "WS_URL=" in joined
    assert "WS_URL=ws://gateway:4000" not in joined
    assert "NEXT_PUBLIC_" not in joined


def test_nginx_routes_api_config_frontend_and_ws_gateway():
    conf = _read(NGINX_CONF)

    assert "location /ws" in conf
    assert "proxy_http_version 1.1;" in conf
    assert "proxy_set_header Upgrade $http_upgrade;" in conf
    assert "proxy_set_header Connection $connection_upgrade;" in conf
    assert "proxy_read_timeout 3600s;" in conf
    assert "proxy_send_timeout 3600s;" in conf
    assert "proxy_buffering off;" in conf

    assert "location = /api/config" in conf
    assert "proxy_pass http://frontend_upstream;" in conf
    assert "location /api/" in conf
    assert "proxy_pass http://gateway_upstream;" in conf


def test_helm_frontend_uses_server_side_runtime_env_only():
    template = _read(FRONTEND_TEMPLATE)

    assert "NEXT_PUBLIC_" not in template
    assert "name: GATEWAY_INTERNAL_URL" in template
    assert "name: API_URL" in template
    assert "name: WS_URL" in template


def test_helm_ingress_routes_and_timeouts():
    for values_file in ("values.yaml", "values-staging.yaml", "values-production.yaml"):
        values = _load_yaml(CHART_DIR / values_file)

        # Annotations may be inherited from the default values.yaml in some
        # override files. Only assert when present at this file level.
        annotations = values["ingress"].get("annotations", {})
        if annotations:
            assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "3600", (
                f"{values_file}: proxy-read-timeout must be 3600"
            )
            assert annotations["nginx.ingress.kubernetes.io/proxy-send-timeout"] == "3600", (
                f"{values_file}: proxy-send-timeout must be 3600"
            )

        paths = values["ingress"]["hosts"][0]["paths"]

        # /api/config must be Exact → frontend (not caught by /api Prefix → gateway)
        config_paths = [p for p in paths if p.get("path") == "/api/config"]
        assert config_paths, f"{values_file}: /api/config ingress path missing"
        assert config_paths[0]["pathType"] == "Exact", (
            f"{values_file}: /api/config must be Exact (not Prefix) to beat /api"
        )
        assert config_paths[0]["service"] == "frontend", (
            f"{values_file}: /api/config must route to frontend (Next.js route handler)"
        )

        # / must be Prefix → frontend
        root_paths = [p for p in paths if p.get("path") == "/"]
        assert root_paths, f"{values_file}: / ingress path missing"
        assert root_paths[0]["service"] == "frontend"

        # /api must be Prefix → gateway
        api_paths = [p for p in paths if p.get("path") == "/api"]
        assert api_paths, f"{values_file}: /api ingress path missing"
        assert api_paths[0]["service"] == "gateway"

        # /ws must be Prefix → gateway
        ws_paths = [p for p in paths if p.get("path") == "/ws"]
        assert ws_paths, f"{values_file}: /ws ingress path missing"
        assert ws_paths[0]["service"] == "gateway"


def test_no_browser_facing_ws_anti_patterns_in_frontend_source():
    """Source code must not actually USE NEXT_PUBLIC_* or hardcoded gateway URLs.
    Documentation comments explaining why we don't use them are fine."""
    lines = []
    for path in (REPO_ROOT / "frontend" / "src").rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # Skip documentation comments
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            lines.append(line)

    source_text = "\n".join(lines)

    assert "?token=" not in source_text
    assert "ws://gateway:4000" not in source_text
    # Only flag actual env-var usage, not documentation mentions
    assert "process.env.NEXT_PUBLIC_API_URL" not in source_text
    assert "process.env.NEXT_PUBLIC_WS_URL" not in source_text


def test_api_config_route_uses_strict_undefined_check():
    """/api/config route.ts must use !== undefined, not ||, so that
    explicit empty string WS_URL='' and API_URL='' pass through
    without falling back to dev defaults."""
    route_path = REPO_ROOT / "frontend" / "src" / "app" / "api" / "config" / "route.ts"
    source = route_path.read_text(encoding="utf-8")

    assert "process.env.API_URL !== undefined" in source, (
        "API_URL must be checked with !== undefined, not ||, "
        "to preserve explicit empty string"
    )
    assert "process.env.WS_URL !== undefined" in source, (
        "WS_URL must be checked with !== undefined, not ||, "
        "to preserve explicit empty string"
    )
    assert "process.env.API_URL ||" not in source, (
        "API_URL || fallback treats '' as falsy — use !== undefined instead"
    )
    assert "process.env.WS_URL ||" not in source, (
        "WS_URL || fallback treats '' as falsy — use !== undefined instead"
    )
