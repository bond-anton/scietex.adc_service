"""Entry point to run the ADC worker as a foreground daemon."""

import argparse
import asyncio
import os

from scietex.service import ValkeyWorkerConfig

from .adc_worker import AdcWorker
from .config import ADC_CONFIG_SUBDIR
from .version import __version__

#: Environment fallbacks for the CLI options, so a container can be configured
#: with ``-e`` without overriding its command. An explicit CLI flag still wins.
ENV_SERVICE_NAME: str = "SCIETEX_SERVICE_NAME"
ENV_LOGGING_LEVEL: str = "SCIETEX_LOGGING_LEVEL"

DEFAULT_SERVICE_NAME: str = "AdcService"
DEFAULT_LOGGING_LEVEL: str = "INFO"


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with environment-variable fallbacks.

    ``--conf-dir`` defaults to ``None`` so the framework's ``prepare_conf_dir``
    resolves it (it already honors ``SCIETEX_CONFIG_DIR``); the other options
    fall back to their environment variables when no flag is given.
    """
    parser = argparse.ArgumentParser(description="Run the ADC worker.")
    parser.add_argument("--conf-dir", default=None, help="Configuration directory (default: auto-resolved)")
    parser.add_argument(
        "--service-name",
        default=os.environ.get(ENV_SERVICE_NAME, DEFAULT_SERVICE_NAME),
        help=f"Service name (default: ${ENV_SERVICE_NAME} or {DEFAULT_SERVICE_NAME})",
    )
    parser.add_argument(
        "--logging-level",
        default=os.environ.get(ENV_LOGGING_LEVEL, DEFAULT_LOGGING_LEVEL),
        help=f"Logging level (default: ${ENV_LOGGING_LEVEL} or {DEFAULT_LOGGING_LEVEL})",
    )
    return parser


def _build_config(args: argparse.Namespace) -> ValkeyWorkerConfig:
    """Build the worker config from parsed CLI arguments."""
    return ValkeyWorkerConfig(
        service_name=args.service_name,
        version=__version__,
        conf_dir=args.conf_dir,
        logging_level=args.logging_level,
        remote_config_enabled=True,
        valkey_config=None,
        # Namespace the framework snapshot under the same subdir as adc.yml,
        # so services sharing one config dir cannot clobber each other's config.
        config_file=f"{ADC_CONFIG_SUBDIR}/config.yml",
    )


def main() -> None:
    """Parse CLI arguments and run the ADC worker until exit is requested."""
    args = _build_parser().parse_args()
    config = _build_config(args)

    async def run() -> None:
        worker = AdcWorker(config)
        await worker.start()
        await worker.events["exit"].wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
