"""Tests for the entry point's CLI/env-var configuration."""

from scietex.adc_service.config import ADC_CONFIG_SUBDIR
from scietex.adc_service.run_worker import (
    DEFAULT_LOGGING_LEVEL,
    DEFAULT_SERVICE_NAME,
    ENV_LOGGING_LEVEL,
    ENV_SERVICE_NAME,
    _build_config,
    _build_parser,
)
from scietex.adc_service.version import __version__


def test_defaults_when_no_env_or_flags(monkeypatch) -> None:
    """Without env vars or flags, the built-in defaults apply."""
    monkeypatch.delenv(ENV_SERVICE_NAME, raising=False)
    monkeypatch.delenv(ENV_LOGGING_LEVEL, raising=False)

    args = _build_parser().parse_args([])

    assert args.service_name == DEFAULT_SERVICE_NAME
    assert args.logging_level == DEFAULT_LOGGING_LEVEL
    assert args.conf_dir is None


def test_env_vars_supply_defaults(monkeypatch) -> None:
    """Environment variables override the built-in defaults."""
    monkeypatch.setenv(ENV_SERVICE_NAME, "FromEnv")
    monkeypatch.setenv(ENV_LOGGING_LEVEL, "DEBUG")

    args = _build_parser().parse_args([])

    assert args.service_name == "FromEnv"
    assert args.logging_level == "DEBUG"


def test_cli_flags_win_over_env(monkeypatch) -> None:
    """An explicit CLI flag takes precedence over the environment."""
    monkeypatch.setenv(ENV_SERVICE_NAME, "FromEnv")
    monkeypatch.setenv(ENV_LOGGING_LEVEL, "DEBUG")

    args = _build_parser().parse_args(["--service-name", "FromFlag", "--logging-level", "WARNING"])

    assert args.service_name == "FromFlag"
    assert args.logging_level == "WARNING"


def test_build_config_namespaces_framework_snapshot(monkeypatch) -> None:
    """The framework snapshot is namespaced under the adc subdir."""
    monkeypatch.delenv(ENV_SERVICE_NAME, raising=False)
    monkeypatch.delenv(ENV_LOGGING_LEVEL, raising=False)
    args = _build_parser().parse_args([])

    config = _build_config(args)

    assert config.config_file == f"{ADC_CONFIG_SUBDIR}/config.yml"
    assert config.remote_config_enabled is True
    assert config.valkey_config is None


def test_build_config_reports_package_version(monkeypatch) -> None:
    """The worker reports its own package version, not the framework default."""
    monkeypatch.delenv(ENV_SERVICE_NAME, raising=False)
    monkeypatch.delenv(ENV_LOGGING_LEVEL, raising=False)
    args = _build_parser().parse_args([])

    config = _build_config(args)

    assert config.version == __version__
