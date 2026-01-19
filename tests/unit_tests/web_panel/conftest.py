# -*- coding: utf-8 -*-
"""
Pytest configuration for web_panel tests.

This conftest.py provides fixtures that are independent of NautilusTrader core.
"""

import asyncio
import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
def event_loop_for_setup(event_loop):
    """Provide event loop for setup/teardown operations."""
    return event_loop
