"""Telemetry payloads the ADC worker publishes to Valkey.

The snapshot reports the polling state of every configured device and the last
outcome of each sampled input, so the UI can tell an operator that a converter
disappeared, or that a bus fault appeared and cleared, without watching the
readings streams. The payload is a plain mapping because both sides key devices
and channels by ids from the configuration, not by struct fields.

The key shape, push interval, and TTL are the frozen contract: key
``scietex:{service}:{instance_id}:telemetry`` (the ``telemetry`` suffix never
matches the heartbeat discovery glob ``scietex:*:*:status``), pushed every 2 s
with a 6 s TTL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .poller import STATE_ERROR, STATE_OK, STATE_STOPPED

if TYPE_CHECKING:
    from .adc_worker import AdcWorker
    from .poller import AdcChannelState, AdcDeviceState

#: Push cadence and key TTL: 2 s interval, 6 s TTL so a single missed beat does
#: not expire the key.
TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6

#: Payload revision: 2 added the service/device states and the per-channel
#: snapshots on top of the open-device list of revision 1.
SCHEMA_VERSION: int = 2


def _isoformat(value: datetime | None) -> str | None:
    """Render a timestamp as ISO-8601, keeping ``None`` distinguishable from ""."""
    return value.isoformat() if value is not None else None


def _channel_payload(state: AdcChannelState) -> dict[str, Any]:
    """Encode one channel's last outcome for the payload.

    The value is the raw code as a float; ``voltage`` stays ``None`` when the
    device has no reference, so the UI renders a dash instead of a fake zero.
    """
    return {
        "value": state.value,
        "voltage": state.voltage,
        "ts": _isoformat(state.ts),
        "error": state.error,
    }


def _device_payload(
    cfg: Any | None,
    state: AdcDeviceState | None,
    open_error: str | None,
) -> dict[str, Any]:
    """Encode one device's polling state and channel snapshots.

    A device that failed to open is an error regardless of the poll snapshot it
    never got: the recorded open error is the reason the operator needs. A
    configured device the poller does not sample reports ``stopped``.
    """
    if open_error is not None:
        device_state = STATE_ERROR
        last_error = open_error
    elif state is not None:
        device_state = state.state
        last_error = state.last_error
    else:
        device_state = STATE_STOPPED
        last_error = None
    return {
        "state": device_state,
        "type": cfg.type if cfg is not None else None,
        "last_error": last_error,
        "last_ok": _isoformat(state.last_ok if state is not None else None),
        "channels": {
            str(index): _channel_payload(channel) for index, channel in sorted((state.channels if state else {}).items())
        },
    }


def _service_state(devices: dict[str, dict[str, Any]]) -> str:
    """Aggregate the device states: any error wins, then any live device."""
    states = {entry["state"] for entry in devices.values()}
    if STATE_ERROR in states:
        return STATE_ERROR
    if STATE_OK in states:
        return STATE_OK
    return STATE_STOPPED


def build_telemetry(worker: AdcWorker) -> dict[str, Any]:
    """Snapshot the worker's settings, registry, and poll snapshots.

    ``configured`` is False only before the first settings resolution; the
    default struct has no devices but is a valid configuration. A device that
    is configured but failed to open is reported as ``error`` with the recorded
    reason, so one missing converter does not hide the others.
    """
    settings = worker.adc_settings
    registry = worker.devices
    states = worker.poller.states
    errors = registry.errors
    devices: dict[str, dict[str, Any]] = {}
    for device_id in registry.ids():
        cfg = settings.devices.get(device_id) if settings is not None else None
        devices[device_id] = _device_payload(cfg, states.get(device_id), errors.get(device_id))
    return {
        "service": worker.service_name,
        "instance_id": worker.instance_id,
        "configured": settings is not None,
        "poll_enabled": settings.poll_enabled if settings is not None else True,
        "state": _service_state(devices),
        "devices": devices,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
    }
