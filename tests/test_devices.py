"""Tests for the ADC device builder and registry."""

import pytest

from scietex.adc_service import devices
from scietex.adc_service.config import AdcChannelSettings, AdcDeviceSettings, AdcServiceSettings
from scietex.adc_service.devices import (
    ADC_PARTS,
    MAX_INTERVAL_MS,
    MIN_INTERVAL_MS,
    AdcDeviceRegistry,
    ChannelNotFound,
    DeviceNotFound,
    DeviceOpenError,
    DeviceUnavailable,
    InvalidParameter,
    UnknownDeviceType,
    UnknownParameter,
    build_adc,
    poll_plan,
)
from scietex.hal import adc as hal


class StubTransport:
    """SPI transport double: no hardware, scripted response, call log."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []
        self.closed = False

    def transfer(self, tx: list[int]) -> list[int]:
        self.calls.append(list(tx))
        return [0x00, 0x0A, 0xBC]

    def close(self) -> None:
        self.closed = True


def test_real_parts_resolve() -> None:
    """Both MCP320x classes are registered under their part names."""
    assert ADC_PARTS["MCP3204"].cls is hal.MCP3204
    assert ADC_PARTS["MCP3208"].cls is hal.MCP3208


def test_real_mcp3208_builds_from_transport_without_io() -> None:
    """The real class opens from a caller-supplied transport and reads on demand."""
    transport = StubTransport()
    device = hal.MCP3208(transport=transport)

    assert transport.calls == []
    assert len(device.channels) == 8
    assert len(device.differential) == 4
    assert device.v_ref is None
    assert device.closed is False

    assert device.channel(0).value == 0xABC
    assert transport.calls == [[0x06, 0x00, 0x00]]

    device.close()
    assert transport.closed is True


def test_unknown_type_is_rejected_with_supported_list() -> None:
    """An unsupported part names the supported ones in the error."""
    with pytest.raises(UnknownDeviceType) as excinfo:
        build_adc("adc0", AdcDeviceSettings(type="ADS1115"))

    assert "MCP3204" in str(excinfo.value)
    assert "MCP3208" in str(excinfo.value)


def test_builder_forwards_only_set_fields(fake_parts: None) -> None:
    """A settings field left at None is not forwarded to the constructor."""
    device = build_adc("adc0", AdcDeviceSettings(type="FAKE", v_ref=4.096, label="cell"))

    assert device.init_kwargs == {"v_ref": 4.096, "label": "cell"}


def test_builder_accepts_v_ref_none(fake_parts: None) -> None:
    """v_ref=None means "not configured" and still builds the device."""
    device = build_adc("adc0", AdcDeviceSettings(type="FAKE", v_ref=None))

    assert device.init_kwargs == {}
    assert device.v_ref is None


def test_builder_forwards_port_as_integer_bus_number(fake_parts: None) -> None:
    """port is the SPI bus number, forwarded to the HAL as an int."""
    device = build_adc("adc0", AdcDeviceSettings(type="FAKE", port=0, device=1))

    assert device.init_kwargs["port"] == 0
    assert type(device.init_kwargs["port"]) is int
    assert device.init_kwargs["device"] == 1
    assert type(device.init_kwargs["device"]) is int


def test_builder_rejects_unknown_parameter(fake_parts: None) -> None:
    """A field the class signature lacks is a config error, not a TypeError."""
    with pytest.raises(UnknownParameter):
        build_adc("adc0", AdcDeviceSettings(type="NOPINS", clock_pin=18))


def test_builder_wraps_oserror_as_open_error(fake_parts: None) -> None:
    """A constructor failure at the bus level becomes DeviceOpenError."""
    with pytest.raises(DeviceOpenError) as excinfo:
        build_adc("broken", AdcDeviceSettings(type="BROKEN"))

    assert excinfo.value.device_id == "broken"
    assert isinstance(excinfo.value.cause, OSError)


def test_builder_rejects_invalid_parameter_value(fake_parts: None) -> None:
    """A value the constructor rejects becomes InvalidParameter."""
    with pytest.raises(InvalidParameter):
        build_adc("adc0", AdcDeviceSettings(type="FAKE", v_ref=0.0))


def test_registry_opens_configured_devices(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """A successful rebuild exposes sorted ids and a live object."""
    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}))

    assert registry.ids() == ["adc0"]
    assert registry.errors == {}
    assert registry.generation == 1
    assert registry.get("adc0").v_ref is None


def test_registry_records_bad_device_and_keeps_others(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """One bad entry is recorded while the rest still open."""
    registry.rebuild(
        AdcServiceSettings(
            devices={
                "bad": AdcDeviceSettings(type="ADS1115"),
                "good": AdcDeviceSettings(type="FAKE"),
            }
        )
    )

    assert registry.ids() == ["bad", "good"]
    assert list(registry.errors) == ["bad"]
    assert registry.get("good").closed is False
    with pytest.raises(DeviceUnavailable):
        registry.get("bad")


def test_registry_records_open_failure(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """A bus-level open failure is recorded, not raised."""
    registry.rebuild(AdcServiceSettings(devices={"broken": AdcDeviceSettings(type="BROKEN")}))

    assert registry.ids() == ["broken"]
    assert "Failed to open device" in registry.errors["broken"]
    with pytest.raises(DeviceUnavailable) as excinfo:
        registry.get("broken")
    assert "SPI bus is gone" in str(excinfo.value)
    assert excinfo.value.device_id == "broken"
    assert excinfo.value.reason == registry.errors["broken"]


def test_close_closes_all_and_is_idempotent(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """close() closes every device, clears the set, and tolerates a second call."""
    registry.rebuild(AdcServiceSettings(devices={"a": AdcDeviceSettings(type="FAKE"), "b": AdcDeviceSettings(type="FAKE")}))
    first = registry.get("a")
    second = registry.get("b")

    registry.close()

    assert first.closed is True
    assert second.closed is True
    # The ids stay configured, so a closed registry reports them unavailable
    # rather than unknown.
    with pytest.raises(DeviceUnavailable):
        registry.get("a")
    registry.close()


def test_get_unknown_id_raises_device_not_found(registry: AdcDeviceRegistry) -> None:
    """An id that was never configured is a DeviceNotFound, not a raw KeyError."""
    with pytest.raises(DeviceNotFound) as excinfo:
        registry.get("nope")

    assert "nope" in str(excinfo.value)


def test_get_configured_id_that_failed_is_unavailable_not_not_found(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """The taxonomy separates a wrong id from a configured device that did not open."""
    registry.rebuild(AdcServiceSettings(devices={"broken": AdcDeviceSettings(type="BROKEN")}))

    with pytest.raises(DeviceUnavailable) as excinfo:
        registry.get("broken")
    assert "Failed to open device 'broken'" in str(excinfo.value)

    with pytest.raises(DeviceNotFound) as notfound:
        registry.get("missing")
    assert "is not configured" in str(notfound.value)


def test_channel_out_of_range_raises_channel_not_found(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """Bounds are checked against the live device before the HAL accessor."""
    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}))

    assert registry.channel("adc0", 0).index == 0
    with pytest.raises(ChannelNotFound):
        registry.channel("adc0", 8)
    with pytest.raises(ChannelNotFound):
        registry.differential_channel("adc0", 4)


def test_rebuild_closes_old_generation_and_bumps_generation(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """Changed settings close the old objects and install a new generation."""
    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}))
    old = registry.get("adc0")

    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE", v_ref=5.0)}))

    assert old.closed is True
    assert registry.generation == 2
    assert registry.get("adc0") is not old
    assert registry.get("adc0").v_ref == 5.0


def test_identical_settings_skip_rebuild(
    registry: AdcDeviceRegistry, fake_parts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same effective settings with a live device do not churn the hardware."""
    calls: list[str] = []
    original = devices.build_adc

    def recording_build(device_id: str, cfg: AdcDeviceSettings) -> hal.ADC:
        calls.append(device_id)
        return original(device_id, cfg)

    monkeypatch.setattr(devices, "build_adc", recording_build)
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})

    registry.rebuild(settings)
    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}))

    assert calls == ["adc0"]
    assert registry.generation == 1


def test_errors_cleared_on_successful_rebuild(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """A rebuild that no longer contains a failing device drops its error."""
    registry.rebuild(
        AdcServiceSettings(devices={"bad": AdcDeviceSettings(type="ADS1115"), "good": AdcDeviceSettings(type="FAKE")})
    )
    assert "bad" in registry.errors

    registry.rebuild(AdcServiceSettings(devices={"good": AdcDeviceSettings(type="FAKE")}))

    assert registry.errors == {}
    assert registry.ids() == ["good"]


def test_poll_targets_honours_poll_false(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """Only devices with poll=True are polling targets."""
    registry.rebuild(
        AdcServiceSettings(
            devices={
                "polled": AdcDeviceSettings(type="FAKE"),
                "manual": AdcDeviceSettings(type="FAKE", poll=False),
            }
        )
    )

    assert [device_id for device_id, _ in registry.poll_targets()] == ["polled"]


def test_poll_targets_empty_before_first_rebuild(registry: AdcDeviceRegistry) -> None:
    """No settings means no polling targets and no known ids."""
    assert registry.poll_targets() == []
    assert registry.ids() == []
    assert registry.settings is None


def test_registry_logs_close_failure(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """A failing close() is logged and does not prevent the rebuild."""
    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="STUBBORN")}))
    failing = registry.get("adc0")

    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="STUBBORN", v_ref=3.3)}))

    assert registry.generation == 2
    assert registry.get("adc0") is not failing
    assert registry.get("adc0").v_ref == 3.3


def test_poll_plan_empty_channels_is_not_polled(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """A device with no channel rows is silent: inputs are sampled only via rows."""
    registry.rebuild(AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}))

    assert poll_plan(registry) == {}


def test_poll_plan_explicit_rows_override_period_and_carry_labels(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """Each explicit row carries its own period and label."""
    registry.rebuild(
        AdcServiceSettings(
            devices={
                "adc0": AdcDeviceSettings(
                    type="FAKE",
                    channels=[
                        AdcChannelSettings(channel=2, poll_interval_ms=100, label="cell"),
                        AdcChannelSettings(channel=5),
                    ],
                )
            }
        )
    )

    plan = poll_plan(registry)["adc0"]

    assert [(entry.channel, entry.interval_ms, entry.label) for entry in plan] == [
        (2, 100, "cell"),
        (5, 1000, None),
    ]


def test_poll_plan_drops_disabled_out_of_range_and_duplicate_rows(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """A row that cannot be sampled is dropped instead of failing every tick."""
    registry.rebuild(
        AdcServiceSettings(
            devices={
                "adc0": AdcDeviceSettings(
                    type="FAKE",
                    channels=[
                        AdcChannelSettings(channel=0),
                        AdcChannelSettings(channel=0, poll_interval_ms=200),
                        AdcChannelSettings(channel=1, enabled=False),
                        AdcChannelSettings(channel=99),
                    ],
                )
            }
        )
    )

    plan = poll_plan(registry)["adc0"]

    assert [(entry.channel, entry.interval_ms) for entry in plan] == [(0, 1000)]


def test_poll_plan_skips_poll_false_and_disabled_service(registry: AdcDeviceRegistry, fake_parts: None) -> None:
    """Neither a device opt-out nor the service switch produces a plan."""
    settings = AdcServiceSettings(
        devices={
            "polled": AdcDeviceSettings(type="FAKE", channels=[AdcChannelSettings(channel=0)]),
            "manual": AdcDeviceSettings(type="FAKE", poll=False, channels=[AdcChannelSettings(channel=0)]),
        }
    )
    registry.rebuild(settings)

    assert list(poll_plan(registry)) == ["polled"]

    registry.rebuild(
        AdcServiceSettings(
            devices={
                "polled": AdcDeviceSettings(type="FAKE", channels=[AdcChannelSettings(channel=0)]),
                "manual": AdcDeviceSettings(type="FAKE", poll=False, channels=[AdcChannelSettings(channel=0)]),
            },
            poll_enabled=False,
        )
    )
    assert poll_plan(registry) == {}


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (0, MIN_INTERVAL_MS),
        (-50, MIN_INTERVAL_MS),
        (100, 100),
        (MAX_INTERVAL_MS + 1, MAX_INTERVAL_MS),
    ],
)
def test_poll_plan_clamps_intervals(
    registry: AdcDeviceRegistry,
    fake_parts: None,
    caplog: pytest.LogCaptureFixture,
    configured: int,
    expected: int,
) -> None:
    """A period outside the supported range degrades to the bound with a warning."""
    registry.rebuild(
        AdcServiceSettings(
            devices={
                "adc0": AdcDeviceSettings(
                    type="FAKE",
                    channels=[AdcChannelSettings(channel=0, poll_interval_ms=configured)],
                )
            }
        )
    )

    plan = poll_plan(registry)["adc0"]

    assert plan[0].interval_ms == expected
    clamped = configured != expected
    assert bool(caplog.records) is clamped


def test_poll_plan_empty_before_first_rebuild(registry: AdcDeviceRegistry) -> None:
    """No settings means no plan."""
    assert poll_plan(registry) == {}
