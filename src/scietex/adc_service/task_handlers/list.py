"""``adc:list`` control-plane task: report the devices this worker can read.

The API cannot know which converters are configured or actually open on the
worker host, so the operator's device picker asks the worker. The answer
includes devices that failed to open with the recorded reason: a missing
converter is a state the UI must show, not a reason to fail the whole listing.

This is a control-plane task because the answer describes the worker's own
hardware, not a measurement.
"""

from typing import ClassVar

import msgspec

from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

from ..devices import AdcDeviceRegistry, DeviceNotFound, DeviceUnavailable
from ._request import decode_request, error_result, ok_result

#: Task name for the device/channel enumeration control task.
ADC_LIST_TASK_NAME = "adc:list"


class AdcListRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for ``adc:list``.

    Args:
        device_id: Id to describe. ``None`` or an empty payload lists every
            configured device.
    """

    device_id: str | None = None


class AdcDeviceInfo(msgspec.Struct, frozen=True):
    """One configured device and its open state.

    Args:
        device_id: Id from the ``devices`` mapping in ``adc.yml``.
        available: Whether the device is open and readable.
        error: Why the device is unavailable; ``None`` when it is available.
        v_ref: Reference voltage the device reports, or ``None`` when the
            scaling is unknown.
        channels: Number of single-ended inputs.
        differential: Number of differential pairs.
        poll: Whether this device opts into polling. Effective polling also
            requires `AdcListResponse.poll_enabled`.
    """

    device_id: str
    available: bool
    error: str | None = None
    v_ref: float | None = None
    channels: int = 0
    differential: int = 0
    poll: bool = False


class AdcListResponse(msgspec.Struct, frozen=True):
    """Result payload of an ``adc:list`` task.

    Args:
        devices: Requested devices, sorted by id; empty is a normal result.
        poll_enabled: Service-wide polling switch.
    """

    devices: list[AdcDeviceInfo] = msgspec.field(default_factory=list)
    poll_enabled: bool = True


class AdcListHandler(TaskHandler):
    """Handle ``adc:list`` on the control lane."""

    control: ClassVar[bool] = True

    def __init__(self, name: str, context: TaskHandlerContext, *, devices: AdcDeviceRegistry) -> None:
        """Initialize the handler.

        Args:
            name: Human-readable name for this handler instance.
            context: Narrow context provided by the owning processor.
            devices: Shared registry injected by the worker, so the handler
                sees live rebuilds instead of a startup snapshot.
        """
        super().__init__(name, context)
        self._devices = devices

    @property
    def supported_tasks(self) -> list[str]:
        """Task types this handler serves."""
        return [ADC_LIST_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Describe one configured device, or every device when none is named."""
        try:
            request = decode_request(task_data, AdcListRequest)
            if request.device_id is None:
                infos = [self._describe(device_id) for device_id in self._devices.ids()]
            else:
                infos = [self._describe(request.device_id)]
        except Exception as exc:
            return error_result(exc)
        settings = self._devices.settings
        return ok_result(
            AdcListResponse(
                devices=infos,
                poll_enabled=settings.poll_enabled if settings is not None else True,
            )
        )

    def _describe(self, device_id: str) -> AdcDeviceInfo:
        """Build one entry, reporting an open failure instead of raising it.

        An id that is not configured at all is still a terminal request error;
        an id that is configured but failed to open is a successful listing
        entry with ``available=False``.
        """
        if device_id not in self._devices.ids():
            raise DeviceNotFound(f"Device {device_id!r} is not configured; known ids: {self._devices.ids()}")
        settings = self._devices.settings
        cfg = settings.devices.get(device_id) if settings is not None else None
        poll = cfg.poll if cfg is not None else False
        try:
            device = self._devices.get(device_id)
        except DeviceUnavailable:
            return AdcDeviceInfo(
                device_id=device_id,
                available=False,
                error=self._devices.errors.get(device_id),
                poll=poll,
            )
        return AdcDeviceInfo(
            device_id=device_id,
            available=True,
            v_ref=device.v_ref,
            channels=len(device.channels),
            differential=len(device.differential),
            poll=poll,
        )
