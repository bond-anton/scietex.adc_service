# Configuration

The service reads its bootstrap settings from `adc/adc.yml`, a YAML file in an
`adc/` subdirectory of the config directory. The file is the **L1 bootstrap
patch** of the framework's four-layer merge: a field-level map merged onto the
constructor defaults, then overlaid by the framework's `adc/config.yml` snapshot
and the remote `adc` section. The merged result is validated through
`AdcServiceSettings`, a frozen struct with `forbid_unknown_fields=True`, so a
typo in a field name is a hard error, not a silently ignored key.

## Config directory resolution

The config directory is resolved once, at worker construction, by the framework's
`prepare_conf_dir`. The first **existing** directory wins:

1. The `--conf-dir` CLI argument
2. `SCIETEX_CONFIG_DIR`
3. `$XDG_CONFIG_HOME/scietex`
4. `~/.config/scietex`
5. `/etc/scietex`
6. `/usr/local/etc/scietex`
7. `./config` (current working directory)
8. `~/.config/scietex` — **created** if none of the above exist

Only step 8 creates a directory; steps 1–7 require the directory to already
exist. A set-but-nonexistent `SCIETEX_CONFIG_DIR` is silently skipped, so in a
container always create the directory before the worker starts.

## File layout

The service namespaces its files under an `adc/` subdirectory so that multiple
`scietex.*` services sharing one config directory cannot collide:

```
<conf_dir>/
└── adc/
    ├── adc.yml        # service-owned bootstrap (this page)
    └── config.yml     # framework snapshot: core: + services.adc (see remote-config.md)
```

`ADC_CONFIG_SUBDIR` in `config.py` is the single source of truth for the
subdirectory name; `ADC_CONFIG_FILE` names the bootstrap file. The service sets
`config_file="adc/config.yml"`, so the framework's snapshot lands at
`<conf_dir>/adc/config.yml` and holds `core:` plus `services.adc`. The framework
writes exactly this one snapshot per service; there is no separate shared root
`config.yml`.

`adc.yml` is the **L1 bootstrap patch**: a field-level map that holds only the
`adc` section. It can never carry a `core:` key — the merged section is converted
through `AdcServiceSettings`, whose `forbid_unknown_fields=True` rejects that as
an unknown field. The framework never writes `adc.yml`.

## Settings precedence

Effective settings resolve through four layers, later layers winning:

```
constructor default (L0)  <  adc.yml (L1)  <  config.yml (L2)  <  remote adc section (L3)
```

Each layer is a **field-level patch**, not a whole-object replacement. A key has
three states:

- **absent** — inherit the value from the layer below;
- **`null`** — clear the field, falling back to the L0 constructor default;
- **a value** — set the field.

The L0 base is `ADC_SETTINGS_DEFAULTS` (a concrete `AdcServiceSettings`
instance). `config.yml` is the namespaced snapshot at
`<conf_dir>/adc/config.yml`; a successful remote apply auto-writes it, so it
mirrors the last-applied remote config and is re-applied ahead of the remote read
on the next start. See [Remote configuration](remote-config.md).

A `devices` entry is an exception to field-level merge: an entry is replaced
**wholesale**, never deep-merged with the entry of the same id below it. A device
present only in a lower layer is kept; a device present in the winning layer
takes that layer's entry as-is.

## `adc.yml` top-level fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `devices` | mapping | `{}` | Devices keyed by **string** device id |
| `poll_enabled` | bool | `true` | Service-wide polling switch |

Device keys are strings (`adc0`, `pump-pressure`), not integers; the key becomes
the device's `device_id` in task payloads and telemetry, so the key and the id can
never disagree.

## Device fields

Every entry under `devices` is an `AdcDeviceSettings`:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | — (required) | Part name: `MCP3204` or `MCP3208` |
| `port` | int or null | `null` | SPI bus number for the spidev backend |
| `device` | int or null | `null` | Chip-select for the spidev backend |
| `clock_pin` | int or string or null | `null` | SPI clock pin (gpiozero backend) |
| `mosi_pin` | int or string or null | `null` | SPI MOSI pin (gpiozero backend) |
| `miso_pin` | int or string or null | `null` | SPI MISO pin (gpiozero backend) |
| `select_pin` | int or string or null | `null` | Chip-select pin (gpiozero backend) |
| `v_ref` | float or null | `null` | Reference voltage used to scale codes into volts |
| `label` | string or null | `null` | Human-readable label forwarded to the HAL |
| `log_level` | string or null | `null` | Per-device log level forwarded to the HAL |
| `poll` | bool | `true` | Include this device in the background sweep |
| `channels` | list | `[]` | Per-input polling rows; an empty list means the device is not polled |

Fields left unset (`null` or absent) are not forwarded to the HAL constructor, so
the class default applies; this keeps "not configured" distinct from "explicitly
disabled".

### Per-channel polling

A device is sampled only through the rows in its `channels` list — an empty list
means the device is not polled at all. On-demand reads (`adc:read`,
`adc:read_all`) keep working regardless. Each row is an `AdcChannelSettings`:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `channel` | int | — (required) | Single-ended input index |
| `poll_interval_ms` | int | `1000` | Sampling period for this input |
| `enabled` | bool | `true` | Sample this input in the sweep |
| `label` | string or null | `null` | Display label |

There is no device-level period. A row naming an input the part does not have,
or a duplicate row, is dropped with a warning; a period outside
`[100, 3600000]` ms is clamped with a warning.

### Choosing a transport

There are exactly two forms, and they are mutually exclusive:

- **spidev** — set `port` and `device`, both **integers**. `port` is the SPI bus
  number and `device` is the chip-select: `port: 0, device: 0` maps to
  `/dev/spidev0.0`. `/dev/spidevX.Y` is a real path only in a container
  `--device` flag; it is never written in `adc.yml`.
- **gpiozero** — set **any** of `clock_pin`, `mosi_pin`, `miso_pin`,
  `select_pin`. Any one of these keys selects the bit-banged backend instead of
  spidev.

Mixing the two — for example `port` together with `select_pin` — is rejected by
the HAL constructor; the service forwards the configured keywords unchanged and
records the failure as described in [Validation versus degradation](#validation-versus-degradation).

## Supported parts

| `type` | Single-ended channels | Differential pairs | Resolution | `max_code` |
| --- | --- | --- | --- | --- |
| `MCP3204` | 4 | 2 | 12-bit | 4095 |
| `MCP3208` | 8 | 4 | 12-bit | 4095 |

Both parts accept the same constructor keywords, so there is no per-part field
difference: the transport fields, `v_ref`, `label`, and `log_level` apply
identically. Only the geometry differs. The registry table `ADC_PARTS` in
`devices.py` is the single source of truth for the supported names; an unknown
name is handled at apply time (see below).

## Validation versus degradation

Failures split into two classes with different outcomes.

**Decode time — the configuration is rejected.** The merged section is converted
through `AdcServiceSettings` / `AdcDeviceSettings`, both
`forbid_unknown_fields=True`:

- An unknown key at any level is a hard error (a typo is never ignored).
- A missing `type` on a device entry is a validation error.
- A value of the wrong type (for example `port: "zero"`, `poll: 1`) is a hard
  error.

**Apply time — the device is degraded, the worker keeps running.** These are
caught per device by `AdcDeviceRegistry.rebuild` and recorded instead of raised:

- an unknown `type` name (not in `ADC_PARTS`);
- a parameter the part does not accept, or a combination it rejects (including
  mixing the two transport forms);
- a bus that will not open (missing `/dev/spidevX.Y`, pin conflict, permission
  error).

For each degraded case the device appears in `adc:list` and in telemetry with
`available: false` and the recorded `error` string; the other devices stay
usable, and the worker keeps running. A rebuild retries every device that failed,
so a converter that appears later is picked up on the next apply.

### Task error codes

When a task reaches a device that is not usable, the worker maps the exception to
a task error (first match wins):

| Exception | Code | Retryable |
| --- | --- | --- |
| `DeviceNotFound` | `DEVICE_NOT_FOUND` | no — id is not configured |
| `ChannelNotFound` | `CHANNEL_NOT_FOUND` | no — channel or pair index out of range |
| `DeviceUnavailable` | `DEVICE_UNAVAILABLE` | yes — configured but not open |
| `DeviceOpenError` | `DEVICE_UNAVAILABLE` | yes |
| `ADCFrameError` | `ADC_FRAME_ERROR` | yes |
| `TransportError` / `TransportClosed` | `TRANSPORT_ERROR` | yes |
| decode error / `DeviceConfigError` | `INVALID_REQUEST` | no |
| anything else | `DEVICE_IO_ERROR` | yes |

Retryable codes mean the hardware or configuration may recover without the
request changing; non-retryable codes mean the request itself is wrong.

## Worked example

Two MCP3204 converters on the same bus: the first with a reference voltage, the
second excluded from polling.

```yaml
poll_enabled: true
devices:
  adc0:
    type: MCP3204
    port: 0
    device: 0
    v_ref: 3.3
  adc1:
    type: MCP3204
    port: 0
    device: 1
    v_ref: 5.0
    poll: false
```

`adc0` maps to `/dev/spidev0.0` and `adc1` to `/dev/spidev0.1`. Both stay
readable on demand through `adc:read`; only `adc0` is in the background sweep.

## Regenerating defaults

If `adc/adc.yml` is missing, the service writes a default file on first run
(`read_adc_config(..., create_default=True)`, the bootstrap path). If the file
exists but cannot be parsed, it is **left untouched** and the loader raises
`RuntimeError`; fix the file or remove it to regenerate defaults. Passing
`create_default=False` makes a missing file an error instead of writing one.
