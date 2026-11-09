"""Tests for the per-device poll loop, readings encoding, and backoff."""

import asyncio
import logging

import pytest
from conftest import FakeADC, FakeAdcChannel

from scietex.adc_service.config import (
    AdcChannelSettings,
    AdcDeviceSettings,
    AdcServiceSettings,
)
from scietex.adc_service.devices import AdcDeviceRegistry, poll_plan
from scietex.adc_service.poller import (
    MAX_BACKOFF_MS,
    AdcPoller,
    backoff_ms,
    encode_value,
    readings_key,
)


class RecordingStreamClient:
    """Valkey client double that records every ``xadd``."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, list[tuple[bytes, bytes]], object]] = []

    async def xadd(self, key, fields, options=None):
        self.entries.append((key, fields, options))

    def keys(self) -> list[str]:
        return [key for key, _, _ in self.entries]

    def count(self, key: str) -> int:
        return sum(1 for entry_key in self.keys() if entry_key == key)


class FailingChannel:
    """Channel double whose read always raises."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    @property
    def value(self) -> int:
        raise self.exc


class FakeClock:
    """Monotonic clock double advanced by the driven ``_sleep``."""

    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> float:
        return self.ms / 1000.0

    def advance_ms(self, delay_s: float) -> None:
        self.ms += round(delay_s * 1000)


def _settings(*channels: AdcChannelSettings, **service) -> AdcServiceSettings:
    return AdcServiceSettings(
        devices={
            "adc0": AdcDeviceSettings(
                type="FAKE",
                channels=list(channels),
            )
        },
        **service,
    )


def _poller(settings: AdcServiceSettings, client: RecordingStreamClient) -> tuple[AdcPoller, AdcDeviceRegistry]:
    registry = AdcDeviceRegistry(logging.getLogger("test.adc_service.poller"))
    registry.rebuild(settings)
    poller = AdcPoller(
        registry,
        logging.getLogger("test.adc_service.poller.loop"),
        service_name="AdcService",
        instance_id="inst-1",
        client_provider=lambda: client,
        clock=FakeClock(),
    )
    poller.configure(settings)
    return poller, registry


def test_readings_key_is_the_frozen_contract() -> None:
    """The API builds this key from the same template; pin it literally."""
    key = readings_key("AdcService", "inst-1", "adc0", 0)
    assert key == "scietex:AdcService:inst-1:adc:adc0:0:readings"


def test_encode_value_keeps_none_distinguishable_from_zero() -> None:
    """``None`` is the empty field; a real zero round-trips through repr."""
    assert encode_value(None) == b""
    assert encode_value(0.0) == b"0.0"
    assert float(encode_value(1.65)) == 1.65


@pytest.mark.parametrize(
    ("interval_ms", "failures", "expected"),
    [
        (1000, 0, 1000),
        (1000, 1, 2000),
        (1000, 2, 4000),
        (1000, 6, MAX_BACKOFF_MS),
        (1000, 20, MAX_BACKOFF_MS),
    ],
)
def test_backoff_is_exponential_and_capped(interval_ms: int, failures: int, expected: int) -> None:
    assert backoff_ms(interval_ms, failures) == expected


@pytest.mark.asyncio
async def test_one_task_per_device_not_per_channel(fake_parts) -> None:
    """SPI is not concurrency-safe: a device is sampled by exactly one task."""
    settings = _settings(AdcChannelSettings(channel=0), AdcChannelSettings(channel=1))
    poller, _ = _poller(settings, RecordingStreamClient())

    await poller.start()

    assert list(poller.tasks) == ["adc0"]
    assert poller.tasks["adc0"].get_name() == "adc-poll-adc0"
    await poller.stop()
    assert poller.tasks == {}


@pytest.mark.asyncio
async def test_per_channel_cadence_publishes_to_each_stream(fake_parts) -> None:
    """A 100 ms and a 300 ms channel publish at their own cadence."""
    settings = _settings(
        AdcChannelSettings(channel=0, poll_interval_ms=100),
        AdcChannelSettings(channel=1, poll_interval_ms=300),
    )
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    registry.get("adc0").channels[0].code = 11
    registry.get("adc0").channels[1].code = 22
    clock = poller._clock
    sleeps: list[float] = []

    async def driven_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance_ms(delay)
        if clock.ms > 900:
            raise asyncio.CancelledError

    poller._sleep = driven_sleep

    await poller.start()
    await asyncio.gather(*poller.tasks.values(), return_exceptions=True)
    await poller.stop()

    assert client.count("scietex:AdcService:inst-1:adc:adc0:0:readings") == 10
    assert client.count("scietex:AdcService:inst-1:adc:adc0:1:readings") == 4
    assert sleeps == pytest.approx([0.1] * 10)
    key, fields, options = client.entries[0]
    assert key == "scietex:AdcService:inst-1:adc:adc0:0:readings"
    assert fields[1] == (b"value", b"11.0")
    assert fields[2] == (b"voltage", b"")
    assert options is not None


@pytest.mark.asyncio
async def test_failed_read_backs_off_and_publishes_nothing(fake_parts) -> None:
    """A failing channel is retried exponentially and never published."""
    settings = _settings(AdcChannelSettings(channel=0), failure_threshold=3)
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    registry.get("adc0").channels = (FailingChannel(OSError("bus error")),)
    clock = poller._clock
    sleeps: list[float] = []

    async def driven_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance_ms(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    poller._sleep = driven_sleep

    await poller.start()
    await asyncio.gather(*poller.tasks.values(), return_exceptions=True)
    await poller.stop()

    assert client.entries == []
    assert sleeps == [2.0, 4.0, 8.0]
    state = poller.states["adc0"]
    assert state.state == "error"
    assert state.consecutive_failures == 3
    assert state.last_error == "bus error"
    assert state.channels[0].value is None


@pytest.mark.asyncio
async def test_success_after_failure_publishes_and_resets(fake_parts) -> None:
    """A recovered bus read resets the counter and resumes the stream."""
    settings = _settings(AdcChannelSettings(channel=0), failure_threshold=2)
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    channel = FakeAdcChannel(0, code=5, v_ref=3.3)
    registry.get("adc0").channels = (channel,)
    await poller.start()
    try:
        bad = FailingChannel(OSError("gone"))
        registry.get("adc0").channels = (bad,)
        await poller._read_and_publish("adc0", 0)
        await poller._read_and_publish("adc0", 0)
        assert poller.states["adc0"].state == "error"

        registry.get("adc0").channels = (FakeAdcChannel(0, code=512, v_ref=3.3),)
        assert await poller._read_and_publish("adc0", 0) is True
    finally:
        await poller.stop()

    assert poller.states["adc0"].state == "ok"
    assert poller.states["adc0"].consecutive_failures == 0
    assert poller.states["adc0"].channels[0].value == 512.0
    assert client.count("scietex:AdcService:inst-1:adc:adc0:0:readings") == 1


@pytest.mark.asyncio
async def test_no_task_for_poll_false_device(fake_parts) -> None:
    """A device excluded from the plan keeps a stopped telemetry entry."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE", poll=False)})
    poller, _ = _poller(settings, RecordingStreamClient())

    await poller.start()

    assert poller.tasks == {}
    assert poller.states["adc0"].state == "stopped"
    await poller.stop()


def test_plan_has_no_entry_without_channel_rows(fake_parts) -> None:
    """A device with no configured rows yields no plan, so the loop stays silent."""
    settings = _settings()
    registry = AdcDeviceRegistry(logging.getLogger("test.adc_service.poller.plan"))
    registry.rebuild(settings)

    assert poll_plan(registry) == {}


def test_registry_generates_fake_geometry() -> None:
    """The fixture device has the MCP3208 surface the plan expands over."""
    assert len(FakeADC().channels) == 8
