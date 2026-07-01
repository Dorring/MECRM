"""conftest for tests/infra/ -- opt OUT of the repo-root Docker autouse fixture.

The repo-root tests/conftest.py defines a session-scoped, `autouse=True`
fixture `_infra_ready` that runs `docker compose up` and applies SQL migrations
via psql before ANY test under tests/ runs. That is correct for the integration
tests that need live infra, but the infra-config regression tests in this
directory are explicitly Docker/psql/helm-free: they parse YAML and template
text only.

To prevent the root autouse fixture from running (and failing with FileNotFoundError
on a host without Docker) for these tests, we OVERRIDE `_infra_ready` here with a
no-op. In pytest, a fixture defined in a closer conftest shadows a same-named
fixture in a parent conftest, and the autouse flag of the closer definition wins
for tests under this directory.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _infra_ready() -> None:
    # Intentionally a no-op: infra-config tests do not require Docker.
    return None
