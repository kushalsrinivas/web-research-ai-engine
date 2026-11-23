from __future__ import annotations

import pytest
from wre_pipeline.config import ConfigurationError, load


def test_defaults() -> None:
    config = load("api", 8080, environ={})
    assert config.env == "development"
    assert config.port == 8080
    assert config.log_level == "info"
    assert config.sample_ratio == 1.0
    assert config.processor_version == "api@dev"


def test_overrides() -> None:
    config = load(
        "api",
        8080,
        environ={
            "APP_ENV": "development",
            "PORT": "9090",
            "LOG_LEVEL": "debug",
            "OTEL_SAMPLE_RATIO": "0.25",
            "SERVICE_VERSION": "0.1.0+abc1234",
        },
    )
    assert config.port == 9090
    assert config.log_level == "debug"
    assert config.sample_ratio == 0.25
    assert config.processor_version == "api@0.1.0+abc1234"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_ENV", "qa"),
        ("PORT", "not-a-port"),
        ("PORT", "70000"),
        ("LOG_LEVEL", "verbose"),
        ("OTEL_SAMPLE_RATIO", "2"),
    ],
)
def test_rejects_bad_values(key: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        load("api", 8080, environ={"APP_ENV": "development", key: value})


def test_staging_requires_infrastructure() -> None:
    with pytest.raises(ConfigurationError):
        load("api", 8080, environ={"APP_ENV": "staging"})
