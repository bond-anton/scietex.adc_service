# Deployment

How to run `scietex.adc_service` as a container (Podman or Docker) on a host
with SPI ADC hardware, and how to give the worker access to the SPI device
nodes. The service is a plain Python console script that talks to Valkey; see
[Configuration](configuration.md) for the `adc/adc.yml` format and
[Remote configuration](remote-config.md) for runtime updates.

## Entry point

The package installs one console script:

```bash
start-adc-service
```

| Flag | Environment fallback | Default | Meaning |
| --- | --- | --- | --- |
| `--conf-dir` | `SCIETEX_CONFIG_DIR` | auto-resolved | Configuration directory containing `adc/adc.yml` |
| `--service-name` | `SCIETEX_SERVICE_NAME` | `AdcService` | Service name reported to the framework |
| `--logging-level` | `SCIETEX_LOGGING_LEVEL` | `INFO` | Logging level |

Precedence is **CLI flag > environment variable > built-in default**.
`--conf-dir` defaults to `None` so the framework's `prepare_conf_dir` resolves
the directory itself — that resolver already honors `SCIETEX_CONFIG_DIR`.

The worker runs in the foreground until the framework tears it down. `podman
stop` / `systemctl --user stop` send `SIGTERM`, which the framework handles
without extra wiring.

## Container image

| Item | Value |
| --- | --- |
| Image | `registry.buro-nts.ru/scietex-adc-service` |
| Entrypoint | `start-adc-service` |
| Config dir | `/config` (`SCIETEX_CONFIG_DIR`) |
| Config volume | `VOLUME ["/config"]` |
| User | `appuser` (non-root) |

The image installs the released wheel
`scietex.adc_service[$EXTRAS]==$VERSION` from PyPI, so **the matching version
must already be published on PyPI** or the build fails at the `pip install`
step. The image and the PyPI artifact for a version are the same code.

`EXTRAS` selects the ADC backend:

| Extra | Installs | For |
| --- | --- | --- |
| `spi` (default) | `scietex.hal.adc[spi]` | `/dev/spidev*` SPI buses |
| `gpiozero` | `scietex.hal.adc[gpiozero]` | plain GPIO pins via `gpiochip` |

## Build

```bash
./build_image.sh
```

```bash
./build_image.sh --latest
```

The script reads the version from `src/scietex/adc_service/version.py`, builds a
multi-arch manifest for `linux/amd64,linux/arm64`, installs the matching PyPI
wheel, and pushes to `registry.buro-nts.ru/scietex-adc-service`. `--latest`
pushes the additional `latest` tag.

To build for the GPIO backend:

```bash
./build_image.sh --extras gpiozero
```

`--extras` accepts any extra name from `pyproject.toml`. Builds are multi-arch
and pushed directly to the registry; there is no local-only mode.

## Run

```bash
podman run --rm \
  --device /dev/spidev0.0 \
  -v ~/.config/scietex:/config:z \
  registry.buro-nts.ru/scietex-adc-service:latest
```

| Option | Purpose |
| --- | --- |
| `--device /dev/spidev0.0` | Passes the SPI node into the container |
| `-v ~/.config/scietex:/config:z` | Host-owned config tree, SELinux-relabelled as shared (`:z`) |

Without `--device`, the worker still starts: the device node is absent, so the
registry records every configured device as unavailable and `adc:list` reports
`DEVICE_UNAVAILABLE` for each. Nothing else fails.

## Quadlet (systemd user unit)

Install `deploy/adc-service.container` as
`~/.config/containers/systemd/scietex-adc-service.container`:

```ini
[Unit]
Description=Scietex ADC service (SPI)
After=network-online.target
Wants=network-online.target

[Container]
Image=registry.buro-nts.ru/scietex-adc-service:latest
ContainerName=scietex-adc-service
AutoUpdate=registry
Environment=SCIETEX_SERVICE_NAME=AdcService
Environment=SCIETEX_LOGGING_LEVEL=INFO
EnvironmentFile=-%h/.config/scietex/adc.env
# Bind mount rather than a named volume so adc/adc.yml stays hand-editable on
# the host at ~/.config/scietex/adc/adc.yml.
Volume=%h/.config/scietex:/config:z
AddDevice=/dev/spidev0.0
# The service only dials Valkey; it never listens. No PublishPort, no
# NET_BIND_SERVICE.
NoNewPrivileges=true
ReadOnly=true
Tmpfs=/tmp:rw,size=16m

[Service]
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

Then reload systemd and start it:

```bash
systemctl --user daemon-reload
systemctl --user start scietex-adc-service
```

The `Volume=` line bind-mounts the host config tree instead of using a named
volume, so `~/.config/scietex/adc/adc.yml` is editable on the host and survives
container replacement. `:z` applies the shared SELinux relabel
(`container_file_t`, no private MCS category), which is what a tree mounted by
more than one container needs. `ReadOnly=true` makes the container root
filesystem read-only; `Tmpfs=/tmp` supplies the only writable scratch space.

The service never listens on a socket — it only dials Valkey — so the unit has
no `PublishPort`, the image has no `EXPOSE`, and no `NET_BIND_SERVICE` capability
is needed.

## SPI device drop-in

Install `deploy/adc-service.container.d/50-spi-devices.conf` as
`~/.config/containers/systemd/scietex-adc-service.container.d/50-spi-devices.conf`:

```ini
[Container]
# Additional SPI buses and, for the gpiozero backend, the GPIO controller.
AddDevice=/dev/spidev0.1
AddDevice=/dev/spidev1.0
AddDevice=/dev/gpiochip0
# Map the container uid to the invoking user so host-owned device nodes are
# writable inside the container.
UserNS=keep-id
# Pass the host supplementary groups that own the device node through to the
# container, without the image knowing their GIDs.
GroupAdd=keep-groups
# Device nodes cannot be relabelled for the container; disable per-container
# SELinux labelling for this unit.
SecurityLabelDisable=true
# DeviceAllow= is a [Service] systemd key, not a Quadlet [Container] key. It
# belongs in [Service] only if this unit is additionally hardened with
# DevicePolicy=closed; the default policy already permits the AddDevice= nodes.
```

The four settings exist because rootless Podman does not otherwise reach the
device nodes:

| Setting | Rationale |
| --- | --- |
| `AddDevice=` | Makes the listed nodes present inside the container; extend the list for every bus or `gpiochip` used |
| `UserNS=keep-id` | Maps the container uid to the invoking host user, so a host-owned device node is openable |
| `GroupAdd=keep-groups` | Passes the host supplementary groups that own the node through, without baking their GIDs into the image |
| `SecurityLabelDisable=true` | Device nodes cannot be relabelled per container, so per-container SELinux labelling is turned off for this unit |

`DeviceAllow=` is a `[Service]` systemd key, not a `[Container]` Quadlet key. It
is only relevant when the unit additionally sets `DevicePolicy=closed`; with the
default policy the `AddDevice=` nodes are already permitted.

## Diagnostics

| Symptom | Cause |
| --- | --- |
| No `/dev/spidev*` node inside the container | `--device` (or Quadlet `AddDevice=`) missing |
| `PermissionError` opening the node | uid/group mapping — the drop-in (`UserNS=keep-id`, `GroupAdd=keep-groups`) is not installed |
| `No such device` | Wrong bus or chip-select index in the device's `port` / `device` field |
| `voltage` is `null` | `v_ref` not configured for that device (by design) |
| `DEVICE_NOT_FOUND` from a read | Device id absent from `adc/adc.yml` |
| `DEVICE_UNAVAILABLE` from a read | Device configured but the bus would not open |

## Multiple devices on one bus

One `adc.yml`, one worker. Each SPI device is a separate entry with its own
`port` (bus) and `device` (chip select), so several converters can share a bus:

```yaml
devices:
  adc0:
    type: MCP3208
    port: 0
    device: 0
    v_ref: 3.3
  adc1:
    type: MCP3204
    port: 0
    device: 1
    v_ref: 3.3
```

`port` is the bus number and `device` is the chip select, so `adc0` resolves to
`/dev/spidev0.0` and `adc1` to `/dev/spidev0.1`. Each distinct node must also be
passed through with `--device` or `AddDevice=`.
See [Configuration](configuration.md) for the full field list.

## Health and shutdown

The worker exposes no HTTP server and no health endpoint. Liveness is the
telemetry key:

| Property | Value |
| --- | --- |
| Key | `scietex:AdcService:<instance_id>:telemetry` |
| Encoding | msgpack |
| Interval | 2.0 s |
| TTL | 6 s |

A missing or expired key means the worker is gone; a key with a device reporting
`available: false` means only that device is gone. A container healthcheck should
probe the key through `valkey-cli`, not an HTTP port.

Shutdown is framework-driven: `SIGTERM` stops the managers, closes every open
device, and releases the Valkey connection. The Quadlet sets
`TimeoutStopSec=30`, which is the budget the framework has to finish.

## See also

- [Configuration](configuration.md) — `adc/adc.yml` and the settings layers
- [Remote configuration](remote-config.md) — live applies over Valkey
- [Index](index.md)
