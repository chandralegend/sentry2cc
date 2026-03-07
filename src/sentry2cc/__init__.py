"""
sentry2cc — Automatically fix Sentry errors with Claude Code.

CLI usage:
    sentry2cc --config sentry2cc.yaml
    sentry2cc --config sentry2cc.yaml --once
    sentry2cc --config sentry2cc.yaml --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def _configure_logging(level: str) -> None:
    """Set up structured logging for the application."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    # Silence overly verbose third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentry2cc",
        description=(
            "sentry2cc — poll Sentry for errors and automatically fix them "
            "with Claude Code Agent."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Run the poll loop continuously
  sentry2cc --config sentry2cc.yaml

  # Poll once then exit (useful for cron / CI)
  sentry2cc --config sentry2cc.yaml --once

  # Enable verbose logging
  sentry2cc --config sentry2cc.yaml --log-level DEBUG
""",
    )

    parser.add_argument(
        "--config",
        "-c",
        metavar="PATH",
        default="sentry2cc.yaml",
        help="Path to the YAML configuration file (default: sentry2cc.yaml)",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Poll exactly once and then exit (instead of looping continuously)",
    )

    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="sentry2cc 0.1.0",
    )

    return parser


def main() -> None:
    """CLI entry point for sentry2cc."""
    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.log_level)
    logger = logging.getLogger("sentry2cc")

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path.resolve())
        sys.exit(1)

    # Defer heavy imports until after logging is configured
    from sentry2cc.config import load_config
    from sentry2cc.runner import run_poll_loop

    logger.info("Loading config from %s", config_path.resolve())
    try:
        config = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    try:
        asyncio.run(run_poll_loop(config, run_once=args.once))
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception:
        logger.exception("sentry2cc encountered a fatal error")
        sys.exit(1)


__all__ = ["main"]
