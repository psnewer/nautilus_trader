from pathlib import Path 
from typing import Any, Optional 
import yaml 
from .schemas import SystemConfig 
 
 
class ConfigManager: 
    _instance = None 
    _config = None 
 
    def __new__(cls): 
        if cls._instance is None: 
            cls._instance = super().__new__(cls) 
        return cls._instance 
 
    def __init__(self): 
        if self._config is None: 
            self.load_config() 
 
    def load_config(self, config_file=None): 
        if config_file is None: 
            config_file = Path(__file__).parent / "config.yaml" 
            if not config_file.exists(): 
                config_file = Path(__file__).parent / "defaults.yaml" 
        self._config = SystemConfig.from_yaml_file(str(config_file)) 
 
    def save_config(self, config_file=None): 
        if config_file is None: 
            config_file = Path(__file__).parent / "config.yaml" 
        self._config.save_to_file(str(config_file)) 
 
    def update_config(self, path, value): 
        parts = path.split('.') 
        obj = self._config 
        for part in parts[:-1]: 
            obj = getattr(obj, part) 
        setattr(obj, parts[-1], value) 
 
    def get_config(self, path=None): 
        if path is None: 
            return self._config 
        parts = path.split('.') 
        obj = self._config 
        for part in parts: 
            obj = getattr(obj, part) 
        return obj 
 
    @property 
    def market_discovery(self): 
        return self._config.market_discovery 
 
    @property 
    def market_matching(self): 
        return self._config.market_matching 
 
    @property 
    def web_panel(self): 
        return self._config.web_panel 
 
 
config_manager = ConfigManager() 
