"""Regression tests for the Helm chart secretKeyRef alignment (P0-3).

P0-3: gateway.yaml / agents.yaml hardcoded `key: connection-string` for the
DATABASE_URL / REDIS_URL secretKeyRef, while values.yaml declared `passwordKey:
password`. The key names did not match, so the env var resolved to empty and the
pod CrashLoopBackOff'd.

These tests do NOT require the helm binary. They:
  1. Parse values.yaml with PyYAML to read the declared secret keys.
  2. Read the gateway/agents template files as text and extract every
     `secretKeyRef` block's `name:` and `key:` line.
  3. Assert that each template `key:` is either a literal that matches the value
     declared in values.yaml for that secret, or a `{{ .Values.secrets.<g>.<k> }}`
     reference whose path resolves to a key actually declared in values.yaml.

The contract under test: "the secret key consumed by the template equals the
secret key declared in values.yaml."
"""

import os
import re
import unittest

import yaml

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CHART_DIR = os.path.join(REPO_ROOT, "deploy", "helm", "enterprise-crm")
VALUES_PATH = os.path.join(CHART_DIR, "values.yaml")
GATEWAY_TPL = os.path.join(CHART_DIR, "templates", "gateway.yaml")
AGENTS_TPL = os.path.join(CHART_DIR, "templates", "agents.yaml")


def _load_values():
    with open(VALUES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# Match a single secretKeyRef block. A block looks like:
#   secretKeyRef:
#     name: {{ .Values.secrets.postgresql.existingSecret }}
#     key: {{ .Values.secrets.postgresql.connectionStringKey }}
# We capture the name-expression and key-expression (each may be a literal too).
# Indentation is 2+ spaces under valueFrom; we match the block non-greedily.
SECRET_KEY_REF_RE = re.compile(
    r"secretKeyRef:\s*\n"
    r"\s*name:\s*(?P<name>[^\n]+)\n"
    r"\s*key:\s*(?P<key>[^\n]+)",
)


def _extract_secret_key_refs(text):
    """Return list of dicts: {name_expr, key_expr} for each secretKeyRef block."""
    refs = []
    for m in SECRET_KEY_REF_RE.finditer(text):
        refs.append(
            {
                "name_expr": m.group("name").strip(),
                "key_expr": m.group("key").strip(),
            }
        )
    return refs


def _parse_ref(expr):
    """Parse a `.Values.secrets.<group>.<attr>` Helm expression.

    Returns (group, attr) if it is such an expression, or (None, literal_value)
    if it is a literal string.
    """
    m = re.match(
        r"\{\{\s*\.Values\.secrets\.(\w+)\.(\w+)\s*\}\}", expr
    )
    if m:
        return m.group(1), m.group(2)
    return None, expr


class TestSecretKeyAlignment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = _load_values()
        cls.gateway = _read(GATEWAY_TPL)
        cls.agents = _read(AGENTS_TPL)

    def _secrets(self):
        return self.values.get("secrets", {})

    def _check_template(self, name, text):
        refs = _extract_secret_key_refs(text)
        self.assertTrue(
            refs,
            f"{name}: no secretKeyRef blocks found; test harness regex may be stale",
        )
        for ref in refs:
            name_expr = ref["name_expr"]
            key_expr = ref["key_expr"]
            # The `name:` line must reference an existingSecret for some secret
            # group, so we know WHICH secret this key belongs to.
            name_group, name_attr = _parse_ref(name_expr)
            with self.subTest(
                template=name, secret=name_group, name_attr=name_attr
            ):
                self.assertIsNotNone(
                    name_group,
                    f"{name}: secretKeyRef name {name_expr!r} is not a "
                    ".Values.secrets.<g>.existingSecret expression",
                )
                self.assertEqual(
                    name_attr,
                    "existingSecret",
                    f"{name}: expected name to reference .existingSecret, "
                    f"got .{name_attr}",
                )
                declared_secret = self._secrets().get(name_group, {})
                self.assertIn(
                    "existingSecret",
                    declared_secret,
                    f"{name}: values.yaml does not declare "
                    f"secrets.{name_group}.existingSecret",
                )

            # The `key:` line references the key attribute on the SAME secret.
            key_group, key_attr = _parse_ref(key_expr)
            with self.subTest(
                template=name, secret=name_group, key_attr=key_attr
            ):
                if key_group is not None:
                    # Expression form: must reference the same secret group as
                    # the name line, and the attribute must be declared in values.
                    self.assertEqual(
                        key_group,
                        name_group,
                        f"{name}: secretKeyRef key references secrets.{key_group}"
                        f".* but name references secrets.{name_group}.*; they "
                        "must point at the same secret",
                    )
                    declared_secret = self._secrets().get(key_group, {})
                    self.assertIn(
                        key_attr,
                        declared_secret,
                        f"{name}: template references secrets.{key_group}."
                        f"{key_attr} but values.yaml does not declare it",
                    )
                    # P0-3 regression guard: the declared value must NOT be the
                    # stale 'password' while using a connectionStringKey path.
                    declared_value = declared_secret[key_attr]
                    self.assertNotEqual(
                        (key_attr, declared_value),
                        ("passwordKey", "password"),
                        f"{name}: stale P0-3 mismatch detected -- secrets."
                        f"{key_group}.passwordKey=password; rename to "
                        "connectionStringKey",
                    )
                else:
                    # Literal form: the literal must match a value declared in
                    # values for this secret group.
                    declared_secret = self._secrets().get(name_group, {})
                    declared_key = declared_secret.get("connectionStringKey")
                    self.assertEqual(
                        key_attr,
                        declared_key,
                        f"{name}: hardcoded key {key_attr!r} for secret "
                        f"{name_group!r} does not match values connectionStringKey"
                        f"={declared_key!r}",
                    )

    def test_gateway_secret_keys_match_values(self):
        self._check_template("gateway.yaml", self.gateway)

    def test_agents_secret_keys_match_values(self):
        self._check_template("agents.yaml", self.agents)

    def test_values_declares_connection_string_key_not_password_key(self):
        """The renamed key (connectionStringKey) must be declared for both
        postgresql and redis; the old misnamed `passwordKey` must be gone."""
        secrets = self._secrets()
        for g in ("postgresql", "redis"):
            with self.subTest(secret=g):
                self.assertIn(
                    "connectionStringKey",
                    secrets.get(g, {}),
                    f"values.yaml secrets.{g} must declare connectionStringKey "
                    "(renamed from passwordKey so the Secret holds a connection "
                    "string, not a bare password)",
                )
                self.assertNotIn(
                    "passwordKey",
                    secrets.get(g, {}),
                    f"values.yaml secrets.{g} still declares the old misnamed "
                    "`passwordKey`; rename to connectionStringKey",
                )

    def test_no_hardcoded_connection_string_without_values_match(self):
        """Belt-and-suspenders: any literal `key:` in a template must equal the
        connectionStringKey declared in values for that secret (catches a future
        regression to a hardcoded literal that drifts from values)."""
        for name, text in (("gateway.yaml", self.gateway), ("agents.yaml", self.agents)):
            for ref in _extract_secret_key_refs(text):
                key_expr = ref["key_expr"]
                key_group, key_attr = _parse_ref(key_expr)
                if key_group is not None:
                    continue  # expression form, covered above
                name_group, _ = _parse_ref(ref["name_expr"])
                declared = self._secrets().get(name_group, {}).get(
                    "connectionStringKey"
                )
                with self.subTest(template=name, secret=name_group):
                    self.assertEqual(
                        key_attr,
                        declared,
                        f"{name}: literal key {key_attr!r} != values "
                        f"secrets.{name_group}.connectionStringKey={declared!r}",
                    )


if __name__ == "__main__":
    unittest.main()
