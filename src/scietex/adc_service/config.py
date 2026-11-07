"""Configuration models and YAML loader for the ADC service.

`AdcServiceSettings` is the concrete constructor (L0) base and the validation
target of the framework's four-layer config merge:

* L0 — `ADC_SETTINGS_DEFAULTS`, the builtins of a default struct instance.
* L1 — the service-owned ``adc/adc.yml`` bootstrap patch (`read_adc_config`).
* L2 — the framework's ``config.yml`` snapshot from the last successful apply.
* L3 — the remote ``adc`` section of a desired-state envelope.

The layers are patch dicts merged field by field with the three-state rule: a
key absent from every layer inherits the value below, a key present with
``null`` clears it (so the L0 value resurfaces), and a key present with a value
sets it. The struct is kept deliberately thin: backend selection, pin routing,
and part geometry are validated by the HAL constructors in
`devices.build_adc`, so the YAML schema only constrains types and unknown
fields.

Device entries are keyed by a **string** device id (``devices: {adc0: {...}}``),
not an integer. An id names a device in task payloads and telemetry, and a YAML
mapping key is a string anyway, so an ``int`` key would need silent coercion on
every hop. `dict[str, AdcDeviceSettings]` decodes string keys verbatim and
round-trips through `msgspec.to_builtins` unchanged.
"""

from pathlib import Path

import msgspec

#: Remote-config section name the settings are registered under.
ADC_SECTION: str = "adc"

#: Subdirectory under the shared config dir that namespaces this service's
#: files. The framework's ``config.yml`` snapshot and the service-owned
#: ``adc.yml`` both live here, so services sharing one config dir (the
#: framework resolves a single dir for all scietex services) cannot collide.
ADC_CONFIG_SUBDIR: str = "adc"

#: Filename of the service-owned L1 bootstrap patch in the config subdirectory.
ADC_CONFIG_FILE: str = "adc.yml"


class AdcDeviceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One SPI ADC device, keyed by its id in `AdcServiceSettings.devices`.

    ``type`` selects the HAL class through `devices.ADC_PARTS`. Every remaining
    field is optional and forwarded to that class only when set, so the backend
    keywords (``port``/``device`` SPI bus and chip-select) and the shared keywords (``v_ref``,
    ``label``, ``log_level``) can be mixed as the part and its transport
    require. ``None`` means "not configured": the constructor default applies.

    ``poll`` is service-level, not a HAL argument: it excludes a device from
    `AdcDeviceRegistry.poll_targets` while keeping it addressable for on-demand
    reads.
    """

    type: str
    port: int | None = None
    device: int | None = None
    clock_pin: int | str | None = None
    mosi_pin: int | str | None = None
    miso_pin: int | str | None = None
    select_pin: int | str | None = None
    v_ref: float | None = None
    label: str | None = None
    log_level: str | None = None
    poll: bool = True


class AdcServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level ADC service settings.

    ``poll_enabled`` is the service-wide polling switch; a device additionally
    opts out through `AdcDeviceSettings.poll`.
    """

    devices: dict[str, AdcDeviceSettings] = msgspec.field(default_factory=dict)
    poll_enabled: bool = True


#: Concrete L0 base for the framework's layered merge. A field absent from every
#: layer, or explicitly cleared with ``null``, resolves to the value here.
ADC_SETTINGS_DEFAULTS: AdcServiceSettings = AdcServiceSettings()


def read_adc_config(conf_dir: Path | None, *, create_default: bool = True) -> dict[str, object]:
    """Read the L1 bootstrap patch from ``adc/adc.yml`` under the config directory.

    The service's files are namespaced in an ``adc/`` subdirectory so they do
    not collide with other services sharing the framework's single config dir.
    The file (and, when missing, its directory) is only created when
    ``create_default=True`` (the bootstrap path). A ``None`` or non-directory
    ``conf_dir``, a missing file/directory with ``create_default=False``, or an
    unparseable file each raise `RuntimeError`. An existing-but-invalid file is
    left untouched regardless of ``create_default``.

    The return value is the raw ``adc.yml`` mapping — the L1 patch the framework
    merges onto the L0 base and validates through `AdcServiceSettings`. When the
    file is just created, the builtins of the default struct are returned so the
    patch is equivalent to an empty override.

    Args:
        conf_dir: Path to the configuration directory.
        create_default: Whether to create the directory and write a default
            ``adc.yml`` when missing. Default ``True``.

    Returns:
        The parsed ``adc.yml`` mapping as the L1 patch, or the builtins of the
        default `AdcServiceSettings` when the file was just created.

    Raises:
        RuntimeError: If ``conf_dir`` is ``None`` or not a directory, the file
            is missing with ``create_default=False``, or the file cannot be parsed.
    """
    if not isinstance(conf_dir, Path):
        raise RuntimeError("Configuration dir was not set!")
    service_dir = conf_dir / ADC_CONFIG_SUBDIR
    if not service_dir.exists():
        if create_default:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise RuntimeError(f"Failed to create configuration directory {service_dir}!") from exc
        else:
            raise RuntimeError(
                f"Configuration directory {service_dir} does not exist and create_default=False (no default generated)."
            )
    elif not service_dir.is_dir():
        raise RuntimeError(f"Provided configuration directory path {service_dir} is not a directory!")
    adc_yml = service_dir.joinpath(ADC_CONFIG_FILE)
    if not adc_yml.exists():
        if create_default:
            settings = AdcServiceSettings()
            with open(adc_yml, "wb") as f:
                f.write(msgspec.yaml.encode(settings))
            return msgspec.to_builtins(settings)
        raise RuntimeError(
            f"ADC configuration file {adc_yml} does not exist and create_default=False "
            "(pass create_default=True to generate defaults)."
        )
    try:
        with open(adc_yml, "rb") as f:
            return msgspec.yaml.decode(f.read(), type=dict)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse ADC configuration file {adc_yml}. Fix the file or remove it to regenerate defaults."
        ) from exc
