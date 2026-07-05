import os
import yaml

class ConfigLoader:
    _instance = None
    _config = None

    @classmethod
    def get_config(cls):
        if cls._config is None:
            # Locate config.yaml in the same directory as this script (src/)
            current_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(current_dir, "config.yaml")
            
            if not os.path.exists(config_path):
                raise FileNotFoundError(f"Configuration file missing: {config_path}")
            
            with open(config_path, 'r', encoding='utf-8') as f:
                cls._config = yaml.safe_load(f)
                
            # Automatically resolve paths so that scripts can be executed from anywhere
            cls._config['storage']['centroids_path'] = os.path.abspath(
                os.path.join(current_dir, cls._config['storage']['centroids_path'])
            )
            cls._config['storage']['data_path'] = os.path.abspath(
                os.path.join(current_dir, cls._config['storage']['data_path'])
            )
            
        return cls._config

# Utility function for easier importing
def load_config():
    return ConfigLoader.get_config()
