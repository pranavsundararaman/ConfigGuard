import json
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

def parse_config(filename: str, content: str) -> dict[str, Any]:
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
        raw_values = dotenv_values(stream=StringIO(content))

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
