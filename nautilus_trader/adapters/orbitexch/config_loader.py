# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""Configuration loader for OrbitExch adapter."""

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


def load_config(
    env: str = 'dev',
    config_dir: Optional[str] = None,
) -> dict:
    """
    Load OrbitExch configuration.
    
    Combines YAML config file with environment variables.
    Environment variables take precedence.
    
    Parameters
    ----------
    env : str, default 'dev'
        Environment name: 'dev', 'test', 'prod'
    config_dir : str, optional
        Custom config directory path
        
    Returns
    -------
    dict
        Complete configuration
    """
    # Load .env file
    load_dotenv()
    
    # Determine config directory
    if config_dir is None:
        # Assume config/ is in project root
        config_dir = Path(__file__).parent.parent.parent.parent / 'config'
    else:
        config_dir = Path(config_dir)
    
    # Load YAML config
    config_file = config_dir / f'orbitexch_{env}.yaml'
    
    if not config_file.exists():
        raise FileNotFoundError(f'Config file not found: {config_file}')
    
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)
    
    # Override with environment variables
    config['username'] = os.getenv('ORBITEXCH_USERNAME', config.get('username'))
    config['password'] = os.getenv('ORBITEXCH_PASSWORD', config.get('password'))
    
    # Optional overrides
    if os.getenv('ORBITEXCH_BASE_URL'):
        config['base_url'] = os.getenv('ORBITEXCH_BASE_URL')
    
    if os.getenv('ORBITEXCH_HEADLESS'):
        config['headless'] = os.getenv('ORBITEXCH_HEADLESS').lower() == 'true'
    
    return config


def create_data_client_config(env: str = 'dev'):
    """
    Create OrbitExchDataClientConfig from file + env vars.
    
    Parameters
    ----------
    env : str
        Environment: 'dev', 'test', 'prod'
        
    Returns
    -------
    OrbitExchDataClientConfig
    """
    from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
    
    config_dict = load_config(env)
    
    return OrbitExchDataClientConfig(
        username=config_dict['username'],
        password=config_dict['password'],
        base_url=config_dict.get('base_url', 'https://orbitexch.com'),
        headless=config_dict.get('headless', True),
        browser_type=config_dict.get('browser_type', 'chromium'),
        user_data_dir=config_dict.get('user_data_dir'),
        page_timeout=config_dict.get('page_timeout', 30000),
        scrape_interval_ms=config_dict.get('scrape_interval_ms', 1000),
    )


def create_exec_client_config(env: str = 'dev'):
    """
    Create OrbitExchExecClientConfig from file + env vars.
    
    Parameters
    ----------
    env : str
        Environment: 'dev', 'test', 'prod'
        
    Returns
    -------
    OrbitExchExecClientConfig
    """
    from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig
    
    config_dict = load_config(env)
    
    return OrbitExchExecClientConfig(
        username=config_dict['username'],
        password=config_dict['password'],
        base_url=config_dict.get('base_url', 'https://orbitexch.com'),
        headless=config_dict.get('headless', True),
        browser_type=config_dict.get('browser_type', 'chromium'),
        user_data_dir=config_dict.get('user_data_dir'),
        page_timeout=config_dict.get('page_timeout', 30000),
        max_bet_amount=config_dict.get('max_bet_amount', 10000.0),
        confirm_bet=config_dict.get('confirm_bet', True),
    )
