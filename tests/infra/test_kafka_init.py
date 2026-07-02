"""Static tests for Kafka topic initialization setup.

Covers ADR-001:
  - KAFKA_AUTO_CREATE_TOPICS_ENABLE must be false.
  - kafka-init service must exist, use the Kafka image, mount the script, and
    depend on kafka service_healthy.
  - gateway, agents, replay-service and smoke-test must wait for
    kafka-init service_completed_successfully.
  - KafkaJS producer/consumer must have allowAutoTopicCreation: false.
  - scripts/kafka-init.sh must include every topic referenced by the Gateway
    TOPICS constant and the Agents CONSUME_TOPICS list.
"""

import re
import subprocess
import unittest
from pathlib import Path

import yaml

COMPOSE_PATH = "docker-compose.yml"
GATEWAY_KAFKA_TS = "gateway/src/services/kafka.ts"
AGENTS_CONFIG_PY = "agents/src/orchestrator/config.py"
KAFKA_INIT_SH = "scripts/kafka-init.sh"


def _load_compose():
    with open(COMPOSE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env_list(svc):
    env = svc.get("environment") or []
    if isinstance(env, list):
        return [str(e) for e in env]
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return []


def _gateway_topics():
    """Return topic names declared in gateway/src/services/kafka.ts TOPICS."""
    text = Path(GATEWAY_KAFKA_TS).read_text(encoding="utf-8")
    return set(re.findall(r"\b[A-Z_]+:\s*['\"](crm\.[a-z0-9.-]+)['\"]", text))


def _agents_consume_topics():
    """Return topic names declared in agents/src/orchestrator/config.py CONSUME_TOPICS."""
    text = Path(AGENTS_CONFIG_PY).read_text(encoding="utf-8")
    return set(re.findall(r"['\"](crm\.[a-z0-9.-]+)['\"]", text))


def _script_topics():
    """Return topic names declared in scripts/kafka-init.sh."""
    text = Path(KAFKA_INIT_SH).read_text(encoding="utf-8")
    return set(re.findall(r'"(crm\.[a-z0-9.-]+):\d+:[^"]*"', text))


class TestKafkaAutoCreateDisabled(unittest.TestCase):
    def test_auto_create_disabled(self):
        compose = _load_compose()
        kafka = compose["services"].get("kafka")
        self.assertIsNotNone(kafka, "kafka service missing")
        env = _env_list(kafka)
        self.assertIn(
            "KAFKA_AUTO_CREATE_TOPICS_ENABLE=false",
            env,
            "KAFKA_AUTO_CREATE_TOPICS_ENABLE must be explicitly false (ADR-001)",
        )


class TestKafkaInitService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})
        cls.init = cls.services.get("kafka-init")
        cls.kafka = cls.services.get("kafka")

    def test_kafka_init_service_exists(self):
        self.assertIsNotNone(self.init, "kafka-init service must exist (ADR-001)")

    def test_kafka_init_uses_kafka_image(self):
        self.assertEqual(
            self.init.get("image"),
            self.kafka.get("image"),
            "kafka-init should reuse the Kafka image to avoid an extra pull",
        )

    def test_kafka_init_mounts_script(self):
        volumes = self.init.get("volumes", []) or []
        joined = " ".join(str(v) for v in volumes)
        self.assertIn(
            "scripts/kafka-init.sh",
            joined,
            "kafka-init must mount scripts/kafka-init.sh",
        )

    def test_kafka_init_runs_script(self):
        cmd = self.init.get("entrypoint") or self.init.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        self.assertIn(
            "kafka-init.sh",
            cmd,
            "kafka-init entrypoint/command must invoke kafka-init.sh",
        )

    def test_kafka_init_depends_on_kafka_healthy(self):
        deps = self.init.get("depends_on", {})
        kafka_dep = deps.get("kafka")
        self.assertIsNotNone(kafka_dep, "kafka-init must depend on kafka")
        if isinstance(kafka_dep, dict):
            self.assertEqual(
                kafka_dep.get("condition"),
                "service_healthy",
                "kafka-init must wait for kafka service_healthy",
            )


class TestKafkaInitBlocksDownstream(unittest.TestCase):
    def _assert_depends_on_init(self, service_name):
        compose = _load_compose()
        svc = compose["services"].get(service_name)
        self.assertIsNotNone(svc, f"{service_name} service missing")
        deps = svc.get("depends_on", {})
        init = deps.get("kafka-init")
        self.assertIsNotNone(init, f"{service_name} must depend on kafka-init")
        if isinstance(init, dict):
            self.assertEqual(
                init.get("condition"),
                "service_completed_successfully",
                f"{service_name} depends_on kafka-init must wait for service_completed_successfully",
            )

    def test_gateway_depends_on_kafka_init_completed(self):
        self._assert_depends_on_init("gateway")

    def test_agents_depends_on_kafka_init_completed(self):
        self._assert_depends_on_init("agents")

    def test_replay_service_depends_on_kafka_init_completed(self):
        self._assert_depends_on_init("replay-service")

    def test_smoke_test_depends_on_kafka_init_completed(self):
        self._assert_depends_on_init("smoke-test")


class TestKafkaJsAutoCreateDisabled(unittest.TestCase):
    def test_producer_auto_create_disabled(self):
        text = Path(GATEWAY_KAFKA_TS).read_text(encoding="utf-8")
        self.assertIn(
            "allowAutoTopicCreation: false",
            text,
            "KafkaJS producer must disable auto topic creation (ADR-001)",
        )

    def test_consumer_auto_create_disabled(self):
        text = Path(GATEWAY_KAFKA_TS).read_text(encoding="utf-8")
        # Count occurrences: one for producer, one for consumer.
        matches = re.findall(r"allowAutoTopicCreation:\s*false", text)
        self.assertGreaterEqual(
            len(matches),
            2,
            "KafkaJS producer and consumer must both disable auto topic creation (ADR-001)",
        )


class TestTopicListScript(unittest.TestCase):
    def test_script_exists_and_non_empty(self):
        body = Path(KAFKA_INIT_SH).read_text(encoding="utf-8")
        self.assertIn("TOPICS=(", body, "kafka-init.sh must declare a TOPICS array")
        self.assertIn(
            "crm.leads.created",
            body,
            "topic list must include crm.leads.created",
        )

    @unittest.skipUnless(
        subprocess.run(["bash", "-c", "echo ok"], capture_output=True).returncode == 0,
        "bash not available on this host",
    )
    def test_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", KAFKA_INIT_SH],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(
            result.returncode,
            0,
            f"kafka-init.sh bash syntax error: {result.stderr}",
        )

    def test_all_gateway_topics_initialized(self):
        gateway = _gateway_topics()
        script = _script_topics()
        missing = gateway - script
        self.assertFalse(
            missing,
            f"kafka-init.sh is missing Gateway topics: {sorted(missing)}",
        )

    def test_all_agents_consume_topics_initialized(self):
        agents = _agents_consume_topics()
        script = _script_topics()
        missing = agents - script
        self.assertFalse(
            missing,
            f"kafka-init.sh is missing Agents CONSUME_TOPICS: {sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
