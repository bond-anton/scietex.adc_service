"""Device registry: validated settings to live HAL objects.

`build_adc` maps one `AdcDeviceSettings` entry onto a concrete
`scietex.hal.adc` class, rejecting anything the class would not accept instead
of letting a `TypeError` surface from deep inside the constructor.
`AdcDeviceRegistry` owns the live objects, rebuilds them when the effective
settings change, and records a per-device failure in `errors` rather than
raising, so one unplugged converter never keeps the rest of the service from
starting.

`poll_plan` turns the settings plus the live geometry into the per-device,
per-channel sampling plan the poll loop consumes. `read_channel` and
`read_all_channels` are the only paths that touch a converter: they take the
per-device lock so a polled reading and an on-demand task never interleave two
SPI transactions on one chip select. The registry itself stays synchronous and
not thread-safe; the HAL reads are blocking bus transactions by design.
"""

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from scietex.hal import adc as hal

from .config import AdcDeviceSettings, AdcServiceSettings

#: 12-bit converter full scale; also the voltage divisor (DS21298E §4.2).
FULL_SCALE: int = 1 << 12

#: Hard bounds on a sampling period. Below 100 ms one converter's blocking
#: transaction would starve the event loop; above an hour the cadence is
#: indistinguishable from polling being off.
MIN_INTERVAL_MS: int = 100
MAX_INTERVAL_MS: int = 3_600_000

_LOG = logging.getLogger(__name__)


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
        # One bus lock per device id, shared by the poll loop and the task
        # handlers. An SPI transaction is not re-entrant on a chip select, so
        # two readers of the same converter must queue, not interleave.
        self._locks: dict[str, asyncio.Lock] = {}

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

    def opened(self, device_id: str) -> hal.ADC | None:
        """Return the open device under ``device_id``, or ``None`` when it is not open.

        Unlike `get`, this never raises: telemetry needs the live geometry of a
        device that opened and must skip one that did not.
        """
        return self._devices.get(device_id)

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

    def read_lock(self, device_id: str) -> asyncio.Lock:
        """Return the bus lock serializing reads of ``device_id``.

        The lock is created on first use and kept across rebuilds: a rebuild
        runs only after the poll tasks are stopped, so no reader is holding it
        when the device object is replaced.
        """
        lock = self._locks.get(device_id)
        if lock is None:
            lock = self._locks[device_id] = asyncio.Lock()
        return lock


def voltage(code: int, v_ref: float | None) -> float | None:
    """Scale a raw code by ``v_ref``; ``None`` when the scaling is unknown."""
    if v_ref is None:
        return None
    return code * v_ref / FULL_SCALE


@dataclass(frozen=True)
class ChannelReading:
    """One sampled input: the raw code and the code scaled by the reference."""

    device_id: str
    channel: int
    differential: bool
    code: int
    voltage: float | None


async def read_channel(
    registry: AdcDeviceRegistry,
    device_id: str,
    index: int,
    *,
    differential: bool = False,
) -> ChannelReading:
    """Read one input under the device's bus lock.

    This is the single read path shared by the poll loop and ``adc:read``, so
    a polled and an on-demand value come from the same accessor and the same
    scaling. The device is resolved before the lock is taken, so an unknown or
    unavailable id never waits behind a slow bus transaction.

    Raises:
        DeviceNotFound: No device is configured under ``device_id``.
        DeviceUnavailable: The device is configured but not open.
        ChannelNotFound: The device has no input at ``index``.
    """
    registry.get(device_id)
    async with registry.read_lock(device_id):
        device = registry.get(device_id)
        if differential:
            channel = registry.differential_channel(device_id, index)
        else:
            channel = registry.channel(device_id, index)
        code = channel.value
        volts = voltage(code, device.v_ref)
    return ChannelReading(
        device_id=device_id,
        channel=index,
        differential=differential,
        code=code,
        voltage=volts,
    )


async def read_all_channels(
    registry: AdcDeviceRegistry,
    device_id: str,
) -> tuple[list[int], list[float | None]]:
    """Sweep every single-ended input of ``device_id`` under the bus lock.

    `ADC.read_all()` is one device-level operation, so the sweep stays a single
    transaction; only the voltage scaling is repeated per channel, with one
    reference read so every value in the answer agrees.
    """
    registry.get(device_id)
    async with registry.read_lock(device_id):
        device = registry.get(device_id)
        codes = device.read_all()
        v_ref = device.v_ref
        volts = [voltage(code, v_ref) for code in codes]
    return list(codes), volts


@dataclass(frozen=True)
class AdcChannelPlan:
    """One channel the poll loop samples: index, period, and display label."""

    channel: int
    interval_ms: int
    label: str | None = None


def poll_plan(
    registry: AdcDeviceRegistry,
    logger: logging.Logger | None = None,
) -> dict[str, list[AdcChannelPlan]]:
    """Expand the settings and the live geometry into the sampling plan.

    The device subset comes from `AdcDeviceRegistry.poll_targets`, so a device
    with ``poll=False``, an unopened device, and (service-wide)
    ``poll_enabled=False`` never reach the plan. Per device:

    * an empty ``channels`` list yields no plan at all — the device is not
      sampled until explicit rows are configured;
    * an explicit channel row sets that input's period, and an ``enabled=False``
      row is dropped;
    * a row naming an input the part does not have is dropped with a warning,
      because it could only fail on every tick;
    * every period is clamped to ``[MIN_INTERVAL_MS, MAX_INTERVAL_MS]`` with a
      warning, so a misconfigured value degrades instead of starving the loop.

    Args:
        registry: The registry holding the live devices and the settings they
            were built from.
        logger: Destination for clamp and drop warnings; the module logger
            when omitted.

    Returns:
        Device id -> channels to sample. A device with no sampleable input is
        absent, and so is every device when polling is disabled.
    """
    settings = registry.settings
    if settings is None or not settings.poll_enabled:
        return {}
    log = logger if logger is not None else _LOG
    plan: dict[str, list[AdcChannelPlan]] = {}
    for device_id, device in registry.poll_targets():
        cfg = settings.devices[device_id]
        channels = _device_plan(device_id, device, cfg, log)
        if channels:
            plan[device_id] = channels
    return plan


def _device_plan(
    device_id: str,
    device: hal.ADC,
    cfg: AdcDeviceSettings,
    logger: logging.Logger,
) -> list[AdcChannelPlan]:
    """Per-channel plan for one open device, with clamping and drops applied.

    Only explicit rows produce a plan: an empty ``channels`` list returns an
    empty plan, so an unconfigured device is not sampled at all.
    """
    limit = len(device.channels)
    if not cfg.channels:
        return []

    plans: list[AdcChannelPlan] = []
    seen: set[int] = set()
    for entry in cfg.channels:
        if not entry.enabled:
            continue
        if entry.channel in seen:
            logger.warning("Device %s: duplicate channel %d row ignored", device_id, entry.channel)
            continue
        if not 0 <= entry.channel < limit:
            logger.warning(
                "Device %s: channel %d is outside the part's %d single-ended inputs",
                device_id,
                entry.channel,
                limit,
            )
            continue
        seen.add(entry.channel)
        plans.append(
            AdcChannelPlan(
                channel=entry.channel,
                interval_ms=_clamp_interval(device_id, entry.poll_interval_ms, logger),
                label=entry.label,
            )
        )
    return plans


def _clamp_interval(device_id: str, interval_ms: int, logger: logging.Logger) -> int:
    """Clamp one sampling period into the supported range, warning on a change."""
    clamped = min(max(interval_ms, MIN_INTERVAL_MS), MAX_INTERVAL_MS)
    if clamped != interval_ms:
        logger.warning(
            "Device %s: poll_interval_ms=%d is outside [%d, %d]; using %d",
            device_id,
            interval_ms,
            MIN_INTERVAL_MS,
            MAX_INTERVAL_MS,
            clamped,
        )
    return clamped
