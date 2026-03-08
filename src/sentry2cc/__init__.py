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
    """
    Configure loguru as the sole logging backend.

    - Removes loguru's default handler and adds a clean stderr sink with a
      format suited for CLI output.
    - Intercepts stdlib logging (used by httpx, httpcore, and any other
      third-party libs) and routes it through loguru so all output is uniform.
    - Suppresses noisy third-party loggers at WARNING level.
    """
    from loguru import logger

    # Remove the default loguru handler (prints to stderr with its own format)
    logger.remove()

    # Add a single stderr sink with a readable format
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "[<level>{level: <8}</level>] "
            "<cyan>{name}</cyan>: "
            "{message}"
        ),
        colorize=True,
        backtrace=True,  # full traceback on exceptions
        diagnose=True,  # variable values in tracebacks (disable in prod if needed)
    )

    # Intercept stdlib logging → loguru so third-party libs (httpx, google-auth)
    # go through the same sink instead of printing raw to stderr.
    class _InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Map stdlib level to loguru level name
            try:
                lvl = logger.level(record.levelname).name
            except ValueError:
                lvl = record.levelno  # type: ignore[assignment]

            # Find the correct caller depth so loguru shows the right source location
            frame, depth = sys._getframe(6), 6
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back  # type: ignore[assignment]
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                lvl, record.getMessage()
            )

    # Replace the root stdlib handler with our interceptor
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
    from loguru import logger

    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.log_level)

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: {}", config_path.resolve())
        sys.exit(1)

    # Add the config file's directory to sys.path so that user-supplied
    # trigger/post_execution modules (e.g. "wovar_rules") can be imported.
    config_dir = str(config_path.resolve().parent)
    if config_dir not in sys.path:
        sys.path.insert(0, config_dir)
        logger.debug("Added {} to sys.path for user module imports", config_dir)

    # Defer heavy imports until after logging is configured
    from sentry2cc.config import load_config
    from sentry2cc.runner import run_poll_loop

    logger.info("Loading config from {}", config_path.resolve())
    try:
        config = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config: {}", exc)
        sys.exit(1)

    try:
        asyncio.run(run_poll_loop(config, run_once=args.once))
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception:
        logger.exception("sentry2cc encountered a fatal error")
        sys.exit(1)


__all__ = ["main"]
