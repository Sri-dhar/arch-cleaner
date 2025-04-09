import toml
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
import os
import logging

logger = logging.getLogger(__name__)

# Define default configuration structure and values
DEFAULT_CONFIG = {
    "general": {
        "aggressiveness": 2,
    },
    "safety": {
        "use_trash": True,
        "default_dry_run": False,
    },
    "paths": {
        "scan": [
            "~/.cache",
            "~/Downloads",
            "~/.local/share",
        ],
        "exclude": [
            "*/.git/*",
            "*/node_modules/*",
            "*/__pycache__/*",
            "*.important",
            "~/.config/*",
        ],
    },
    "thresholds": {
        "old_file": "3m", # 3 months
        "large_file": "500M", # 500 MB
    },
    "arch": {
        "clean_pacman_cache": True,
        "pacman_cache_keep": 1,
        "clean_uninstalled_cache": False,
        "remove_orphans": True,
        "clean_journal": True,
        "journal_max_disk_size": "500M",
        "journal_max_age": None, # Prioritize size by default
    },
    "duplicates": {
        "enabled": True,
        "min_size": "1M",
        "scan_paths": [], # Empty means use general scan_paths
    },
    "automation": {
        "enabled": False,
        "schedule": "weekly",
        "free_space_threshold": "10%",
        "min_confidence": 0.8,
    },
    "learning": {
        "enabled": True,
        "feedback_history_limit": 1000,
    }
}

class ConfigManager:
    """Manages loading and accessing the application configuration."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config: Dict[str, Any] = self._load_config()
        self._expand_paths()

    def _load_config(self) -> Dict[str, Any]:
        """Loads configuration from the TOML file, merging with defaults."""
        config = self._deep_merge_dicts(DEFAULT_CONFIG, self._read_toml_file())
        return config

    def _read_toml_file(self) -> Dict[str, Any]:
        """Reads the TOML configuration file."""
        if not self.config_path.exists():
            logger.warning(f"Configuration file not found at {self.config_path}. Using default settings.")
            # In a real scenario, might prompt user or copy example config here if not handled by main.py/installer
            return {}
        try:
            with open(self.config_path, 'r') as f:
                loaded_config = toml.load(f)
                logger.info(f"Successfully loaded configuration from {self.config_path}")
                return loaded_config
        except toml.TomlDecodeError as e:
            logger.error(f"Error decoding TOML file {self.config_path}: {e}")
            print(f"Error: Invalid configuration file format in {self.config_path}. Please check the syntax.", file=sys.stderr)
            sys.exit(1)
        except IOError as e:
            logger.error(f"Error reading configuration file {self.config_path}: {e}")
            print(f"Error: Could not read configuration file {self.config_path}.", file=sys.stderr)
            sys.exit(1)
        return {}

    def _deep_merge_dicts(self, base: Dict, overlay: Dict) -> Dict:
        """Recursively merges two dictionaries. Overlay values take precedence."""
        merged = base.copy()
        for key, value in overlay.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = self._deep_merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _expand_paths(self):
        """Expands ~ and environment variables in path lists."""
        home_dir = Path.home()
        paths_to_expand = [
            ('paths', 'scan'),
            ('paths', 'exclude'),
            ('duplicates', 'scan_paths')
        ]

        for section, key in paths_to_expand:
            if section in self.config and key in self.config[section]:
                expanded_list = []
                for path_str in self.config[section][key]:
                    if isinstance(path_str, str):
                        # Expand ~ and environment variables
                        p = os.path.expanduser(os.path.expandvars(path_str))
                        # Make absolute if not already (relative paths could be ambiguous)
                        # We might want paths relative to home, but absolute is safer generally.
                        # Let's keep them as potentially relative for now, interpretation depends on usage.
                        # expanded_list.append(str(Path(p).resolve())) # Option: Make absolute
                        expanded_list.append(p)
                    else:
                        expanded_list.append(path_str) # Keep non-string items as is
                self.config[section][key] = expanded_list


    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Gets a configuration value using a dot-separated key path.
        Example: get('arch.pacman_cache_keep')
        """
        keys = key_path.split('.')
        value = self.config
        try:
            for key in keys:
                if isinstance(value, dict):
                    value = value[key]
                else:
                    # Handle case where intermediate key is not a dict
                    logger.warning(f"Config key path '{key_path}' intermediate key '{key}' is not a dictionary.")
                    return default
            return value
        except KeyError:
            logger.debug(f"Config key '{key_path}' not found, returning default: {default}")
            return default
        except TypeError:
             # Handle cases where value is not subscriptable (e.g., trying to access key in a list/str)
            logger.warning(f"Config key path '{key_path}' encountered non-dictionary value.")
            return default


    def get_scan_paths(self) -> List[Path]:
        """Gets the list of resolved scan paths."""
        raw_paths = self.get('paths.scan', [])
        return [Path(p) for p in raw_paths if isinstance(p, str)]

    def get_exclude_patterns(self) -> List[str]:
        """Gets the list of exclusion glob patterns."""
        return self.get('paths.exclude', [])

    def get_duplicate_scan_paths(self) -> Optional[List[Path]]:
        """Gets the specific paths for duplicate scanning, or None to use general scan paths."""
        raw_paths = self.get('duplicates.scan_paths', [])
        if not raw_paths: # If empty list in config, use general scan paths
            return None
        return [Path(p) for p in raw_paths if isinstance(p, str)]

    # Add more specific getter methods as needed for type safety or complex logic
    # e.g., get_old_file_threshold_seconds() -> int

    def reload(self):
        """Reloads the configuration from the file."""
        logger.info(f"Reloading configuration from {self.config_path}")
        self.config = self._load_config()
        self._expand_paths()

    # TODO: Implement methods for setting/saving configuration if needed
    # def set(self, key_path: str, value: Any): ...
    # def save(self): ...

# Example Usage (typically done in main.py)
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    # Assume config.toml exists in the same directory for this example
    script_dir = Path(__file__).parent
    example_config_path = script_dir.parent.parent / 'config.toml.example' # Adjust path as needed
    # Create a dummy config for testing if example doesn't exist
    if not example_config_path.exists():
         dummy_path = Path('./temp_config.toml')
         dummy_path.write_text("""
[general]
aggressiveness = 1

[paths]
scan = ["~/Documents", "/tmp/my_stuff"]
exclude = ["*.log"]
         """)
         example_config_path = dummy_path


    print(f"Using config file: {example_config_path}")
    config_manager = ConfigManager(example_config_path)

    print("\n--- Full Config ---")
    import json
    print(json.dumps(config_manager.config, indent=2))

    print("\n--- Specific Values ---")
    print(f"Aggressiveness: {config_manager.get('general.aggressiveness')}")
    print(f"Use Trash: {config_manager.get('safety.use_trash')}")
    print(f"Scan Paths: {config_manager.get_scan_paths()}")
    print(f"Exclude Patterns: {config_manager.get_exclude_patterns()}")
    print(f"Pacman Keep: {config_manager.get('arch.pacman_cache_keep')}")
    print(f"Non-existent key: {config_manager.get('foo.bar.baz', 'default_value')}")

    # Clean up dummy file if created
    if 'dummy_path' in locals() and dummy_path.exists():
        dummy_path.unlink()
