# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""
Pytest configuration for OrbitExch adapter tests.

This conftest overrides the global conftest to avoid unnecessary fixtures.
"""

import pytest


# 禁用全局的 cleanup_event_loop_tasks fixture
@pytest.fixture(autouse=False)
def cleanup_event_loop_tasks():
    """Disable the global cleanup_event_loop_tasks fixture."""
    pass
