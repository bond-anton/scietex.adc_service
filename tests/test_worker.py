"""Tests for the AdcWorker lifecycle, config apply hook, and handler wiring."""

import logging
from pathlib import Path

import pytest

from scietex.adc_service import adc_worker as adc_worker_module
from scietex.adc_service.adc_worker import AdcWorker
from scietex.adc_service.config import (
    ADC_CONFIG_FILE,
    ADC_CONFIG_SUBDIR,
    ADC_SECTION,
    AdcDeviceSettings,
    AdcServiceSettings,
)
from scietex.service import TaskProcessor, ValkeyWorker, ValkeyWorkerConfig
from scietex.service.config_reload import ConfigApplyOutcome


def _make_worker(tmp_path) -> AdcWorker:
    """Build a worker with a real conf_dir and no remote-config handlers."""
    return AdcWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


def _make_remote_worker(tmp_path) -> AdcWorker:
    """Build a worker with remote config enabled and a namespaced snapshot file."""
    return AdcWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=True,
            config_file=f"{ADC_CONFIG_SUBDIR}/config.yml",
        )
    )


@pytest.fixture
def fake_bootstrap(monkeypatch):
    """Return a setter that replaces the L1 bootstrap provider's loader."""

    def _set(patch: dict) -> None:
        monkeypatch.setattr(adc_worker_module, "read_adc_config", lambda conf_dir, **kwargs: patch)

    return _set


@pytest.fixture
def skip_valkey(monkeypatch):
    """Replace ``ValkeyWorker.initialize`` with the L0+L1 seeding step only."""

    async def fake_initialize(self) -> bool:
        self.seed_config_bootstrap()
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)


@pytest.mark.asyncio
async def test_cleanup_safe_when_never_started(tmp_path) -> None:
    """cleanup() is a no-op when the worker was never initialized."""
    worker = _make_worker(tmp_path)

    await worker.cleanup()

    assert worker.adc_settings is None
    assert worker.devices.generation == 0
    assert worker.devices.ids() == []


@pytest.mark.asyncio
async def test_initialize_survives_unopenable_device(tmp_path, fake_parts, fake_bootstrap, skip_valkey) -> None:
    """A device that fails to open is recorded; startup still succeeds."""
    fake_bootstrap({"devices": {"adc0": {"type": "BROKEN"}}})
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True

    assert worker.adc_settings is not None
    assert worker.adc_settings.devices["adc0"].type == "BROKEN"
    assert worker.devices.ids() == ["adc0"]
    assert "SPI bus is gone" in worker.devices.errors["adc0"]
    await worker.cleanup()


@pytest.mark.asyncio
async def test_initialize_fails_when_settings_unresolved(tmp_path, monkeypatch, skip_valkey, caplog) -> None:
    """A bootstrap loader failure leaves the section unresolved and fails startup."""

    def bad_loader(conf_dir, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(adc_worker_module, "read_adc_config", bad_loader)
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert worker.adc_settings is None
    assert worker.devices.generation == 0
    assert any("were not resolved" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_uses_merged_settings(tmp_path, fake_parts, fake_bootstrap, skip_valkey) -> None:
    """initialize() reads the merged struct: the bootstrap patch overlays L0."""
    fake_bootstrap({"devices": {"adc0": {"type": "FAKE", "v_ref": 3.3}}, "poll_enabled": False})
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True

    settings = worker.adc_settings
    assert settings is not None
    assert settings.poll_enabled is False
    assert settings.devices["adc0"].v_ref == 3.3
    # The registry is built from the same merged struct the worker stores.
    assert worker.devices.settings is settings
    assert worker.devices.ids() == ["adc0"]
    await worker.cleanup()


def test_apply_hook_stores_without_rebuild_before_startup(tmp_path) -> None:
    """Before startup the apply hook stores settings without opening devices."""
    worker = _make_worker(tmp_path)
    settings = AdcServiceSettings(devices={"adc0": AdcDeviceSettings(type="FAKE")})

    worker._apply_adc_settings(settings)

    assert worker.adc_settings is settings
    assert worker.devices.generation == 0
    assert worker.devices.settings is None
    assert worker.devices.ids() == []


@pytest.mark.asyncio
async def test_apply_hook_rebuilds_after_startup(tmp_path, fake_parts, fake_bootstrap, skip_valkey) -> None:
    """A config delivery after startup rebuilds the live registry."""
    fake_bootstrap({"devices": {"a": {"type": "FAKE"}}})
    worker = _make_worker(tmp_path)
    assert await worker.initialize() is True
    assert worker.devices.generation == 1

    applied = AdcServiceSettings(
        devices={
            "a": AdcDeviceSettings(type="FAKE"),
            "b": AdcDeviceSettings(type="FAKE", v_ref=5.0),
        }
    )
    worker._apply_adc_settings(applied)

    assert worker.adc_settings is applied
    assert worker.devices.generation == 2
    assert worker.devices.ids() == ["a", "b"]
    assert worker.devices.get("b").v_ref == 5.0
    await worker.cleanup()


@pytest.mark.asyncio
async def test_handlers_registered_on_their_lanes(tmp_path) -> None:
    """The list handler is control-lane; both read handlers are data-lane."""
    worker = _make_worker(tmp_path)

    assert await TaskProcessor.initialize(worker) is True

    assert "AdcListHandler" in worker.control_task_handlers
    assert "AdcListHandler" not in worker.task_handlers
    assert {"AdcReadHandler", "AdcReadAllHandler"} <= set(worker.task_handlers)
    assert not {"AdcReadHandler", "AdcReadAllHandler"} & set(worker.control_task_handlers)


@pytest.mark.asyncio
async def test_remote_apply_persists_namespaced_config_yml(tmp_path, monkeypatch) -> None:
    """A successful remote apply mirrors the config under adc/config.yml."""

    async def fake_remote_outcome(self) -> ConfigApplyOutcome:
        return ConfigApplyOutcome(applied=True, revision=1, hash="abc123", changed=[])

    monkeypatch.setattr(ValkeyWorker, "_read_remote_outcome", fake_remote_outcome)
    worker = _make_remote_worker(tmp_path)

    await worker._reload_remote_config()

    snapshot = Path(tmp_path) / ADC_CONFIG_SUBDIR / "config.yml"
    assert snapshot.is_file()
    assert snapshot.read_bytes()


@pytest.mark.asyncio
async def test_remote_apply_does_not_rewrite_adc_yml(tmp_path, monkeypatch, fake_bootstrap) -> None:
    """A remote apply writes only config.yml; adc.yml stays a pure bootstrap."""
    fake_bootstrap({"devices": {"adc0": {"type": "FAKE"}}})

    async def fake_remote_outcome(self) -> ConfigApplyOutcome:
        return ConfigApplyOutcome(applied=True, revision=1, hash="abc123", changed=[])

    monkeypatch.setattr(ValkeyWorker, "_read_remote_outcome", fake_remote_outcome)
    worker = _make_remote_worker(tmp_path)
    adc_yml = Path(tmp_path) / ADC_CONFIG_SUBDIR / ADC_CONFIG_FILE
    adc_yml.parent.mkdir(parents=True, exist_ok=True)
    original = b"devices: {}\npoll_enabled: true\n"
    adc_yml.write_bytes(original)
    worker.seed_config_bootstrap()

    await worker._reload_remote_config()

    assert adc_yml.read_bytes() == original


def test_section_name_is_registered(tmp_path) -> None:
    """The worker registers its settings under the adc section."""
    worker = _make_worker(tmp_path)
    worker.seed_config_bootstrap()

    assert worker.current_config_settings(ADC_SECTION) == AdcServiceSettings()
