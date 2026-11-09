"""Tests for the telemetry payload builder and the Telemetry manager loop."""

import logging

import msgspec
import pytest
from fake_worker import FailingClient, FakeAdcWorker, RecordingClient
from glide import ExpiryType, RequestError

from scietex.adc_service import telemetry as telemetry_module
from scietex.adc_service.adc_worker import AdcWorker
from scietex.adc_service.config import AdcDeviceSettings, AdcServiceSettings
from scietex.adc_service.devices import AdcDeviceRegistry
from scietex.adc_service.poller import AdcPoller
from scietex.adc_service.telemetry import (
    SCHEMA_VERSION,
    TELEMETRY_INTERVAL,
    TELEMETRY_TTL,
    build_telemetry,
)
from scietex.service import ValkeyWorkerConfig


class ToggleChannel:
    """Channel double whose read fails while ``exc`` is set."""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.exc: Exception | None = None

    @property
    def value(self) -> int:
        if self.exc is not None:
            raise self.exc
        return self.code


def _registry(settings: AdcServiceSettings) -> AdcDeviceRegistry:
    registry = AdcDeviceRegistry(logging.getLogger("test.adc_service.telemetry"))
    registry.rebuild(settings)
    return registry


def _poller(registry: AdcDeviceRegistry, settings: AdcServiceSettings, **kwargs) -> AdcPoller:
    poller = AdcPoller(
        registry,
        logging.getLogger("test.adc_service.telemetry.poller"),
        service_name="adc-test",
        instance_id="instance-1",
        **kwargs,
    )
    poller.configure(settings)
    return poller


def _make_worker(tmp_path) -> AdcWorker:
    """Build a real worker with a conf dir and no remote-config handlers."""
    return AdcWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


def test_telemetry_interval_and_ttl() -> None:
    """The push cadence and TTL are the frozen contract."""
    assert TELEMETRY_INTERVAL == 2.0
    assert TELEMETRY_TTL == 6


def test_build_telemetry_before_initialize_is_unconfigured() -> None:
    """A pre-startup worker reports configured=False with no devices."""
    payload = build_telemetry(FakeAdcWorker())

    assert payload["service"] == "adc-test"
    assert payload["instance_id"] == "instance-1"
    assert payload["configured"] is False
    assert payload["poll_enabled"] is True
    assert payload["state"] == "stopped"
    assert payload["devices"] == {}
    assert payload["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_build_telemetry_reports_channels_and_states(fake_parts) -> None:
    """A polled device reports its state, type, and per-channel snapshots."""
    settings = AdcServiceSettings(
        devices={
            "adc1": AdcDeviceSettings(type="FAKE", poll=False),
            "adc0": AdcDeviceSettings(type="FAKE", v_ref=10.24),
        }
    )
    registry = _registry(settings)
    poller = _poller(registry, settings)
    registry.get("adc0").channels[2].code = 512
    await poller.start()
    try:
        assert await poller._read_and_publish("adc0", 2) is True
    finally:
        await poller.stop()

    payload = build_telemetry(FakeAdcWorker(settings=settings, registry=registry, poller=poller))

    assert payload["configured"] is True
    assert payload["state"] == "ok"
    assert list(payload["devices"]) == ["adc0", "adc1"]
    polled = payload["devices"]["adc0"]
    assert polled["state"] == "ok"
    assert polled["type"] == "FAKE"
    assert polled["last_error"] is None
    assert polled["last_ok"] is not None
    channel = polled["channels"]["2"]
    assert channel["value"] == 512.0
    assert channel["voltage"] == pytest.approx(512 * 10.24 / 4096)
    assert channel["ts"] is not None
    assert channel["error"] is None
    stopped = payload["devices"]["adc1"]
    assert stopped["state"] == "stopped"
    assert stopped["channels"] == {}


@pytest.mark.asyncio
async def test_build_telemetry_reports_open_failure(fake_parts) -> None:
    """A configured device that failed to open is reported as an error."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="BROKEN", v_ref=5.0)})
    registry = _registry(settings)
    poller = _poller(registry, settings)
    await poller.start()
    await poller.stop()

    payload = build_telemetry(FakeAdcWorker(settings=settings, registry=registry, poller=poller))

    assert payload["state"] == "error"
    device = payload["devices"]["adc0"]
    assert device["state"] == "error"
    assert device["type"] == "BROKEN"
    assert "SPI bus is gone" in device["last_error"]
    assert device["last_ok"] is None
    assert device["channels"] == {}


@pytest.mark.asyncio
async def test_voltage_stays_none_without_reference(fake_parts) -> None:
    """An unscaled device reports a null voltage, never a zero."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    poller = _poller(registry, settings)
    registry.get("adc0").channels[0].code = 100
    await poller.start()
    try:
        await poller._read_and_publish("adc0", 0)
    finally:
        await poller.stop()

    payload = build_telemetry(FakeAdcWorker(settings=settings, registry=registry, poller=poller))

    channel = payload["devices"]["adc0"]["channels"]["0"]
    assert channel["value"] == 100.0
    assert channel["voltage"] is None


@pytest.mark.asyncio
async def test_error_threshold_flips_state_and_recovery_clears_it(fake_parts) -> None:
    """Consecutive failures reach the threshold; one success restores the device."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}, failure_threshold=2)
    registry = _registry(settings)
    poller = _poller(registry, settings)
    channel = ToggleChannel()
    registry.get("adc0").channels = (channel,)
    await poller.start()
    try:
        channel.exc = OSError("bus error")
        await poller._read_and_publish("adc0", 0)
        await poller._read_and_publish("adc0", 0)
        failed = build_telemetry(FakeAdcWorker(settings=settings, registry=registry, poller=poller))
        assert failed["state"] == "error"
        assert failed["devices"]["adc0"]["state"] == "error"
        assert failed["devices"]["adc0"]["last_error"] == "bus error"
        assert failed["devices"]["adc0"]["channels"]["0"]["value"] is None
        assert failed["devices"]["adc0"]["channels"]["0"]["error"] == "bus error"

        channel.exc = None
        channel.code = 7
        await poller._read_and_publish("adc0", 0)
        recovered = build_telemetry(FakeAdcWorker(settings=settings, registry=registry, poller=poller))
        assert recovered["state"] == "ok"
        assert recovered["devices"]["adc0"]["state"] == "ok"
        assert recovered["devices"]["adc0"]["last_error"] is None
        assert recovered["devices"]["adc0"]["channels"]["0"]["value"] == 7.0
        assert recovered["devices"]["adc0"]["channels"]["0"]["error"] is None
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poll_disabled_reports_stopped(fake_parts) -> None:
    """A configured device is still listed when service-wide polling is off."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")}, poll_enabled=False)
    registry = _registry(settings)
    poller = _poller(registry, settings)
    await poller.start()
    await poller.stop()

    payload = build_telemetry(FakeAdcWorker(settings=settings, registry=registry, poller=poller))

    assert payload["poll_enabled"] is False
    assert payload["devices"]["adc0"]["state"] == "stopped"


@pytest.mark.asyncio
async def test_telemetry_loop_writes_key_with_ttl(tmp_path, monkeypatch) -> None:
    """The manager writes scietex:{service}:{instance}:telemetry with TTL 6."""
    monkeypatch.setattr("scietex.adc_service.adc_worker.TELEMETRY_INTERVAL", 0)
    worker = _make_worker(tmp_path)
    client = RecordingClient()
    monkeypatch.setattr(worker, "_client", client)

    await worker.telemetry_loop()

    assert len(client.calls) == 1
    key, value, expiry = client.calls[0]
    assert key == f"scietex:{worker.service_name}:{worker.instance_id}:telemetry"
    assert expiry.expiry_type == ExpiryType.SEC
    assert int(expiry.value) == 6

    decoded = msgspec.msgpack.decode(value, type=dict)
    assert decoded["configured"] is False
    assert decoded["devices"] == {}
    assert decoded["state"] == "stopped"
    assert decoded["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_telemetry_loop_reports_write_failure(tmp_path, monkeypatch) -> None:
    """A Glide failure is logged and reported to the transport health."""
    monkeypatch.setattr("scietex.adc_service.adc_worker.TELEMETRY_INTERVAL", 0)
    worker = _make_worker(tmp_path)
    client = FailingClient(RequestError("valkey is down"))
    monkeypatch.setattr(worker, "_client", client)

    failures: list[Exception] = []
    monkeypatch.setattr(worker._health, "report_failure", failures.append)

    await worker.telemetry_loop()

    assert client.calls == 1
    assert len(failures) == 1
    assert str(failures[0]) == "valkey is down"


def test_state_constants_are_the_contract() -> None:
    """The three states the API and UI switch on are spelled exactly."""
    assert telemetry_module.STATE_OK == "ok"
    assert telemetry_module.STATE_ERROR == "error"
    assert telemetry_module.STATE_STOPPED == "stopped"
