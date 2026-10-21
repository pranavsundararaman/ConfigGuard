import json
import re
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from dotenv.parser import parse_stream

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONFIG_COMPONENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

def parse_config(filename: str, content: str, *, strict_env: bool = False) -> dict[str, Any]:
    suffix = Path(filename).suffix.lower()

    if suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(content) or {}
        if not isinstance(data, dict):
            raise ValueError("YAML root must be an object/mapping.")
        return data

    if suffix == ".json":
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("JSON root must be an object/object-like mapping.")
        return data

    if suffix == ".env" or Path(filename).name == ".env":
        if strict_env:
            seen_keys: set[str] = set()
            for binding in parse_stream(StringIO(content)):
                invalid = binding.error or (
                    binding.key is not None
                    and (not _ENV_KEY.fullmatch(binding.key) or binding.value is None)
                )
                if invalid:
                    raise ValueError(f"Invalid ENV syntax on line {binding.original.line}. Use KEY=value assignments.")
                if binding.value is not None and "\n" in binding.value:
                    raise ValueError("Multiline quoted ENV values are not supported.")
                if binding.key is not None and binding.key in seen_keys:
                    raise ValueError(f"Duplicate ENV key '{binding.key}' on line {binding.original.line}.")
                if binding.key is not None:
                    seen_keys.add(binding.key)
        raw_values = dotenv_values(stream=StringIO(content), interpolate=not strict_env)

        result = {}

        for key, value in raw_values.items():
            if key is None:
                continue

            if value is None:
                result[key] = None
            elif value.lower() in {"true", "false"}:
                result[key] = value.lower() == "true"
            else:
                try:
                    result[key] = int(value)
                except ValueError:
                    try:
                        result[key] = float(value)
                    except ValueError:
                        result[key] = value

        return result

    raise ValueError("Unsupported configuration format. Use YAML, JSON, or .env.")

def flatten_config(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(flatten_config(value, path))
        else:
            result[path] = value
    return result


def normalize_historical_configuration(data: dict[str, Any], format: str) -> dict[str, Any]:
    """Normalize JSON/YAML paths and ENV names to the same dotted parameter names.

    ENV naming convention: uppercase path components separated by underscores;
    underscores inside JSON/YAML field names are treated as separators too.
    """
    normalized: dict[str, Any] = {}

    def add(source_path: list[str], value: Any) -> None:
        parts: list[str] = []
        for component in source_path:
            if not _CONFIG_COMPONENT.fullmatch(component):
                raise ValueError(f"Configuration key '{'.'.join(source_path)}' cannot be represented consistently across formats.")
            segments = component.split("_")
            if any(not segment for segment in segments):
                raise ValueError(f"Configuration key '{'.'.join(source_path)}' has an ambiguous underscore separator.")
            parts.extend(segment.lower() for segment in segments)
        parameter = ".".join(parts)
        if parameter in normalized:
            raise ValueError(f"Configuration keys collide after format normalization at '{parameter}'.")
        normalized[parameter] = value

    def visit(mapping: dict[str, Any], path: list[str] | None = None) -> None:
        path = path or []
        for key, value in mapping.items():
            if not isinstance(key, str):
                raise ValueError("Configuration keys must be strings to compare formats consistently.")
            if format == "env" and not _ENV_KEY.fullmatch(key):
                raise ValueError(f"ENV key '{key}' cannot be represented consistently across formats.")
            next_path = [*path, key]
            if isinstance(value, dict) and format != "env":
                visit(value, next_path)
            else:
                add(next_path, value)

    visit(data)
    return normalized
