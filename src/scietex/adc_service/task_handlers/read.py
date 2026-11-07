"""``adc:read`` and ``adc:read_all`` data-plane tasks: sample converter inputs.

Both handlers read the live device at request time; nothing is cached, so a
result is the bus state at the moment the task ran. The reference voltage is
read once per call and the raw codes are scaled here, rather than through
`ADCChannel.voltage`, so every channel in one response uses the same reference.
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

from ..devices import AdcDeviceRegistry
from ._request import decode_request, error_result, ok_result

#: Task name for a single-channel read.
ADC_READ_TASK_NAME = "adc:read"

#: Task name for a full single-ended sweep.
ADC_READ_ALL_TASK_NAME = "adc:read_all"

#: 12-bit converter full scale; also the voltage divisor (DS21298E §4.2).
_FULL_SCALE = 1 << 12


def voltage(code: int, v_ref: float | None) -> float | None:
    """Scale a raw code by ``v_ref``; ``None`` when the scaling is unknown."""
    if v_ref is None:
        return None
    return code * v_ref / _FULL_SCALE


class AdcReadRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for ``adc:read``.

    Args:
        device_id: Device to read.
        channel: Single-ended input index, or differential pair index when
            ``differential`` is set.
        differential: Read a pseudo-differential pair instead of a
            single-ended input.
    """

    device_id: str
    channel: int = 0
    differential: bool = False


class AdcReadResponse(msgspec.Struct, frozen=True):
    """Result payload of an ``adc:read`` task.

    Args:
        device_id: Device the reading came from.
        channel: Index that was read.
        differential: Whether ``channel`` names a differential pair.
        code: Raw 12-bit code.
        voltage: Code scaled by the device reference, or ``None`` when the
            device has no reference voltage configured.
    """

    device_id: str
    channel: int
    differential: bool
    code: int
    voltage: float | None


class AdcReadAllRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for ``adc:read_all``.

    Args:
        device_id: Device to sweep.
    """

    device_id: str


class AdcReadAllResponse(msgspec.Struct, frozen=True):
    """Result payload of an ``adc:read_all`` task.

    Args:
        device_id: Device the sweep came from.
        codes: Raw codes in single-ended channel order.
        voltages: ``codes`` scaled by one reference-voltage read, or all
            ``None`` when the device has no reference voltage configured.
    """

    device_id: str
    codes: list[int]
    voltages: list[float | None]


class AdcReadHandler(TaskHandler):
    """Handle ``adc:read`` on the data lane."""

    control: ClassVar[bool] = False

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
        return [ADC_READ_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Read one input or differential pair and scale it in volts."""
        try:
            request = decode_request(task_data, AdcReadRequest)
            device = self._devices.get(request.device_id)
            if request.differential:
                channel = self._devices.differential_channel(request.device_id, request.channel)
            else:
                channel = self._devices.channel(request.device_id, request.channel)
            code = channel.value
            volts = voltage(code, device.v_ref)
        except Exception as exc:
            return error_result(exc)
        return ok_result(
            AdcReadResponse(
                device_id=request.device_id,
                channel=request.channel,
                differential=request.differential,
                code=code,
                voltage=volts,
            )
        )


class AdcReadAllHandler(TaskHandler):
    """Handle ``adc:read_all`` on the data lane."""

    control: ClassVar[bool] = False

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
        return [ADC_READ_ALL_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Sweep every single-ended input in one device-level read.

        `ADC.read_all()` is called exactly once: the sweep is the device's
        operation, and re-reading per channel would sample the bus twice for
        no reason.
        """
        try:
            request = decode_request(task_data, AdcReadAllRequest)
            device = self._devices.get(request.device_id)
            codes = device.read_all()
            v_ref = device.v_ref
            volts = [voltage(code, v_ref) for code in codes]
        except Exception as exc:
            return error_result(exc)
        return ok_result(
            AdcReadAllResponse(
                device_id=request.device_id,
                codes=list(codes),
                voltages=volts,
            )
        )
