"""Pytest configuration for Phase 5B tests.

Adds the test package directory to ``sys.path`` so test files can
import the shared :mod:`phase5b_helpers` module without each test
file having to manipulate ``sys.path`` itself.
"""

from __future__ import annotations

import os
import sys

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)
