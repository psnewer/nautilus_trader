# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""Tests for OrbitExch configuration."""

import pytest

from nautilus_trader.adapters.orbitexch.config import (
    OrbitExchDataClientConfig,
    OrbitExchExecClientConfig,
)


class TestOrbitExchDataClientConfig:
    """Tests for OrbitExchDataClientConfig."""
    
    def test_config_with_required_params(self):
        """Test creating config with only required parameters."""
        config = OrbitExchDataClientConfig(
            username='test_user',
            password='test_pass',
        )
        
        assert config.username == 'test_user'
        assert config.password == 'test_pass'
        assert config.base_url == 'https://orbitexch.com'
        assert config.headless is True
        assert config.browser_type == 'chromium'
        assert config.user_data_dir is None
        assert config.page_timeout == 30000
        assert config.scrape_interval_ms == 1000
    
    def test_config_with_custom_params(self):
        """Test creating config with custom parameters."""
        config = OrbitExchDataClientConfig(
            username='test_user',
            password='test_pass',
            base_url='https://custom.orbitexch.com',
            headless=False,
            browser_type='firefox',
            user_data_dir='./custom_data',
            page_timeout=60000,
            scrape_interval_ms=500,
        )
        
        assert config.base_url == 'https://custom.orbitexch.com'
        assert config.headless is False
        assert config.browser_type == 'firefox'
        assert config.user_data_dir == './custom_data'
        assert config.page_timeout == 60000
        assert config.scrape_interval_ms == 500
    
    def test_config_is_frozen(self):
        """Test that config is immutable."""
        config = OrbitExchDataClientConfig(
            username='test_user',
            password='test_pass',
        )
        
        with pytest.raises(AttributeError):
            config.username = 'new_user'


class TestOrbitExchExecClientConfig:
    """Tests for OrbitExchExecClientConfig."""
    
    def test_config_with_required_params(self):
        """Test creating config with only required parameters."""
        config = OrbitExchExecClientConfig(
            username='test_user',
            password='test_pass',
        )
        
        assert config.username == 'test_user'
        assert config.password == 'test_pass'
        assert config.max_bet_amount == 10000.0
        assert config.confirm_bet is True
    
    def test_config_with_custom_params(self):
        """Test creating config with custom parameters."""
        config = OrbitExchExecClientConfig(
            username='test_user',
            password='test_pass',
            max_bet_amount=5000.0,
            confirm_bet=False,
        )
        
        assert config.max_bet_amount == 5000.0
        assert config.confirm_bet is False
