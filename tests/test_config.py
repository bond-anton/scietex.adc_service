"""Tests for the ADC service configuration models and YAML loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.adc_service.config import (
    ADC_CONFIG_FILE,
    ADC_CONFIG_SUBDIR,
    ADC_SETTINGS_DEFAULTS,
    AdcChannelSettings,
    AdcDeviceSettings,
    AdcServiceSettings,
    read_adc_config,
)
from scietex.service.config_merge import resolve_section


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / ADC_CONFIG_SUBDIR / ADC_CONFIG_FILE


def test_defaults_constant_is_l0_base() -> None:
    """The exported L0 base is a concrete default struct instance."""
    assert isinstance(ADC_SETTINGS_DEFAULTS, AdcServiceSettings)
    assert ADC_SETTINGS_DEFAULTS == AdcServiceSettings()


def test_read_adc_config_writes_defaults_when_missing(tmp_path: Path) -> None:
    """A missing file is created under the adc/ subdir with the default patch."""
    patch = read_adc_config(tmp_path)

    assert patch == msgspec.to_builtins(AdcServiceSettings())
    path = _config_path(tmp_path)
    assert path.is_file()
    assert msgspec.yaml.decode(path.read_bytes(), type=dict) == patch


def test_read_adc_config_returns_dict_not_struct(tmp_path: Path) -> None:
    """The loader returns the L1 patch dict, never a struct."""
    patch = read_adc_config(tmp_path)

    assert type(patch) is dict
    assert not isinstance(patch, AdcServiceSettings)


def test_read_adc_config_none_dir_raises() -> None:
    """A None conf_dir is rejected with RuntimeError."""
    with pytest.raises(RuntimeError):
        read_adc_config(None)


def test_read_adc_config_missing_without_create_raises(tmp_path: Path) -> None:
    """A missing file with create_default=False raises and writes nothing."""
    with pytest.raises(RuntimeError):
        read_adc_config(tmp_path, create_default=False)

    assert not _config_path(tmp_path).exists()


def test_read_adc_config_malformed_raises_and_leaves_file(tmp_path: Path) -> None:
    """An unparseable file raises RuntimeError and is left untouched."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("unterminated: [flow\n")

    with pytest.raises(RuntimeError):
        read_adc_config(tmp_path)

    assert path.read_text() == "unterminated: [flow\n"


def test_read_adc_config_partial_file_returns_only_present_keys(tmp_path: Path) -> None:
    """A partial file yields a partial patch — absent keys inherit the layer below."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("poll_enabled: false\n")

    assert read_adc_config(tmp_path) == {"poll_enabled": False}


def test_string_device_ids_survive_round_trip() -> None:
    """Device ids stay plain strings through YAML, builtins, and decode."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="MCP3208", select_pin="CE0")})

    encoded = msgspec.yaml.encode(settings)
    decoded = msgspec.yaml.decode(encoded, type=AdcServiceSettings)

    assert b"adc0:" in encoded
    assert list(msgspec.to_builtins(settings)["devices"]) == ["adc0"]
    assert list(decoded.devices) == ["adc0"]
    assert all(isinstance(key, str) for key in decoded.devices)


def test_four_layer_precedence() -> None:
    """L3 beats L2 beats L1 beats L0, and absent keys inherit the layer below."""
    l1 = {"poll_enabled": False, "devices": {"adc0": {"type": "MCP3208", "v_ref": 3.3}}}
    l2 = {"devices": {"adc0": {"v_ref": 4.096, "label": "cell"}}}
    l3 = {"devices": {"adc0": {"label": "from-remote"}}}

    settings = resolve_section(AdcServiceSettings, [l3, l2, l1], defaults=ADC_SETTINGS_DEFAULTS)

    assert settings.poll_enabled is False
    assert settings.devices["adc0"].type == "MCP3208"
    assert settings.devices["adc0"].v_ref == 4.096
    assert settings.devices["adc0"].label == "from-remote"
    assert settings.devices["adc0"].poll is True


def test_null_resets_field_to_l0() -> None:
    """A null in a higher layer clears the field so the L0 value resurfaces."""
    l1 = {"poll_enabled": False, "devices": {"adc0": {"type": "MCP3208", "v_ref": 3.3}}}
    l3 = {"poll_enabled": None, "devices": {"adc0": {"v_ref": None}}}

    settings = resolve_section(AdcServiceSettings, [l3, l1], defaults=ADC_SETTINGS_DEFAULTS)

    assert settings.poll_enabled is True
    assert settings.devices["adc0"].v_ref is None


def test_forbid_unknown_fields_rejects_vref() -> None:
    """The struct field is ``v_ref``; ``vref`` is rejected, not silently ignored."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "MCP3208", "vref": 3.3}, AdcDeviceSettings)


def test_devices_must_be_a_mapping() -> None:
    """A non-mapping ``devices`` fails validation instead of coercing."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"devices": "adc0"}, AdcServiceSettings)


def test_type_is_required_per_device() -> None:
    """A device entry without ``type`` cannot be validated."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"devices": {"adc0": {"port": 0}}}, AdcServiceSettings)


def test_port_is_an_integer_bus_number() -> None:
    """``port`` is the SPI bus number, so a device path is rejected."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "MCP3208", "port": "/dev/spidev0.0"}, AdcDeviceSettings)

    settings = msgspec.convert({"type": "MCP3208", "port": 0, "device": 0}, AdcDeviceSettings)

    assert settings.port == 0
    assert settings.device == 0


def test_poll_false_is_preserved() -> None:
    """Per-device ``poll: false`` survives struct construction and YAML."""
    settings = msgspec.convert({"devices": {"adc0": {"type": "MCP3208", "poll": False}}}, AdcServiceSettings)

    assert settings.devices["adc0"].poll is False
    assert msgspec.to_builtins(settings)["devices"]["adc0"]["poll"] is False


def test_poll_enabled_defaults_true() -> None:
    """Service-wide polling is on unless configured otherwise."""
    assert AdcServiceSettings().poll_enabled is True
    assert AdcServiceSettings().devices == {}


def test_channel_settings_decode_from_yaml_shaped_dict() -> None:
    """A channel row the API publishes decodes with every field present."""
    channel = msgspec.convert(
        {"channel": 3, "poll_interval_ms": 250, "enabled": False, "label": "cell"},
        AdcChannelSettings,
    )

    assert channel.channel == 3
    assert channel.poll_interval_ms == 250
    assert channel.enabled is False
    assert channel.label == "cell"


def test_channel_settings_defaults() -> None:
    """Only ``channel`` is required; period, enablement, and label default."""
    channel = AdcChannelSettings(channel=0)

    assert channel.poll_interval_ms == 1000
    assert channel.enabled is True
    assert channel.label is None


def test_channel_settings_reject_unknown_field() -> None:
    """A field the worker does not know fails the whole apply, not silently."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"channel": 0, "interval": 100}, AdcChannelSettings)


def test_device_poll_defaults_and_channels() -> None:
    """A device entry without polling fields samples every input every second."""
    device = msgspec.convert({"type": "MCP3208"}, AdcDeviceSettings)

    assert device.poll is True
    assert device.poll_interval_ms == 1000
    assert device.channels == []


def test_service_poll_defaults() -> None:
    """The service-wide polling knobs mirror the VFD domain defaults."""
    settings = AdcServiceSettings()

    assert settings.poll_enabled is True
    assert settings.failure_threshold == 3
    assert settings.stream_maxlen == 10000


def test_channels_round_trip_through_yaml() -> None:
    """A per-channel plan survives YAML, including the disabled rows."""
    settings = AdcServiceSettings(
        devices={
            "adc0": AdcDeviceSettings(
                type="MCP3208",
                channels=[AdcChannelSettings(channel=0, poll_interval_ms=100), AdcChannelSettings(channel=1, enabled=False)],
            )
        }
    )

    decoded = msgspec.yaml.decode(msgspec.yaml.encode(settings), type=AdcServiceSettings)

    assert decoded == settings
    assert decoded.devices["adc0"].channels[1].enabled is False
