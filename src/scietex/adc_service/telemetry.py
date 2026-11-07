"""Telemetry payloads the ADC worker publishes to Valkey.

The snapshot reports which configured devices are actually open, their
geometry, and the reference voltage in use, so the UI can tell an operator that
a converter disappeared without the worker going down. The key shape, push
interval, and TTL are the frozen contract: key
``scietex:{service}:{instance_id}:telemetry`` (the ``telemetry`` suffix never
matches the heartbeat discovery glob ``scietex:*:*:status``), pushed every 2 s
with a 6 s TTL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import msgspec

from .devices import DeviceUnavailable

if TYPE_CHECKING:
    from .adc_worker import AdcWorker

#: Push cadence and key TTL: 2 s interval, 6 s TTL so a single missed beat does
#: not expire the key.
TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6


class AdcDeviceTelemetry(msgspec.Struct, frozen=True):
    """Open state and geometry of one configured device."""

    device_id: str
    available: bool
    error: str | None = None
    v_ref: float | None = None
    channels: int = 0
    differential: int = 0
    poll: bool = False


class AdcTelemetry(msgspec.Struct, frozen=True):
    """Telemetry snapshot pushed by the ADC worker.

    ``configured`` is False only before the first settings resolution; the
    default struct has no devices but is a valid configuration.
    """

    service: str
    instance_id: str
    configured: bool
    poll_enabled: bool
    devices: list[AdcDeviceTelemetry]
    timestamp: datetime = msgspec.field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1


def build_telemetry(worker: AdcWorker) -> AdcTelemetry:
    """Snapshot the worker's settings and live device registry.

    A device that is configured but failed to open is reported with
    ``available=False`` and the reason recorded by the registry, so one missing
    converter does not hide the others. A worker before startup reports
    ``configured=False`` with an empty device list.
    """
    settings = worker.adc_settings
    registry = worker.devices
    errors = registry.errors
    devices: list[AdcDeviceTelemetry] = []
    for device_id in registry.ids():
        cfg = settings.devices.get(device_id) if settings is not None else None
        poll = cfg.poll if cfg is not None else False
        try:
            device = registry.get(device_id)
        except DeviceUnavailable:
            devices.append(
                AdcDeviceTelemetry(
                    device_id=device_id,
                    available=False,
                    error=errors.get(device_id),
                    v_ref=cfg.v_ref if cfg is not None else None,
                    poll=poll,
                )
            )
            continue
        devices.append(
            AdcDeviceTelemetry(
                device_id=device_id,
                available=True,
                v_ref=device.v_ref,
                channels=len(device.channels),
                differential=len(device.differential),
                poll=poll,
            )
        )
    return AdcTelemetry(
        service=worker.service_name,
        instance_id=worker.instance_id,
        configured=settings is not None,
        poll_enabled=settings.poll_enabled if settings is not None else True,
        devices=devices,
    )
