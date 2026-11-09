"""Per-device sampling loops for the configured SPI ADC converters.

SPI transactions are not concurrency-safe on one chip select, so a device is
sampled by **one** asyncio task that keeps a due time per channel and sleeps
until the earliest one — never one task per channel. Each due read goes through
`devices.read_channel`, the same helper ``adc:read`` uses, so a polled and an
on-demand value come from the same accessor and scaling.

A failed read never stops the loop: the device failure counter grows, the
channel is backed off exponentially (capped at a minute), no reading is
published — a gap is the signal — and the last error is kept in the in-memory
snapshot `telemetry.build_telemetry` turns into the Valkey payload. At
``failure_threshold`` consecutive failures the device is reported as ``error``;
the loop keeps polling, so clearing a transient bus fault needs no config apply.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import msgspec
from glide import (
    ConnectionError as GlideConnectionError,
)
from glide import (
    RequestError,
)
from glide import (
    TimeoutError as GlideTimeoutError,
)

from scietex.service.valkey._glide import StreamAddOptions, TrimByMaxLen

from .config import AdcServiceSettings
from .devices import (
    AdcChannelPlan,
    AdcDeviceRegistry,
    ChannelReading,
    poll_plan,
    read_channel,
)

#: Device states reported in telemetry.
STATE_OK: str = "ok"
STATE_ERROR: str = "error"
STATE_STOPPED: str = "stopped"

#: Shortest sleep the loop takes, so a due time already in the past still
#: yields to the event loop instead of spinning.
MIN_SLEEP_S: float = 0.01

#: Cap on a failed channel's exponential backoff.
MAX_BACKOFF_MS: int = 60_000

#: Defaults for a poller configured before the first settings apply.
DEFAULT_FAILURE_THRESHOLD: int = 3
DEFAULT_STREAM_MAXLEN: int = 10000

#: Template of the per-channel readings stream. Frozen contract: the API's
#: stream reader builds the same key, and nothing type-checks across that
#: boundary, so a literal-string test pins it on this side.
READINGS_STREAM_TEMPLATE: str = "scietex:{service}:{instance_id}:adc:{device_id}:{channel}:readings"


def readings_key(service_name: str, instance_id: str, device_id: str, channel: int) -> str:
    """Build the readings stream key for one device channel."""
    return READINGS_STREAM_TEMPLATE.format(
        service=service_name,
        instance_id=instance_id,
        device_id=device_id,
        channel=channel,
    )


def encode_value(value: float | None) -> bytes:
    """Encode a reading value: ``b""`` for `None`, otherwise ``repr``.

    ``repr`` round-trips a float exactly, so the API's re-parse is lossless;
    an empty value keeps "not reported" distinguishable from a real zero.
    """
    return b"" if value is None else repr(value).encode()


class AdcChannelState(msgspec.Struct):
    """Last outcome of one sampled input.

    Mutable (no ``frozen=True``): the poll loop updates the fields in place and
    the telemetry builder reads them without a copy.
    """

    value: float | None = None
    voltage: float | None = None
    ts: datetime | None = None
    error: str | None = None


class AdcDeviceState(msgspec.Struct):
    """Polling state of one device: health plus its per-channel snapshots."""

    state: str = STATE_STOPPED
    consecutive_failures: int = 0
    last_error: str | None = None
    last_ok: datetime | None = None
    channels: dict[int, AdcChannelState] = msgspec.field(default_factory=dict)


def _no_client() -> None:
    """Default client provider: a poller with no Valkey client publishes nothing."""
    return None


def backoff_ms(interval_ms: int, failures: int) -> int:
    """Delay before retrying a channel that failed ``failures`` times in a row.

    Exponential in the consecutive failures, capped at `MAX_BACKOFF_MS` so a
    dead converter is retried once a minute instead of never. The exponent is
    bounded because the counter keeps growing while the loop keeps polling.
    """
    return min(interval_ms * 2 ** min(failures, 16), MAX_BACKOFF_MS)


class AdcPoller:
    """Owns the poll tasks, the sampling plan, and the in-memory snapshots.

    ``configure`` supplies the service-wide failure threshold and stream
    retention; ``start``/``stop`` bracket a device generation. The worker drives
    the pair from its config apply hook so a rebuild never races a bus
    transaction.
    """

    def __init__(
        self,
        registry: AdcDeviceRegistry,
        logger: logging.Logger,
        *,
        service_name: str,
        instance_id: str,
        client_provider: Callable[[], Any | None] = _no_client,
        report_failure: Callable[[BaseException], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._logger = logger
        self._service_name = service_name
        self._instance_id = instance_id
        self._client_provider = client_provider
        self._report_failure = report_failure
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, AdcDeviceState] = {}
        self._failure_threshold = DEFAULT_FAILURE_THRESHOLD
        self._stream_maxlen = DEFAULT_STREAM_MAXLEN

    @property
    def tasks(self) -> Mapping[str, asyncio.Task[None]]:
        """One poll task per sampled device, keyed by device id."""
        return dict(self._tasks)

    @property
    def states(self) -> Mapping[str, AdcDeviceState]:
        """Device id -> polling state, including devices that are not polled."""
        return self._states

    def configure(self, settings: AdcServiceSettings) -> None:
        """Adopt the service-wide thresholds from the applied settings."""
        self._failure_threshold = settings.failure_threshold
        self._stream_maxlen = settings.stream_maxlen

    async def start(self) -> None:
        """Recompute the plan and start one task per sampled device.

        Called after the registry was rebuilt, so the plan reflects the new
        geometry and settings. A device with no sampleable channel gets no task
        but keeps its telemetry entry.
        """
        plan = poll_plan(self._registry, self._logger)
        self._sync_states(plan)
        loop = asyncio.get_running_loop()
        for device_id, channels in plan.items():
            self._tasks[device_id] = loop.create_task(
                self._poll_device(device_id, channels),
                name=f"adc-poll-{device_id}",
            )

    async def stop(self) -> None:
        """Cancel every poll task and wait for it to actually finish.

        Awaiting matters: a cancelled task may be inside a blocking SPI
        transaction, and the caller closes the device objects right after.
        """
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sleep(self, delay: float) -> None:
        """Sleep for ``delay``; a separate method so tests can drive the loop."""
        await asyncio.sleep(delay)

    def _now_ms(self) -> int:
        """Monotonic clock in whole milliseconds, the unit of the plan."""
        return int(self._clock() * 1000)

    async def _poll_device(self, device_id: str, channels: list[AdcChannelPlan]) -> None:
        """Sample one device forever, one channel per due time.

        The task ends only through cancellation: every failure is recorded and
        retried with a backoff, so a converter that comes back is picked up
        without a restart.
        """
        intervals = {plan.channel: plan.interval_ms for plan in channels}
        due = {plan.channel: self._now_ms() for plan in channels}
        while True:
            now = self._now_ms()
            for channel, due_ms in list(due.items()):
                if due_ms > now:
                    continue
                if await self._read_and_publish(device_id, channel):
                    due[channel] = self._now_ms() + intervals[channel]
                    continue
                failures = self._states[device_id].consecutive_failures
                due[channel] = self._now_ms() + backoff_ms(intervals[channel], failures)
            wait_s = (min(due.values()) - self._now_ms()) / 1000.0
            await self._sleep(max(MIN_SLEEP_S, wait_s))

    async def _read_and_publish(self, device_id: str, channel: int) -> bool:
        """Read one channel, update the snapshot, publish on success.

        Returns:
            `True` when the read succeeded. A publish failure is a transport
            problem, not a device one, so it does not count as a read failure.
        """
        now = datetime.now(timezone.utc)
        try:
            reading = await read_channel(self._registry, device_id, channel)
        except Exception as exc:
            self._record_failure(device_id, channel, exc, now)
            return False
        self._record_success(device_id, channel, reading, now)
        await self._publish_reading(device_id, channel, reading, now)
        return True

    def _record_success(self, device_id: str, channel: int, reading: ChannelReading, now: datetime) -> None:
        """Reset the device failure counter and store the fresh value."""
        state = self._states[device_id]
        channel_state = state.channels.setdefault(channel, AdcChannelState())
        channel_state.value = float(reading.code)
        channel_state.voltage = reading.voltage
        channel_state.ts = now
        channel_state.error = None
        state.state = STATE_OK
        state.consecutive_failures = 0
        state.last_error = None
        state.last_ok = now

    def _record_failure(self, device_id: str, channel: int, exc: Exception, now: datetime) -> None:
        """Count one failed read and weaken the reported state past the threshold."""
        state = self._states[device_id]
        state.consecutive_failures += 1
        state.last_error = str(exc)
        if state.consecutive_failures >= self._failure_threshold:
            state.state = STATE_ERROR
        channel_state = state.channels.setdefault(channel, AdcChannelState())
        # The value is unknown after a failed frame: clearing it makes the UI
        # render a gap with the error badge instead of a stale number.
        channel_state.value = None
        channel_state.voltage = None
        channel_state.ts = now
        channel_state.error = str(exc)
        self._logger.warning("Failed to read ADC %s[%d]: %s", device_id, channel, exc)

    async def _publish_reading(
        self,
        device_id: str,
        channel: int,
        reading: ChannelReading,
        now: datetime,
    ) -> None:
        """Append one entry to the channel's readings stream.

        The write is guarded on the client and a glide error is reported to the
        transport health, matching the telemetry loop's handling.
        """
        client = self._client_provider()
        if client is None:
            return
        key = readings_key(self._service_name, self._instance_id, device_id, channel)
        try:
            await client.xadd(
                key,
                [
                    (b"ts", now.isoformat().encode()),
                    (b"value", encode_value(float(reading.code))),
                    (b"voltage", encode_value(reading.voltage)),
                ],
                StreamAddOptions(trim=TrimByMaxLen(exact=False, threshold=self._stream_maxlen)),
            )
        except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
            self._logger.warning("Failed to publish reading for ADC %s[%d]: %s", device_id, channel, exc)
            if self._report_failure is not None:
                self._report_failure(exc)

    def _sync_states(self, plan: Mapping[str, list[AdcChannelPlan]]) -> None:
        """Keep the snapshot in step with the configured devices and the plan.

        A device that survives a config apply keeps its last value and health so
        an unrelated interval change does not blank the monitor; a removed
        device or a channel that is no longer sampled is dropped.
        """
        planned = {device_id: {channel.channel for channel in channels} for device_id, channels in plan.items()}
        settings = self._registry.settings
        device_ids = set(settings.devices) if settings is not None else set(planned)
        previous = self._states
        states: dict[str, AdcDeviceState] = {}
        for device_id in device_ids:
            state = previous.get(device_id) or AdcDeviceState()
            sampled = planned.get(device_id, set())
            state.channels = {index: channel for index, channel in state.channels.items() if index in sampled}
            states[device_id] = state
        self._states = states
