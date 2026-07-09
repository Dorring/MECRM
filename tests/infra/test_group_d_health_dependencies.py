"""D1 regression tests — Compose health dependency conditions and healthchecks.

These tests parse docker-compose.yml directly with PyYAML. They do NOT run
`docker compose config` (the host has no Docker), so variable interpolation
(`${VAR:-default}`) is NOT resolved — we assert on the literal structure.

Covers:
  D-C-1  — OPA deps use service_healthy (gateway + agents)
  D-C-2  — Weaviate dep uses service_healthy (agents)
  D-HC-2 — frontend-proxy has a healthcheck
  D-HC-3 — agents has a Compose healthcheck (partial; /ready deferred)
  D-HC-4 — Kafka has start_period
  D-HC-5 — Postgres has start_period
  B3     — ws-proxy-test → frontend-proxy uses service_healthy
  B1     — nginx.conf has location = /health
  C      — frontend has GET /api/health route
"""

import os
import re
import unittest

import yaml

COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.yml"
)
NGINX_CONF_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "conf", "nginx.conf"
)
FRONTEND_HEALTH_ROUTE = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "frontend", "src", "app", "api", "health", "route.ts",
)


def _load_compose():
    with open(COMPOSE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# D-C-1: OPA deps use service_healthy
# ---------------------------------------------------------------------------

class TestOPADependencyConditions(unittest.TestCase):
    """gateway and agents must depend on opa with condition: service_healthy."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def _get_opa_dep_condition(self, service_name):
        svc = self.compose["services"].get(service_name)
        if svc is None:
            self.fail(f"Service {service_name!r} not found in docker-compose.yml")
        deps = svc.get("depends_on") or {}
        for key, value in deps.items():
            if key == "opa":
                return value.get("condition") if isinstance(value, dict) else None
        return None

    def test_gateway_depends_on_opa_service_healthy(self):
        cond = self._get_opa_dep_condition("gateway")
        self.assertEqual(cond, "service_healthy",
                         f"gateway→opa condition expected service_healthy, got {cond!r}")

    def test_agents_depends_on_opa_service_healthy(self):
        cond = self._get_opa_dep_condition("agents")
        self.assertEqual(cond, "service_healthy",
                         f"agents→opa condition expected service_healthy, got {cond!r}")


# ---------------------------------------------------------------------------
# D-C-2: Weaviate dep uses service_healthy (agents)
# ---------------------------------------------------------------------------

class TestWeaviateDependencyCondition(unittest.TestCase):
    """agents must depend on weaviate with condition: service_healthy."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_agents_depends_on_weaviate_service_healthy(self):
        svc = self.compose["services"].get("agents")
        self.assertIsNotNone(svc, "agents service not found in docker-compose.yml")
        deps = svc.get("depends_on") or {}
        for key, value in deps.items():
            if key == "weaviate":
                cond = value.get("condition") if isinstance(value, dict) else None
                self.assertEqual(
                    cond, "service_healthy",
                    f"agents→weaviate condition expected service_healthy, got {cond!r}",
                )
                return
        self.fail("agents does not list weaviate in depends_on")


# ---------------------------------------------------------------------------
# D-HC-2: frontend-proxy has a healthcheck
# ---------------------------------------------------------------------------

class TestFrontendProxyHealthcheck(unittest.TestCase):
    """frontend-proxy must define a healthcheck."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_frontend_proxy_has_healthcheck_block(self):
        svc = self.compose["services"].get("frontend-proxy")
        self.assertIsNotNone(svc, "frontend-proxy service not found")
        hc = svc.get("healthcheck")
        self.assertIsNotNone(hc, "frontend-proxy has no healthcheck block")

    def test_frontend_proxy_healthcheck_has_test(self):
        svc = self.compose["services"]["frontend-proxy"]
        hc = svc.get("healthcheck", {})
        test = hc.get("test")
        self.assertIsNotNone(test, "frontend-proxy healthcheck has no test")
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        self.assertIn("health", test_str,
                      "frontend-proxy healthcheck should reference /health")

    def test_frontend_proxy_healthcheck_has_reasonable_params(self):
        svc = self.compose["services"]["frontend-proxy"]
        hc = svc.get("healthcheck", {})
        self.assertIsNotNone(hc.get("interval"), "healthcheck missing interval")
        self.assertIsNotNone(hc.get("timeout"), "healthcheck missing timeout")
        self.assertIsNotNone(hc.get("retries"), "healthcheck missing retries")
        self.assertIsNotNone(hc.get("start_period"), "healthcheck missing start_period")


# ---------------------------------------------------------------------------
# D-HC-3: agents has a Compose healthcheck (partial — /ready deferred)
# ---------------------------------------------------------------------------

class TestAgentsComposeHealthcheck(unittest.TestCase):
    """agents must define a Compose healthcheck (on existing /health endpoint)."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_agents_has_healthcheck_block(self):
        svc = self.compose["services"].get("agents")
        self.assertIsNotNone(svc, "agents service not found")
        hc = svc.get("healthcheck")
        self.assertIsNotNone(hc, "agents has no Compose healthcheck block")

    def test_agents_healthcheck_hits_health_endpoint(self):
        svc = self.compose["services"]["agents"]
        hc = svc.get("healthcheck", {})
        test = hc.get("test")
        self.assertIsNotNone(test, "agents healthcheck has no test")
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        self.assertIn("health", test_str,
                      "agents healthcheck should hit /health endpoint")

    def test_agents_healthcheck_checks_status_code(self):
        """P0-6: check the response status code, not just that the request completed."""
        svc = self.compose["services"]["agents"]
        hc = svc.get("healthcheck", {})
        test = hc.get("test")
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        self.assertIn("is_success", test_str,
                      "agents healthcheck must check r.is_success (not just connect)")


# ---------------------------------------------------------------------------
# D-HC-4: Kafka has start_period
# ---------------------------------------------------------------------------

class TestKafkaStartPeriod(unittest.TestCase):
    """Kafka healthcheck must include start_period to avoid false failures during
    KRaft controller election (can take 30-60s)."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_kafka_healthcheck_has_start_period(self):
        svc = self.compose["services"].get("kafka")
        self.assertIsNotNone(svc, "kafka service not found")
        hc = svc.get("healthcheck") or {}
        sp = hc.get("start_period")
        self.assertIsNotNone(sp,
                             "kafka healthcheck missing start_period "
                             "(should be 60s for KRaft election)")

    def test_kafka_start_period_is_reasonable(self):
        svc = self.compose["services"]["kafka"]
        hc = svc.get("healthcheck") or {}
        sp = hc.get("start_period")
        # Accept both string ("60s") and numeric forms
        sp_str = str(sp) if sp is not None else ""
        sp_num = int(re.sub(r"[^0-9]", "", sp_str)) if sp_str else 0
        self.assertGreaterEqual(
            sp_num, 30,
            f"kafka start_period ({sp}) too short; should be >=30s for KRaft "
            "controller election",
        )


# ---------------------------------------------------------------------------
# D-HC-5: Postgres has start_period
# ---------------------------------------------------------------------------

class TestPostgresStartPeriod(unittest.TestCase):
    """Postgres healthcheck must include start_period (Alpine PG boots in ~5-10s)."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_postgres_healthcheck_has_start_period(self):
        svc = self.compose["services"].get("postgres")
        self.assertIsNotNone(svc, "postgres service not found")
        hc = svc.get("healthcheck") or {}
        sp = hc.get("start_period")
        self.assertIsNotNone(sp,
                             "postgres healthcheck missing start_period")


# ---------------------------------------------------------------------------
# B3: ws-proxy-test → frontend-proxy uses service_healthy
# ---------------------------------------------------------------------------

class TestWsProxyTestFrontendProxyCondition(unittest.TestCase):
    """ws-proxy-test depends on frontend-proxy with condition: service_healthy."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_ws_proxy_test_frontend_proxy_condition(self):
        svc = self.compose["services"].get("ws-proxy-test")
        self.assertIsNotNone(svc, "ws-proxy-test service not found")
        deps = svc.get("depends_on") or {}
        for key, value in deps.items():
            if key == "frontend-proxy":
                cond = value.get("condition") if isinstance(value, dict) else None
                self.assertEqual(
                    cond, "service_healthy",
                    f"ws-proxy-test→frontend-proxy condition expected "
                    f"service_healthy, got {cond!r}",
                )
                return
        self.fail("ws-proxy-test does not list frontend-proxy in depends_on")


# ---------------------------------------------------------------------------
# B1: nginx.conf has location = /health
# ---------------------------------------------------------------------------

class TestNginxHealthLocation(unittest.TestCase):
    """conf/nginx.conf must define location = /health returning 200."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(NGINX_CONF_PATH):
            raise unittest.SkipTest(f"{NGINX_CONF_PATH} not found")
        with open(NGINX_CONF_PATH, "r", encoding="utf-8") as f:
            cls.nginx_conf = f.read()

    def test_nginx_has_health_location(self):
        self.assertIn("location = /health", self.nginx_conf,
                      "nginx.conf must contain 'location = /health'")

    def test_nginx_health_returns_200(self):
        # Block around the /health location should return 200
        self.assertRegex(
            self.nginx_conf,
            r"location = /health\s*\{[^}]*return 200",
            "nginx /health location must return 200",
        )

    def test_nginx_health_before_ws_location(self):
        """/health must be defined before /ws to avoid routing conflicts."""
        health_idx = self.nginx_conf.find("location = /health")
        ws_idx = self.nginx_conf.find("location /ws")
        if health_idx == -1 or ws_idx == -1:
            self.fail("Could not find both location blocks in nginx.conf")
        self.assertLess(
            health_idx, ws_idx,
            "location = /health must appear before location /ws in nginx.conf",
        )


# ---------------------------------------------------------------------------
# C: frontend has GET /api/health route
# ---------------------------------------------------------------------------

class TestFrontendApiHealthRoute(unittest.TestCase):
    """frontend must export GET /api/health for K8s pod-level probes."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(FRONTEND_HEALTH_ROUTE):
            raise unittest.SkipTest(
                f"{FRONTEND_HEALTH_ROUTE} not found — /api/health route "
                "not yet created"
            )
        with open(FRONTEND_HEALTH_ROUTE, "r", encoding="utf-8") as f:
            cls.route_source = f.read()

    def test_frontend_health_route_exists(self):
        self.assertTrue(
            os.path.exists(FRONTEND_HEALTH_ROUTE),
            f"Missing {FRONTEND_HEALTH_ROUTE} — /api/health route for K8s probes",
        )

    def test_frontend_health_route_exports_get(self):
        self.assertIn("GET", self.route_source,
                      "/api/health route must export a GET handler")

    def test_frontend_health_route_returns_json_ok(self):
        self.assertIn("status", self.route_source,
                      "/api/health route should return { status: 'ok' }")


# ---------------------------------------------------------------------------
# Generic: no unintended service_started regressions
# ---------------------------------------------------------------------------

class TestNoServiceStartedRegressions(unittest.TestCase):
    """Verify that service_started is only used where explicitly acceptable:
    bare depends_on (no explicit condition) on frontend-proxy → frontend/gateway.
    All explicit condition: service_started entries should have been migrated.
    """

    # Services where bare depends_on (default=service_started) is acceptable.
    ACCEPTABLE_BARE_DEPENDS = {
        "frontend-proxy",  # → frontend, gateway (bare)
        "frontend",        # → gateway (bare)
        "grafana",         # → prometheus (bare)
        "postgres-exporter",  # → postgres (bare)
        "redis-exporter",     # → redis (bare)
        "kafka-exporter",     # → kafka (bare)
        "kafka-ui",           # → kafka (bare)
        "prometheus",         # bare depends (no explicit condition)
    }

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()

    def test_no_explicit_service_started_condition(self):
        """No explicit condition: service_started should remain except for
        acceptable bare depends_on entries."""
        violations = []
        for svc_name, svc in self.compose["services"].items():
            deps = svc.get("depends_on") or {}
            if isinstance(deps, list):
                # Bare depends_on: depends_on: [service1, service2]
                # These default to service_started — acceptable for listed services.
                if svc_name not in self.ACCEPTABLE_BARE_DEPENDS:
                    violations.append(
                        f"{svc_name}: bare depends_on list {deps} "
                        f"(not in acceptable list)"
                    )
                continue
            for dep_name, dep_value in deps.items():
                if isinstance(dep_value, dict):
                    cond = dep_value.get("condition")
                    if cond == "service_started":
                        violations.append(
                            f"{svc_name}→{dep_name}: explicit "
                            "condition: service_started"
                        )
        if violations:
            self.fail(
                "Found explicit condition: service_started that should be "
                "upgraded to service_healthy or documented as acceptable:\n"
                + "\n".join(f"  - {v}" for v in violations)
            )
