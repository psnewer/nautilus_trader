# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""Configuration loader for OrbitExch adapter."""

import os
from pathlib import Path
from typing import Optional

import yaml


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
    # Load .env file with proper encoding
    try:
        from dotenv import load_dotenv
        # Try UTF-8 first, fall back to system encoding
        try:
            load_dotenv(encoding='utf-8')
        except UnicodeDecodeError:
            load_dotenv()  # Use system default encoding
    except ImportError:
        pass  # dotenv not required if env vars are already set
    
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
    
    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # Override with environment variables
    config['username'] = os.getenv('ORBITEXCH_USERNAME', config.get('username'))
    config['password'] = os.getenv('ORBITEXCH_PASSWORD', config.get('password'))
    
    # Optional overrides
    if os.getenv('ORBITEXCH_BASE_URL'):
        config['base_url'] = os.getenv('ORBITEXCH_BASE_URL')
    
    if os.getenv('ORBITEXCH_HEADLESS'):
        config['headless'] = os.getenv('ORBITEXCH_HEADLESS').lower() == 'true'
    
    # Validate required fields
    if not config.get('username'):
        raise ValueError('Username not configured. Set ORBITEXCH_USERNAME in .env file')
    
    if not config.get('password'):
        raise ValueError('Password not configured. Set ORBITEXCH_PASSWORD in .env file')
    
    return config
