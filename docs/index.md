# Overview

`scietex.adc_service` is a [`scietex.service`](https://github.com/bond-anton/scietex.service)
worker connected to Valkey that owns one or more SPI ADC devices — MCP3204 and
MCP3208 — through
[`scietex.hal.adc`](https://github.com/bond-anton/scietex.hal.adc). It opens the
configured converters, exposes single-ended and differential reads as `adc:*`
tasks, and publishes a telemetry snapshot of the live devices.

## What it does

- **Owns the SPI bus and its converters.** Each configured device is built once
  from `adc/adc.yml` and opened at startup; a device that fails to open does not
  stop the worker.
- **Serves reads as tasks.** Clients submit `adc:read`, `adc:read_all`, and
  `adc:list` over Valkey task lanes; the worker reads the bus at request time.
  Nothing is cached.
- **Runs as a daemon** on the `scietex.service` framework, with signal handling,
  heartbeat, watchdog, and graceful shutdown inherited from `ValkeyWorker`.
- **Accepts remote configuration** through the framework's config channel — see
  [Remote configuration](remote-config.md).
- **Degrades per device.** A converter that will not open is reported as
  `available: false` with the recorded error, and the remaining devices keep
  working.

## How it fits together

```
client ──▶ Valkey task lanes ──▶ AdcWorker ──▶ SPI bus ──▶ MCP3204 / MCP3208
              (control, data)         │
                                      └──▶ telemetry key
```

`AdcWorker` is a `ValkeyWorker` subclass. It owns an `AdcDeviceRegistry`, which
maps each validated settings entry onto a live `scietex.hal.adc` object, and
three task handlers — one control-lane (`adc:list`) and two data-lane
(`adc:read`, `adc:read_all`). The registry is rebuilt whenever the effective
settings change, so a remote config apply is reflected in the live device set.

The HAL device classes are documented in
[`scietex.hal.adc`](https://github.com/bond-anton/scietex.hal.adc).

## Lifecycle

The worker starts whether or not any device opens. It listens on no port and
has no TCP or serial listener of its own: its only interface is the Valkey task
lanes plus the telemetry key.

Device state is per-entry. When the registry rebuilds, each configured device is
opened independently and a failure is recorded rather than raised. In the
degraded case where zero devices open:

- `adc:read` and `adc:read_all` against a configured but unopened id return
  `DEVICE_UNAVAILABLE` (retryable — the hardware may reappear).
- `adc:list` still succeeds and reports each device with `available: false` and
  the recorded `error`.
- Telemetry carries the same `available: false` and `error` fields.

An id that was never configured is a different outcome: `DEVICE_NOT_FOUND`,
which is a request error, not a hardware state.

## Requirements

- **Python** 3.10 or higher.
- An **SPI bus** reachable as `/dev/spidevX.Y`, or the gpiozero pin backend with
  `clock_pin`/`mosi_pin`/`miso_pin`/`select_pin` wired. The `spi` or `gpiozero`
  extra supplies the matching backend — see the
  [README](https://github.com/bond-anton/scietex.adc_service#installation).
- Network access to a **Valkey** (Redis-compatible) instance for the worker
  framework's task transport.
- **Linux** (the container image targets Debian Bookworm).

## Configuration layers

Effective settings resolve through four layers, later layers winning: constructor
defaults < `adc/adc.yml` < the framework's `adc/config.yml` snapshot < the remote
`adc` section. See [Configuration](configuration.md) for the schema, the merge
rules, and validation behavior.

## Tasks

| Task | Lane | Request | Result |
|---|---|---|---|
| `adc:list` | control | `device_id` (optional; omit to list all) | `devices[]` with `device_id`, `available`, `error`, `v_ref`, `channels`, `differential`, `poll`; plus `poll_enabled` |
| `adc:read` | data | `device_id`, `channel` (default `0`), `differential` (default `false`) | `device_id`, `channel`, `differential`, `code`, `voltage` |
| `adc:read_all` | data | `device_id` | `device_id`, `codes`, `voltages` |

There is no `adc:read_differential` task. Differential reads are `adc:read` with
`differential: true`, where `channel` is the differential-pair index rather than
a single-ended input index.

`voltage` is `code * v_ref / 4096`, or `null` when the device has no `v_ref`
configured. `adc:read_all` reads the reference once per call so every channel in
one response is scaled with the same value.

## Telemetry

The worker publishes to
`scietex:AdcService:<instance_id>:telemetry` — msgpack, every 2.0 s, with a 6 s
TTL (so a single missed beat does not expire the key). The snapshot lists every
configured device with its open state, `v_ref`, `channel_count`, and `poll` flag,
plus the service-wide `poll_enabled` switch. `channel_count` is the opened
part's single-ended input count and is absent for a device that failed to open.

Polling is opt-out in two places: `poll: false` on a device entry excludes it
from the background sweep while keeping it readable on demand, and
`poll_enabled: false` disables the sweep globally, so no polled channel data is
published. A device also stays silent until it has explicit `channels` rows.

## Documentation

| Page | Contents |
| --- | --- |
| [Configuration](configuration.md) | The `adc.yml` schema, defaults, precedence, and validation |
| [Deployment](deployment.md) | Container, SPI device access, Quadlet |
| [Remote configuration](remote-config.md) | The declarative `adc` section |
