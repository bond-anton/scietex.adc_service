"""Test doubles for the SPI ADC HAL plus shared registry fixtures."""

import logging

import pytest

from scietex.adc_service import devices
from scietex.adc_service.devices import AdcDeviceRegistry, PartSpec

#: Distinguishes "not forwarded by the builder" from an explicit ``None`` in the
#: fake constructors, so a test can assert the builder forwards only set fields.
_UNSET = object()


class FakeAdcChannel:
    """Stand-in for `hal.ADCChannel` returning a fixed code."""

    def __init__(self, index: int, code: int = 0, v_ref: float | None = 3.3) -> None:
        self.index = index
        self.code = code
        self.v_ref = v_ref

    @property
    def value(self) -> int:
        return self.code

    @property
    def voltage(self) -> float | None:
        if self.v_ref is None:
            return None
        return self.code * self.v_ref / 4096


class FakeADC:
    """Stand-in for `hal.ADC` with the same geometry and accessor surface."""

    def __init__(
        self,
        *,
        channels: int = 8,
        differential: int = 4,
        v_ref=_UNSET,
        port=_UNSET,
        device=_UNSET,
        clock_pin=_UNSET,
        mosi_pin=_UNSET,
        miso_pin=_UNSET,
        select_pin=_UNSET,
        label=_UNSET,
        log_level=_UNSET,
    ) -> None:
        if v_ref is not _UNSET and v_ref is not None and v_ref <= 0:
            raise ValueError(f"Expected a positive v_ref, got {v_ref!r}")
        self.v_ref = None if v_ref is _UNSET else v_ref
        self.channels: tuple[FakeAdcChannel, ...] = tuple(FakeAdcChannel(index, v_ref=self.v_ref) for index in range(channels))
        self.differential: tuple[FakeAdcChannel, ...] = tuple(
            FakeAdcChannel(index, v_ref=self.v_ref) for index in range(differential)
        )
        self.closed = False
        self.init_kwargs = {
            name: value
            for name, value in {
                "port": port,
                "device": device,
                "clock_pin": clock_pin,
                "mosi_pin": mosi_pin,
                "miso_pin": miso_pin,
                "select_pin": select_pin,
                "v_ref": v_ref,
                "label": label,
                "log_level": log_level,
            }.items()
            if value is not _UNSET
        }

    def read_all(self) -> tuple[int, ...]:
        return tuple(channel.value for channel in self.channels)

    def channel(self, index: int) -> FakeAdcChannel:
        if not 0 <= index < len(self.channels):
            raise IndexError(f"Channel {index} does not exist")
        return self.channels[index]

    def differential_channel(self, pair: int) -> FakeAdcChannel:
        if not 0 <= pair < len(self.differential):
            raise IndexError(f"Differential pair {pair} does not exist")
        return self.differential[pair]

    def close(self) -> None:
        self.closed = True


class BrokenADC(FakeADC):
    """Fake device whose constructor fails at the bus level."""

    def __init__(self, *, v_ref=_UNSET) -> None:
        raise OSError("SPI bus is gone")


class NoPinsADC(FakeADC):
    """Fake device that accepts only the shared keyword arguments."""

    def __init__(self, *, v_ref=_UNSET, label=_UNSET) -> None:
        super().__init__(v_ref=v_ref, label=label)


class StubbornCloseADC(FakeADC):
    """Fake device whose close() fails, exercised by the rebuild path."""

    def close(self) -> None:
        raise OSError("already gone")


@pytest.fixture
def fake_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the part table with the fake classes (restored by monkeypatch)."""
    monkeypatch.setattr(
        devices,
        "ADC_PARTS",
        {
            "FAKE": PartSpec(FakeADC),
            "BROKEN": PartSpec(BrokenADC),
            "NOPINS": PartSpec(NoPinsADC),
            "STUBBORN": PartSpec(StubbornCloseADC),
        },
    )


@pytest.fixture
def registry() -> AdcDeviceRegistry:
    return AdcDeviceRegistry(logging.getLogger("test.adc_service.devices"))
