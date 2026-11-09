"""Minimal worker and Valkey-client doubles for telemetry tests.

`build_telemetry` reads only ``service_name``, ``instance_id``,
``adc_settings``, ``devices``, and ``poller``, so a real `AdcWorker` is
unnecessary for payload-shape tests. The client double records the single
``set`` call the Telemetry manager makes.
"""

import logging

from scietex.adc_service.devices import AdcDeviceRegistry
from scietex.adc_service.poller import AdcPoller


class FakeAdcWorker:
    """Stand-in exposing exactly the attributes `build_telemetry` reads."""

    def __init__(
        self,
        *,
        settings=None,
        registry: AdcDeviceRegistry | None = None,
        poller: AdcPoller | None = None,
        service_name: str = "adc-test",
        instance_id: str = "instance-1",
    ) -> None:
        self.service_name = service_name
        self.instance_id = instance_id
        self.adc_settings = settings
        self.devices = (
            registry if registry is not None else AdcDeviceRegistry(logging.getLogger("test.adc_service.fake_worker"))
        )
        self.poller = (
            poller
            if poller is not None
            else AdcPoller(
                self.devices,
                logging.getLogger("test.adc_service.fake_worker.poller"),
                service_name=service_name,
                instance_id=instance_id,
            )
        )


class RecordingClient:
    """Valkey client double that records each ``set`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, object]] = []

    async def set(self, key, value=None, expiry=None):
        self.calls.append((key, value, expiry))


class FailingClient:
    """Valkey client double whose every write raises a configured error."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def set(self, key, value=None, expiry=None):
        self.calls += 1
        raise self.exc
