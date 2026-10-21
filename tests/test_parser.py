import json
import re

import pytest
import yaml

from app.parser import normalize_historical_configuration, parse_config


@pytest.mark.parametrize("filename", ["config.yaml", "config.yml"])
def test_yaml_formats_parse_nested_mappings(filename):
    parsed = parse_config(filename, "service:\n  replicas: 3\n  limits:\n    memory: 512\n")

    assert parsed == {"service": {"replicas": 3, "limits": {"memory": 512}}}


def test_env_parses_dotenv_key_value_semantics_and_typed_scalars():
    parsed = parse_config(
        ".env",
        'export ENABLED=true\nPORT=8080\nRATIO=0.75\nNAME="api service"\nEMPTY=\n',
    )

    assert parsed == {
        "ENABLED": True,
        "PORT": 8080,
        "RATIO": 0.75,
        "NAME": "api service",
        "EMPTY": "",
    }


def test_env_keeps_comments_quotes_exports_and_interpolation_literal():
    parsed = parse_config(
        ".env",
        '# settings\nexport DEBUG=false\nAPP_NAME="ConfigGuard"\nLABEL=${OTHER}\n',
        strict_env=True,
    )

    assert parsed == {"DEBUG": False, "APP_NAME": "ConfigGuard", "LABEL": "${OTHER}"}


def test_json_parsing_regression_keeps_object_values():
    parsed = parse_config("config.json", '{"service":{"replicas":3},"enabled":true}')

    assert parsed == {"service": {"replicas": 3}, "enabled": True}


def test_legacy_env_parser_keeps_python_dotenv_key_without_equals_behavior():
    assert parse_config(".env", "LEGACY_FLAG") == {"LEGACY_FLAG": None}


def test_malformed_yaml_and_json_raise_parser_errors():
    with pytest.raises(yaml.YAMLError):
        parse_config("config.yaml", "service: [unterminated")
    with pytest.raises(json.JSONDecodeError):
        parse_config("config.json", '{"service":')


@pytest.mark.parametrize(
    "content",
    [
        '{"service": {"replicas": 3}}',
        "service:\n  replicas: 3\n",
        "NOT_AN_ASSIGNMENT\n",
        "SERVICE_REPLICAS=1\nSERVICE_REPLICAS=2\n",
    ],
)
def test_env_rejects_json_yaml_and_malformed_assignments(content):
    with pytest.raises(ValueError):
        parse_config(".env", content, strict_env=True)


def test_historical_parameter_names_match_across_nested_and_env_formats():
    nested = {"service": {"replicas": 3, "timeout_seconds": 30}}
    env = {"SERVICE_REPLICAS": 3, "SERVICE_TIMEOUT_SECONDS": 30}

    assert normalize_historical_configuration(nested, "json") == normalize_historical_configuration(env, "env")
    assert normalize_historical_configuration(env, "env") == {
        "service.replicas": 3,
        "service.timeout.seconds": 30,
    }


def test_historical_parameter_normalization_rejects_colliding_paths():
    ambiguous = {"service_timeout": {"seconds": 1}, "service": {"timeout_seconds": 2}}

    with pytest.raises(ValueError, match="collide after format normalization"):
        normalize_historical_configuration(ambiguous, "yaml")

    with pytest.raises(ValueError, match="collide after format normalization"):
        normalize_historical_configuration(
            {"SERVICE_TIMEOUT_SECONDS": 1, "service_timeout_seconds": 2}, "env"
        )


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("config.yaml", "- item\n- item", "YAML root must be an object/mapping."),
        ("config.json", "[1, 2]", "JSON root must be an object/object-like mapping."),
        ("config.toml", "key = 'value'", "Unsupported configuration format."),
    ],
)
def test_non_object_and_unsupported_inputs_are_rejected(filename, content, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_config(filename, content)
