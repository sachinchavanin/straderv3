"""Configuration loading and management."""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_config: dict | None = None


def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load configuration from YAML file and environment."""
    global _config

    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path, override=True)

    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file) as f:
            _config = yaml.safe_load(f)
    else:
        _config = {}

    _apply_env_overrides(_config)
    return _config


def _apply_env_overrides(config: dict) -> None:
    if os.environ.get("TRADE_MODE"):
        config["trade_mode"] = os.environ["TRADE_MODE"]

    if os.environ.get("LOG_LEVEL"):
        if "logging" not in config:
            config["logging"] = {}
        config["logging"]["level"] = os.environ["LOG_LEVEL"]

    if os.environ.get("SQLITE_DB_PATH"):
        if "database" not in config:
            config["database"] = {}
        config["database"]["path"] = os.environ["SQLITE_DB_PATH"]


def get_config() -> dict:
    global _config
    if _config is None:
        return load_config()
    return _config


def get_nested(config: dict, path: str, default: Any = None) -> Any:
    """Get nested config value using dot notation."""
    keys = path.split(".")
    value = config
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value
