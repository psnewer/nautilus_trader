"""
Override root conftest's autouse cleanup_event_loop_tasks fixture.

The root tests/conftest.py defines an autouse fixture that depends on
pytest-asyncio's deprecated 'event_loop' fixture. Our arbitrage tests
don't need it, so we override it here with a no-op.
"""

import pytest


@pytest.fixture(autouse=True)
def cleanup_event_loop_tasks():
    yield
