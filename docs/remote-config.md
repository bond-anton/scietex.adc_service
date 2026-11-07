# Remote configuration

The service registers an `adc` section with the framework's remote-config
system. An operator can push ADC settings at runtime over the framework's config
channel (a Valkey key) instead of editing `adc/adc.yml` and restarting.

Unlike a restart-required service, the ADC worker **applies live**: every apply
rebuilds the device registry from the merged settings, so a change reaches the
hardware on the next apply without a restart. See
[Deployment](deployment.md) for the container wiring and
[Configuration](configuration.md) for the `adc.yml` format.

## How it is registered

`AdcWorker.__init__` registers the section:

```python
self.register_config_settings(
    ADC_SECTION,  # "adc"
    AdcServiceSettings,
    apply=self._apply_adc_settings,
    defaults=ADC_SETTINGS_DEFAULTS,  # L0 base
    bootstrap=lambda: read_adc_config(self.conf_dir),  # L1 patch dict
)
```

`defaults` is the concrete L0 base; `bootstrap` returns the `adc/adc.yml` patch
dict. The framework seeds L0+L1 during `initialize()` and validates the merged
section against `AdcServiceSettings`, then calls the synchronous apply hook.

The hook stores the settings and — once startup has completed — calls
`AdcDeviceRegistry.rebuild(settings)`, which closes the old devices and opens the
configured set again. During startup the hook only stores: `initialize()`
rebuilds the registry once from the final merged settings, so devices are not
opened twice.

## Apply behavior

| Worker state | What happens on apply |
| --- | --- |
| **Before startup** (registry not yet built) | Settings are stored; `initialize()` builds the registry from them. |
| **Running** (registry already built) | Settings are stored and the whole registry is rebuilt immediately. |

A rebuild reopens **every** configured device, not just the changed one. A
device that fails to open is recorded and the rest keep working.

| Field changed | Effect |
| --- | --- |
| `devices` — any entry added, removed, or edited | Full registry rebuild: all configured devices are closed and reopened on the next apply |
| `devices.<id>.port` / `device` | Full rebuild, because it is a `devices` change |
| `poll_enabled` | Cheap: stored only, no device is reopened; the telemetry `poll` flag follows the new value |
| Unknown field | Rejected by struct validation (`forbid_unknown_fields`), no state change |

A **per-device** apply failure does not reject the envelope. The apply reports
success, the failed device is recorded with `available: false` and the reason,
and that state surfaces in both `adc:list` output and the telemetry snapshot. A
registry-wide failure (for example, the settings as a whole cannot be
reconciled) does propagate and the framework reports a failed apply.

## Settings precedence

```
constructor default (L0)  <  adc/adc.yml (L1)  <  adc/config.yml (L2)  <  remote adc section (L3)
```

Each layer is a field-level patch (RFC 7396): a key absent from a layer inherits
the layer below, `null` clears it back to the L0 default, and a value sets it.
The remote section is authoritative when present. On every run the worker starts
from the L0 baseline, seeds the L1 bootstrap, and re-applies the local snapshot
and remote source from scratch.

## `config:*` commands

The framework exposes three task types for remote config (available because the
service sets `remote_config_enabled=True`):

| Command | Purpose |
| --- | --- |
| `config:apply` | Apply an envelope (inline, or re-read the source of truth) |
| `config:store` | Persist the effective config to disk, remote, or both |
| `config:show` | Inspect the effective and declarative settings |

These are framework-level commands; their request/response schemas are
documented in the
[`scietex.service` remote-config guide](https://github.com/bond-anton/scietex.service/blob/main/docs/remote_config.md).

### `config:store` and the config files

The framework auto-persists the effective config to `<conf_dir>/adc/config.yml`
on every successful remote apply at startup, not only on an explicit
`config:store`. The snapshot mirrors the last-applied remote config and is
re-applied ahead of the remote read on the next start.

`adc/adc.yml` is never rewritten by the framework; it stays a pure first-run
bootstrap. `config:store` remains available for explicit persistence to disk,
remote, or both (`target`).

## Reading the applied state

`adc:list` is the ground truth for what is actually open. Each entry reports
`available` and, when false, the reason recorded at open time. The same data is
published in the telemetry key `scietex:AdcService:<instance_id>:telemetry`
every 2 s with a 6 s TTL — a device can disappear from the hardware while the
worker stays healthy, and only the `available` flag shows it.

Practical loop for a hardware change:

1. Push the new `devices` mapping with `config:apply`.
2. Read `adc:list` and confirm every intended device reports `available: true`.
3. Check telemetry if the list handler is unreachable — the two share the
   registry, so they never disagree.

## Security

The framework's remote-config security model applies unchanged:

- **Allowlist by type.** Only fields representable in `AdcServiceSettings` can
  be set remotely; unknown fields are rejected.
- **Validate-before-swap.** A candidate is constructed through the real struct
  before it is applied; a bad value is rejected with no state change.
- **Broker ACLs are the primary defense.** Anyone able to write the config key
  can influence worker behavior — including which device nodes are opened.
  Restrict those writes to trusted operators at the broker.

See the
[`scietex.service` security model](https://github.com/bond-anton/scietex.service/blob/main/docs/remote_config.md#security-model)
for the full contract.

## See also

- [Configuration](configuration.md) — `adc/adc.yml` and the settings layers
- [Deployment](deployment.md) — container, SPI access, diagnostics
- [Index](index.md)
