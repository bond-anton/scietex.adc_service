"""Device registry: validated settings to live HAL objects.

`build_adc` maps one `AdcDeviceSettings` entry onto a concrete
`scietex.hal.adc` class, rejecting anything the class would not accept instead
of letting a `TypeError` surface from deep inside the constructor.
`AdcDeviceRegistry` owns the live objects, rebuilds them when the effective
settings change, and records a per-device failure in `errors` rather than
raising, so one unplugged converter never keeps the rest of the service from
starting.

The registry is synchronous and not thread-safe: the worker drives it from one
task, and the HAL reads are blocking bus transactions by design.
"""

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from scietex.hal import adc as hal

from .config import AdcDeviceSettings, AdcServiceSettings


class DeviceConfigError(ValueError):
    """A device settings entry cannot be turned into a HAL object."""


class UnknownDeviceType(DeviceConfigError):
    """The configured ``type`` is not a supported part."""


class UnknownParameter(DeviceConfigError):
    """A configured field is not a constructor parameter of the device class."""


class InvalidParameter(DeviceConfigError):
    """A parameter or parameter combination the device class rejects."""


class DeviceNotFound(KeyError):
    """No device is configured under the requested id."""


class DeviceUnavailable(RuntimeError):
    """The id is configured but its device is not open.

    Retryable: the hardware or its configuration may recover without the id
    changing, unlike `DeviceNotFound`, which is a wrong request.

    ``reason`` keeps the recorded open error so a caller can log it without
    parsing the message.
    """

    def __init__(self, device_id: str, reason: str) -> None:
        super().__init__(f"Device {device_id!r} is unavailable: {reason}")
        self.device_id = device_id
        self.reason = reason


class ChannelNotFound(KeyError):
    """The device has no single-ended channel or differential pair at that index."""


class DeviceOpenError(RuntimeError):
    """The device class was resolvable but opening the hardware failed.

    ``cause`` keeps the original `ADCFrameError`/`OSError` so a caller can log
    or inspect the backend failure without parsing the message.
    """

    def __init__(self, device_id: str, cause: BaseException) -> None:
        super().__init__(f"Failed to open device {device_id!r}: {cause}")
        self.device_id = device_id
        self.cause = cause


@dataclass(frozen=True)
class PartSpec:
    """Constructor metadata for one supported ADC part.

    ``leading`` names parameters that the class takes positionally before the
    rest; ``forbidden`` names parameters it rejects outright; ``require_one_of``
    names an alternative group of which exactly one must be supplied.
    """

    cls: type
    leading: tuple[str, ...] = ()
    forbidden: frozenset[str] = frozenset()
    require_one_of: tuple[str, ...] = ()


#: Supported parts by the ``type`` string used in ``adc.yml``.
ADC_PARTS: dict[str, PartSpec] = {
    "MCP3204": PartSpec(hal.MCP3204),
    "MCP3208": PartSpec(hal.MCP3208),
}

#: Settings field -> constructor keyword. A field the settings leave ``None`` is
#: not forwarded, so the class default applies and no backend keyword is
#: mistaken for "explicitly disabled".
_TO_KWARG: dict[str, str] = {
    "port": "port",
    "device": "device",
    "clock_pin": "clock_pin",
    "mosi_pin": "mosi_pin",
    "miso_pin": "miso_pin",
    "select_pin": "select_pin",
    "v_ref": "v_ref",
    "label": "label",
    "log_level": "log_level",
}


def build_adc(device_id: str, cfg: AdcDeviceSettings) -> hal.ADC:
    """Build the HAL device described by one settings entry.

    Args:
        device_id: Id from the settings mapping, used only in error messages.
        cfg: The validated per-device settings.

    Returns:
        The constructed device, opened but with no bus transaction performed.

    Raises:
        UnknownDeviceType: The ``type`` is not in `ADC_PARTS`.
        UnknownParameter: A forwarded field is not a constructor parameter.
        InvalidParameter: A forwarded field, or the combination of fields, is
            rejected by the class signature or by the constructor itself.
        DeviceOpenError: The constructor failed at the bus level
            (`ADCFrameError` or `OSError`).
    """
    spec = ADC_PARTS.get(cfg.type)
    if spec is None:
        raise UnknownDeviceType(f"Unknown device type {cfg.type!r} for device {device_id!r}; supported: {sorted(ADC_PARTS)}")

    params: dict[str, object] = {}
    for field_name, kwarg in _TO_KWARG.items():
        value = getattr(cfg, field_name)
        if value is not None:
            params[kwarg] = value

    sig = inspect.signature(spec.cls)
    unknown = sorted(set(params) - set(sig.parameters))
    if unknown:
        raise UnknownParameter(f"Device {device_id!r} of type {cfg.type!r} does not accept {unknown}")

    forbidden = sorted(set(params) & spec.forbidden)
    if forbidden:
        raise InvalidParameter(f"Device {device_id!r} of type {cfg.type!r} forbids {forbidden}")

    if spec.require_one_of:
        supplied = [name for name in spec.require_one_of if name in params]
        if len(supplied) != 1:
            raise InvalidParameter(
                f"Device {device_id!r} of type {cfg.type!r} requires exactly one of "
                f"{list(spec.require_one_of)}, got {supplied}"
            )

    args = tuple(params.pop(name) for name in spec.leading if name in params)
    try:
        sig.bind(*args, **params)
    except TypeError as exc:
        raise InvalidParameter(f"Invalid parameters for device {device_id!r}: {exc}") from exc

    try:
        return spec.cls(*args, **params)
    except DeviceOpenError:
        raise
    except (hal.ADCFrameError, OSError) as exc:
        raise DeviceOpenError(device_id, exc) from exc
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"Invalid parameters for device {device_id!r}: {exc}") from exc


class AdcDeviceRegistry:
    """Live devices keyed by the string id from `AdcServiceSettings.devices`.

    A rebuild replaces the whole set: the old objects are closed first, then
    every configured entry is opened independently. Failures are collected in
    `errors` and never raised, so the service always starts with whatever
    hardware is present.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self._devices: dict[str, hal.ADC] = {}
        self._errors: dict[str, str] = {}
        self._settings: AdcServiceSettings | None = None
        self._generation = 0

    @property
    def errors(self) -> Mapping[str, str]:
        """Per-device failure messages from the last rebuild."""
        return dict(self._errors)

    @property
    def generation(self) -> int:
        """Number of rebuilds that replaced the live device set."""
        return self._generation

    @property
    def settings(self) -> AdcServiceSettings | None:
        """Settings the live set was built from, or ``None`` before the first rebuild."""
        return self._settings

    def rebuild(self, settings: AdcServiceSettings) -> None:
        """Close current devices, then open every configured one.

        A per-device failure is recorded in `errors` and never raised, so the
        service boots with whatever hardware is present. A registry-wide
        failure (malformed settings) propagates.

        Identical settings with at least one live device are a no-op, which
        keeps a repeated apply from churning open file descriptors. The guard
        deliberately requires a live device: a rebuild after every device
        failed retries the hardware instead of caching the failure.

        Args:
            settings: The merged settings to realize.
        """
        if self._settings == settings and self._devices:
            return
        old = self._devices
        self._devices = {}
        self._errors = {}
        for device_id, dev in old.items():
            try:
                dev.close()
            except Exception as exc:
                self._log.warning("Error closing %s: %s", device_id, exc)
        for device_id, cfg in settings.devices.items():
            try:
                self._devices[device_id] = build_adc(device_id, cfg)
            except (DeviceConfigError, DeviceOpenError) as exc:
                self._errors[device_id] = str(exc)
                self._log.error("Device %s is unavailable: %s", device_id, exc)
        self._settings = settings
        self._generation += 1

    def close(self) -> None:
        """Close every open device and drop the registry state.

        Idempotent: a second call finds nothing to close. A failing ``close()``
        is logged and does not keep the remaining devices open.
        """
        for device_id, dev in self._devices.items():
            try:
                dev.close()
            except Exception as exc:
                self._log.warning("Error closing %s: %s", device_id, exc)
        self._devices = {}
        self._errors = {}

    def ids(self) -> list[str]:
        """Sorted ids the operator should see, including devices that failed to open."""
        configured = self._settings.devices if self._settings is not None else {}
        return sorted(set(configured) | set(self._devices) | set(self._errors))

    def get(self, device_id: str) -> hal.ADC:
        """Return the open device under ``device_id``.

        Raises:
            DeviceUnavailable: The id is configured but the device did not open.
                The message carries the recorded open error.
            DeviceNotFound: The id is not configured at all. The message lists
                the known ids.
        """
        if device_id in self._devices:
            return self._devices[device_id]
        reason = self._errors.get(device_id)
        if reason is not None:
            raise DeviceUnavailable(device_id, reason)
        configured = self._settings.devices if self._settings is not None else {}
        if device_id in configured:
            # Configured, neither open nor recorded: only a closed registry
            # reaches this. Treat it as a state of the hardware, not a bad id.
            raise DeviceUnavailable(device_id, "configured but not open; rebuild to retry")
        raise DeviceNotFound(f"Device {device_id!r} is not configured; known ids: {self.ids()}")

    def channel(self, device_id: str, index: int) -> hal.ADCChannel:
        """Return single-ended channel ``index`` of ``device_id``.

        Bounds are checked against the live device first, so an out-of-range
        index raises `ChannelNotFound` instead of the HAL's bare `IndexError`.

        Raises:
            DeviceUnavailable: The device is configured but not open.
            DeviceNotFound: No device is configured under ``device_id``.
            ChannelNotFound: The device has no channel at ``index``.
        """
        device = self.get(device_id)
        if not 0 <= index < len(device.channels):
            raise ChannelNotFound(f"{device_id}[{index}]")
        return device.channel(index)

    def differential_channel(self, device_id: str, pair: int) -> hal.DifferentialChannel:
        """Return pseudo-differential pair ``pair`` of ``device_id``.

        Raises:
            DeviceUnavailable: The device is configured but not open.
            DeviceNotFound: No device is configured under ``device_id``.
            ChannelNotFound: The device has no differential pair at ``pair``.
        """
        device = self.get(device_id)
        if not 0 <= pair < len(device.differential):
            raise ChannelNotFound(f"{device_id}[{pair}]")
        return device.differential_channel(pair)

    def poll_targets(self) -> list[tuple[str, hal.ADC]]:
        """Open devices whose settings entry has ``poll=True``, in settings order."""
        if self._settings is None:
            return []
        return [
            (device_id, self._devices[device_id])
            for device_id, cfg in self._settings.devices.items()
            if cfg.poll and device_id in self._devices
        ]
