"""Tests for the ``adc:list``, ``adc:read``, and ``adc:read_all`` handlers."""

import logging
from uuid import uuid4

import msgspec
import pytest

from scietex.adc_service.config import AdcDeviceSettings, AdcServiceSettings
from scietex.adc_service.devices import (
    AdcDeviceRegistry,
    DeviceConfigError,
    DeviceNotFound,
    DeviceOpenError,
    DeviceUnavailable,
)
from scietex.adc_service.task_handlers import (
    ADC_LIST_TASK_NAME,
    ADC_READ_ALL_TASK_NAME,
    ADC_READ_TASK_NAME,
    AdcListHandler,
    AdcListResponse,
    AdcReadAllHandler,
    AdcReadAllResponse,
    AdcReadHandler,
    AdcReadResponse,
)
from scietex.adc_service.task_handlers._request import error_result
from scietex.hal.adc import ADCFrameError
from scietex.hal.spi import TransportClosed
from scietex.service.task_handler import TaskCapabilities, TaskData, TaskHandlerContext


class FailingChannel:
    """Channel double whose read raises the configured exception."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    @property
    def value(self) -> int:
        raise self.exc


def _context() -> TaskHandlerContext:
    return TaskHandlerContext(service_name="test", instance_id="inst", logger=logging.getLogger("test"))


def _capabilities() -> TaskCapabilities:
    async def _noop(_task_id, _value) -> None:
        return None

    return TaskCapabilities(task_id=uuid4(), _write_progress=_noop)


def _handler(handler_class, registry: AdcDeviceRegistry):
    return handler_class("test", _context(), devices=registry)


def _task(task: str, payload: dict | None = None) -> TaskData:
    encoded = b"" if payload is None else msgspec.json.encode(payload)
    return TaskData(task_id=str(uuid4()), task=task, payload=encoded)


def _registry(settings: AdcServiceSettings) -> AdcDeviceRegistry:
    registry = AdcDeviceRegistry(logging.getLogger("test.adc_service.handlers"))
    registry.rebuild(settings)
    return registry


def _decode(result, response_type):
    assert result.status == "success"
    return msgspec.json.decode(result.payload, type=response_type)


def _device(registry: AdcDeviceRegistry, device_id: str = "adc0"):
    """Return the open fake device so a test can script its codes."""
    return registry.get(device_id)


def test_error_result_maps_the_taxonomy() -> None:
    """Every exception family maps to its wire code and retry policy."""
    cases = [
        (DeviceNotFound("missing"), "DEVICE_NOT_FOUND", False),
        (DeviceUnavailable("adc0", "SPI bus is gone"), "DEVICE_UNAVAILABLE", True),
        (DeviceOpenError("adc0", OSError("gone")), "DEVICE_UNAVAILABLE", True),
        (ADCFrameError("bad frame"), "ADC_FRAME_ERROR", True),
        (TransportClosed("closed"), "TRANSPORT_ERROR", True),
        (DeviceConfigError("bad settings"), "INVALID_REQUEST", False),
        (msgspec.ValidationError("bad type"), "INVALID_REQUEST", False),
        (OSError("bus error"), "DEVICE_IO_ERROR", True),
    ]

    for exc, error_code, retryable in cases:
        result = error_result(exc)
        assert result.status == "error"
        assert result.error_code == error_code
        assert result.retryable is retryable


def test_list_declares_control_lane_and_task_name() -> None:
    handler = _handler(AdcListHandler, _registry(AdcServiceSettings()))
    assert handler.supported_tasks == [ADC_LIST_TASK_NAME]
    assert AdcListHandler.control is True


@pytest.mark.asyncio
async def test_list_reports_devices_sorted_with_geometry(fake_parts) -> None:
    """Every configured device is reported, sorted by id, with its live geometry."""
    settings = AdcServiceSettings(
        devices={
            "b": AdcDeviceSettings(type="FAKE", poll=False),
            "a": AdcDeviceSettings(type="FAKE", v_ref=3.3),
        }
    )
    result = await _handler(AdcListHandler, _registry(settings)).handle(
        _task(ADC_LIST_TASK_NAME), capabilities=_capabilities()
    )

    response = _decode(result, AdcListResponse)
    assert [device.device_id for device in response.devices] == ["a", "b"]
    first, second = response.devices
    assert first.available is True
    assert first.v_ref == 3.3
    assert first.channels == 8
    assert first.differential == 4
    assert first.poll is True
    assert second.available is True
    assert second.v_ref is None
    assert second.poll is False
    assert response.poll_enabled is True


@pytest.mark.asyncio
async def test_list_filters_by_device_id(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"a": AdcDeviceSettings(type="FAKE"), "b": AdcDeviceSettings(type="FAKE")})
    result = await _handler(AdcListHandler, _registry(settings)).handle(
        _task(ADC_LIST_TASK_NAME, {"device_id": "b"}), capabilities=_capabilities()
    )

    response = _decode(result, AdcListResponse)
    assert [device.device_id for device in response.devices] == ["b"]


@pytest.mark.asyncio
async def test_list_reports_open_failure(fake_parts) -> None:
    """A configured device that failed to open is listed, not raised."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="BROKEN")})
    result = await _handler(AdcListHandler, _registry(settings)).handle(
        _task(ADC_LIST_TASK_NAME), capabilities=_capabilities()
    )

    [device] = _decode(result, AdcListResponse).devices
    assert device.available is False
    assert device.error is not None
    assert "SPI bus is gone" in device.error


@pytest.mark.asyncio
async def test_list_unknown_device_is_terminal(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    result = await _handler(AdcListHandler, _registry(settings)).handle(
        _task(ADC_LIST_TASK_NAME, {"device_id": "missing"}), capabilities=_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "DEVICE_NOT_FOUND"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_list_malformed_payload_is_invalid_request() -> None:
    result = await _handler(AdcListHandler, _registry(AdcServiceSettings())).handle(
        TaskData(task_id=str(uuid4()), task=ADC_LIST_TASK_NAME, payload=b"not-json"),
        capabilities=_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_REQUEST"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_list_unknown_field_is_invalid_request() -> None:
    result = await _handler(AdcListHandler, _registry(AdcServiceSettings())).handle(
        _task(ADC_LIST_TASK_NAME, {"bogus": 1}), capabilities=_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_REQUEST"


def test_read_declares_data_lane_and_task_name() -> None:
    handler = _handler(AdcReadHandler, _registry(AdcServiceSettings()))
    assert handler.supported_tasks == [ADC_READ_TASK_NAME]
    assert AdcReadHandler.control is False


@pytest.mark.asyncio
async def test_read_returns_code_and_voltage(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE", v_ref=3.3)})
    registry = _registry(settings)
    _device(registry).channels[2].code = 2048

    result = await _handler(AdcReadHandler, registry).handle(
        _task(ADC_READ_TASK_NAME, {"device_id": "adc0", "channel": 2}), capabilities=_capabilities()
    )

    response = _decode(result, AdcReadResponse)
    assert response.device_id == "adc0"
    assert response.channel == 2
    assert response.differential is False
    assert response.code == 2048
    assert response.voltage == pytest.approx(1.65)


@pytest.mark.asyncio
async def test_read_without_reference_reports_no_voltage(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    _device(registry).channels[0].code = 100

    result = await _handler(AdcReadHandler, registry).handle(
        _task(ADC_READ_TASK_NAME, {"device_id": "adc0"}), capabilities=_capabilities()
    )

    response = _decode(result, AdcReadResponse)
    assert response.code == 100
    assert response.voltage is None


@pytest.mark.asyncio
async def test_read_differential_pair(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE", v_ref=3.3)})
    registry = _registry(settings)
    _device(registry).differential[1].code = 300

    result = await _handler(AdcReadHandler, registry).handle(
        _task(ADC_READ_TASK_NAME, {"device_id": "adc0", "channel": 1, "differential": True}),
        capabilities=_capabilities(),
    )

    response = _decode(result, AdcReadResponse)
    assert response.differential is True
    assert response.code == 300
    assert response.voltage == pytest.approx(300 * 3.3 / 4096)


@pytest.mark.asyncio
async def test_read_device_errors_split_not_found_from_unavailable(fake_parts) -> None:
    """An unconfigured id is terminal; a configured device that did not open is retryable."""
    settings = AdcServiceSettings(devices={"broken": AdcDeviceSettings(type="BROKEN")})
    handler = _handler(AdcReadHandler, _registry(settings))

    unknown = await handler.handle(_task(ADC_READ_TASK_NAME, {"device_id": "missing"}), capabilities=_capabilities())
    unavailable = await handler.handle(_task(ADC_READ_TASK_NAME, {"device_id": "broken"}), capabilities=_capabilities())

    assert (unknown.status, unknown.error_code, unknown.retryable) == ("error", "DEVICE_NOT_FOUND", False)
    assert (unavailable.status, unavailable.error_code, unavailable.retryable) == ("error", "DEVICE_UNAVAILABLE", True)
    assert "SPI bus is gone" in str(unavailable.error)


@pytest.mark.asyncio
async def test_read_out_of_range_channel_is_terminal(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    result = await _handler(AdcReadHandler, _registry(settings)).handle(
        _task(ADC_READ_TASK_NAME, {"device_id": "adc0", "channel": 99}), capabilities=_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "CHANNEL_NOT_FOUND"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_read_out_of_range_pair_is_terminal(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    result = await _handler(AdcReadHandler, _registry(settings)).handle(
        _task(ADC_READ_TASK_NAME, {"device_id": "adc0", "channel": 99, "differential": True}),
        capabilities=_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "CHANNEL_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "error_code"),
    [
        (ADCFrameError("bad frame"), "ADC_FRAME_ERROR"),
        (TransportClosed("closed"), "TRANSPORT_ERROR"),
        (OSError("bus error"), "DEVICE_IO_ERROR"),
    ],
)
async def test_read_bus_failures_are_retryable(fake_parts, exc, error_code) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    _device(registry).channels = (FailingChannel(exc),)

    result = await _handler(AdcReadHandler, registry).handle(
        _task(ADC_READ_TASK_NAME, {"device_id": "adc0"}), capabilities=_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == error_code
    assert result.retryable is True


@pytest.mark.asyncio
async def test_read_missing_required_field_is_invalid_request() -> None:
    result = await _handler(AdcReadHandler, _registry(AdcServiceSettings())).handle(
        _task(ADC_READ_TASK_NAME, {"channel": 0}), capabilities=_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_REQUEST"


def test_read_all_declares_data_lane_and_task_name() -> None:
    handler = _handler(AdcReadAllHandler, _registry(AdcServiceSettings()))
    assert handler.supported_tasks == [ADC_READ_ALL_TASK_NAME]
    assert AdcReadAllHandler.control is False


@pytest.mark.asyncio
async def test_read_all_calls_device_read_all_once(fake_parts) -> None:
    """The sweep is one device-level read, not one transaction per channel."""
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE", v_ref=3.3)})
    registry = _registry(settings)
    device = _device(registry)
    for index, channel in enumerate(device.channels):
        channel.code = index + 1
    calls: list[int] = []
    original = device.read_all

    def counting_read_all() -> tuple[int, ...]:
        calls.append(1)
        return original()

    device.read_all = counting_read_all

    result = await _handler(AdcReadAllHandler, registry).handle(
        _task(ADC_READ_ALL_TASK_NAME, {"device_id": "adc0"}), capabilities=_capabilities()
    )

    response = _decode(result, AdcReadAllResponse)
    assert calls == [1]
    assert response.codes == list(range(1, 9))
    assert response.voltages == pytest.approx([code * 3.3 / 4096 for code in range(1, 9)])


@pytest.mark.asyncio
async def test_read_all_without_reference_reports_no_voltages(fake_parts) -> None:
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})
    result = await _handler(AdcReadAllHandler, _registry(settings)).handle(
        _task(ADC_READ_ALL_TASK_NAME, {"device_id": "adc0"}), capabilities=_capabilities()
    )

    response = _decode(result, AdcReadAllResponse)
    assert response.codes == [0] * 8
    assert response.voltages == [None] * 8


@pytest.mark.asyncio
async def test_read_all_unknown_device_is_terminal() -> None:
    result = await _handler(AdcReadAllHandler, _registry(AdcServiceSettings())).handle(
        _task(ADC_READ_ALL_TASK_NAME, {"device_id": "missing"}), capabilities=_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "DEVICE_NOT_FOUND"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_read_all_malformed_payload_is_invalid_request() -> None:
    result = await _handler(AdcReadAllHandler, _registry(AdcServiceSettings())).handle(
        TaskData(task_id=str(uuid4()), task=ADC_READ_ALL_TASK_NAME, payload=b"\xff"),
        capabilities=_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_REQUEST"
    assert result.retryable is False
