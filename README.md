# scietex.adc_service

**scietex.adc_service** is a [`scietex.service`](https://github.com/bond-anton/scietex.service)
worker connected to Valkey that owns one or more SPI ADC devices (MCP3204,
MCP3208) through [`scietex.hal.adc`](https://github.com/bond-anton/scietex.hal.adc).
It opens the configured converters at startup, exposes single-ended and
differential reads as `adc:*` tasks, and publishes a telemetry snapshot of the
live devices. A device that fails to open does not stop the worker: it is
recorded, reported as unavailable, and the remaining devices keep working.
Configuration can also be pushed at runtime through the framework's
remote-config channel.

**Python ≥ 3.10** · **License: MIT**

## Documentation

- [Overview](docs/index.md) — what the service does and how it fits together
- [Configuration](docs/configuration.md) — the `adc.yml` schema and precedence
- [Deployment](docs/deployment.md) — container, SPI device access, Quadlet
- [Remote configuration](docs/remote-config.md) — the declarative `adc` section

## Installation

```bash
pip install "scietex.adc_service[spi]"      # spidev backend for /dev/spidev*
pip install "scietex.adc_service[gpiozero]" # plain-GPIO backend
```

The core package pulls in `scietex.hal.adc`, `scietex.service[valkey]`, and
`msgspec`. The backend extras are optional: a core install can still build a
device from a caller-supplied transport, but an operator normally installs the
extra that matches the board's wiring.

## Quick start

### 1. Write a configuration file

The service reads `adc/adc.yml` under its config directory. Create it by hand,
or let the service generate a default on first run. The file is the bootstrap
layer of a four-layer merge: constructor defaults < `adc.yml` < the framework's
`config.yml` snapshot < the remote `adc` section.

### 2. Run the service

```bash
start-adc-service --conf-dir /etc/scietex
```

The config directory resolves automatically when `--conf-dir` is omitted. The
service runs in the foreground until `SIGINT` or `SIGTERM`.

### 3. Read a channel

Submit an `adc:read` task to the worker with payload
`{"device_id": "adc0", "channel": 0}`. The result carries the raw 12-bit `code`
and the scaled `voltage`.

## Configuration at a glance

Devices are keyed by a **string** id, which names the device in task payloads
and telemetry:

```yaml
devices:
  adc0:
    type: MCP3208
    port: 0
    device: 0
    v_ref: 3.3
  adc1:
    type: MCP3204
    device: 1
    v_ref: 5.0
    poll: false
poll_enabled: true
```

Per-device fields: `type` (required, `MCP3204` or `MCP3208`), `port` and `device`
for the SPI wiring, `clock_pin`/`mosi_pin`/`miso_pin`/`select_pin` for the
gpiozero backend, `v_ref`, `label`, `log_level`, and `poll` (default `true`).
`port` is the SPI bus number and `device` is the chip-select; both are integers.
Top-level fields: `devices` and `poll_enabled` (default `true`). Unknown fields
are rejected.

## Tasks

| Task | Lane | Request | Result |
|---|---|---|---|
| `adc:list` | control | optional `device_id` | per-device `device_id`, `available`, `error`, `v_ref`, `channels`, `differential`, `poll`; plus `poll_enabled` |
| `adc:read` | data | `device_id`, `channel`, `differential` | `device_id`, `channel`, `differential`, `code`, `voltage` |
| `adc:read_all` | data | `device_id` | `device_id`, `codes`, `voltages` |

Differential reads use `adc:read` with `differential: true`; `channel` then
names a differential pair instead of a single-ended input. An id that is
configured but failed to open is a successful `adc:list` entry with
`available: false`, not a task error.

## Telemetry

The worker publishes to `scietex:{service}:{instance_id}:telemetry` every 2 s
with a 6 s TTL. The snapshot lists every configured device with its open state,
`v_ref`, channel counts, and `poll` flag, plus the service-wide `poll_enabled`
switch. Polling is opt-out both per device (`poll: false`) and globally
(`poll_enabled: false`).

## Container and quadlet

```bash
podman build --build-arg VERSION=0.1.0 -t scietex-adc-service .
podman run --rm --device /dev/spidev0.0 -v ./config:/config scietex-adc-service
```

The container only dials Valkey, so it publishes no port and needs no
capabilities. `/dev/spidevX.Y` must be passed with `--device`; without it the
worker still starts and reports the device unavailable.

For a persistent deployment, install `deploy/adc-service.container` as
`~/.config/containers/systemd/adc-service.container` and the SPI drop-in as
`~/.config/containers/systemd/adc-service.container.d/50-spi-devices.conf`,
then `systemctl --user daemon-reload && systemctl --user start adc-service`.
The Quadlet bind-mounts `~/.config/scietex`, so `adc/adc.yml` stays
hand-editable on the host.

## Development

Dependencies are managed with **uv**; checks and tests run through **tox**:

```bash
uv sync --all-extras
uv run tox                    # format, lint, type, py314
uv run tox -e py314           # tests only
```
