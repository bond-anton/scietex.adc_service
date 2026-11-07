"""ADC worker: a Valkey-backed service that owns a set of SPI converters.

`AdcWorker` extends `ValkeyWorker` with a live `AdcDeviceRegistry`. Settings
resolve through the framework's four-layer merge: constructor default (L0) <
``adc.yml`` bootstrap patch (L1) < framework ``config.yml`` snapshot (L2) <
remote ``adc`` section (L3). Every apply rebuilds the registry from the merged
struct; a device that cannot be opened is recorded and the rest keep working.
A ``Telemetry`` manager publishes the open-device snapshot every 2 s.
"""

import asyncio
import logging
from typing import cast

import msgspec
from glide import (
    ConnectionError as GlideConnectionError,
)
from glide import (
    ExpirySet,
    ExpiryType,
    RequestError,
)
from glide import (
    TimeoutError as GlideTimeoutError,
)

from scietex.service import Manager, ValkeyWorker, ValkeyWorkerConfig

from .config import (
    ADC_SECTION,
    ADC_SETTINGS_DEFAULTS,
    AdcServiceSettings,
    read_adc_config,
)
from .devices import AdcDeviceRegistry
from .task_handlers import AdcListHandler, AdcReadAllHandler, AdcReadHandler
from .telemetry import TELEMETRY_INTERVAL, TELEMETRY_TTL, build_telemetry


class AdcWorker(ValkeyWorker):
    """A `ValkeyWorker` that reads configured SPI ADC devices on demand."""

    def __init__(self, config: ValkeyWorkerConfig | None = None, *, client_factory=None, theme=None) -> None:
        super().__init__(config, client_factory=client_factory, theme=theme)
        self._settings: AdcServiceSettings | None = None
        self._devices = AdcDeviceRegistry(self.logger)
        # The apply hook runs during ``initialize`` (L2/L3 delivery) and at
        # runtime. False until ``initialize`` finishes so the hook does not
        # rebuild the registry twice on startup; ``initialize`` rebuilds once
        # from the final merged settings.
        self._startup_complete: bool = False
        # Telemetry encoding is owned here because the framework's encoder is
        # private; the key is resolved once at construction.
        self._telemetry_encoder = msgspec.msgpack.Encoder()
        self._telemetry_key = f"scietex:{self.service_name}:{self.instance_id}:telemetry"
        self.register_config_settings(
            ADC_SECTION,
            AdcServiceSettings,
            apply=self._apply_adc_settings,
            defaults=ADC_SETTINGS_DEFAULTS,
            bootstrap=lambda: read_adc_config(self.conf_dir),
        )
        # All three handlers share the registry, so a config apply is visible
        # to an in-flight handler without re-registration.
        self.add_task_handler(AdcListHandler, devices=self._devices)
        self.add_task_handler(AdcReadHandler, devices=self._devices)
        self.add_task_handler(AdcReadAllHandler, devices=self._devices)

    @property
    def adc_settings(self) -> AdcServiceSettings | None:
        """The effective ADC settings, or `None` before startup."""
        return self._settings

    @property
    def devices(self) -> AdcDeviceRegistry:
        """The live device registry shared with the task handlers."""
        return self._devices

    async def initialize(self) -> bool:
        """Initialize the framework, then open every configured device.

        `super().initialize()` seeds the L0+L1 layers (running the ``adc.yml``
        bootstrap provider) and applies the L2/L3 layers, so the merged settings
        are read back via `current_config_settings` afterwards. A bootstrap
        failure is swallowed by the framework's seeder and surfaces as an
        unresolved section, which is the ``None`` guard below. Per-device open
        failures do not fail startup: the registry records them and the worker
        serves the hardware that is present.

        Returns:
            `True` if the settings resolved and the registry rebuilt, `False`
            on a framework, resolution, or registry-wide failure.
        """
        self._startup_complete = False
        if not await super().initialize():
            return False

        resolved = self.current_config_settings(ADC_SECTION)
        if resolved is None:
            self.logger.error("ADC settings were not resolved before device startup")
            return False
        settings = cast(AdcServiceSettings, resolved)
        try:
            self._devices.rebuild(settings)
        except Exception as exc:
            self.logger.error("Failed to build the ADC device registry: %s", exc)
            return False
        self._settings = settings
        self._startup_complete = True
        return True

    async def cleanup(self) -> None:
        """Close every open device, then the framework resources."""
        self._startup_complete = False
        self._devices.close()
        await super().cleanup()

    def _apply_adc_settings(self, settings: AdcServiceSettings) -> None:
        """Store merged settings and rebuild the live devices.

        The hook is synchronous (the framework invokes it inline, before the
        core swap), and `AdcDeviceRegistry.rebuild` is synchronous by design.
        During startup the hook only stores: `initialize` rebuilds once from
        the final merged settings to avoid opening every device twice.

        A registry-wide failure propagates, which the framework reports as a
        failed apply; per-device failures are recorded in the registry and
        leave the apply successful.
        """
        if not self._startup_complete:
            self._settings = settings
            return
        self._devices.rebuild(settings)
        self._settings = settings

    @Manager(name="Telemetry")
    async def telemetry_loop(self) -> None:
        """Publish device telemetry to Valkey once per iteration.

        The manager runtime repeats this body forever with auto-restart on
        error. The write is guarded on `self.client` and runs even before the
        first configuration, so the UI observes ``configured=false``. Glide
        errors are logged at WARNING and reported to the transport health,
        which drives the reconnect — matching the heartbeat's error handling.
        """
        client = self.client
        if client is not None:
            payload = build_telemetry(self)
            try:
                await client.set(
                    self._telemetry_key,
                    value=self._telemetry_encoder.encode(payload),
                    expiry=ExpirySet(ExpiryType.SEC, TELEMETRY_TTL),
                )
            except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
                self.logger.log(logging.WARNING, "Failed to publish ADC telemetry: %s", exc)
                self._health.report_failure(exc)
        await asyncio.sleep(TELEMETRY_INTERVAL)
