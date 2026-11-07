"""Tests for the telemetry payload builder and the Telemetry manager loop."""

import logging

import msgspec
import pytest
from fake_worker import FailingClient, FakeAdcWorker, RecordingClient
from glide import ExpiryType, RequestError

from scietex.adc_service.adc_worker import AdcWorker
from scietex.adc_service.config import AdcDeviceSettings, AdcServiceSettings
from scietex.adc_service.devices import AdcDeviceRegistry
from scietex.adc_service.telemetry import (
    TELEMETRY_INTERVAL,
    TELEMETRY_TTL,
    AdcTelemetry,
    build_telemetry,
)
from scietex.service import ValkeyWorkerConfig


def _registry(settings: AdcServiceSettings) -> AdcDeviceRegistry:
    registry = AdcDeviceRegistry(logging.getLogger("test.adc_service.telemetry"))
    registry.rebuild(settings)
    return registry


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
    telemetry = build_telemetry(FakeAdcWorker())

    assert telemetry.service == "adc-test"
    assert telemetry.instance_id == "instance-1"
    assert telemetry.configured is False
    assert telemetry.poll_enabled is True
    assert telemetry.devices == []
    assert telemetry.schema_version == 1


def test_build_telemetry_reports_open_devices(fake_parts) -> None:
    """Open devices report their live geometry, reference, and poll flag."""
    settings = AdcServiceSettings(
        devices={
            "adc1": AdcDeviceSettings(type="FAKE", poll=False),
            "adc0": AdcDeviceSettings(type="FAKE", v_ref=3.3),
        }
    )
    telemetry = build_telemetry(FakeAdcWorker(settings=settings, registry=_registry(settings)))

    assert telemetry.configured is True
    assert telemetry.poll_enabled is True
    assert [device.device_id for device in telemetry.devices] == ["adc0", "adc1"]
    first, second = telemetry.devices
    assert first.available is True
    assert first.error is None
    assert first.v_ref == 3.3
    assert first.channels == 8
    assert first.differential == 4
    assert first.poll is True
    assert second.available is True
    assert second.v_ref is None
    assert second.poll is False


def test_build_telemetry_reports_open_failure(fake_parts) -> None:
    """A configured device that failed to open is still listed with its reason."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="BROKEN", v_ref=5.0)})
    telemetry = build_telemetry(FakeAdcWorker(settings=settings, registry=_registry(settings)))

    assert telemetry.configured is True
    [device] = telemetry.devices
    assert device.device_id == "adc0"
    assert device.available is False
    assert device.error is not None
    assert "SPI bus is gone" in device.error
    assert device.v_ref == 5.0
    assert device.channels == 0
    assert device.differential == 0


def test_build_telemetry_reports_poll_disabled(fake_parts) -> None:
    """The service-wide polling switch is visible even with no devices."""
    settings = AdcServiceSettings(poll_enabled=False)
    telemetry = build_telemetry(FakeAdcWorker(settings=settings, registry=_registry(settings)))

    assert telemetry.configured is True
    assert telemetry.poll_enabled is False
    assert telemetry.devices == []


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

    decoded = msgspec.msgpack.decode(value, type=AdcTelemetry)
    assert decoded.configured is False
    assert decoded.devices == []
    assert decoded.schema_version == 1


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
