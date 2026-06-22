"""CLI entry points for straderv3.

Commands:
    python -m strader3 run      Start the trading bot (live or paper mode)
    python -m strader3 backtest  Run backtest on historical data
    python -m strader3 paper     Start in paper-trade mode (explicit)
"""

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

# Ensure src is on the path when running as module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strader3.utils.config import get_nested, load_config
from strader3.utils.logging import setup_logging


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="straderv3",
        description="straderv3 — Retail Algorithmic Trading System",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override SQLite database path",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run
    run_parser = subparsers.add_parser("run", help="Start the trading bot")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Start without feed (verify wiring only)",
    )

    # backtest
    backtest_parser = subparsers.add_parser("backtest", help="Run backtest")
    backtest_parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Backtest start date (ISO format)",
    )
    backtest_parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Backtest end date (ISO format)",
    )

    # paper
    subparsers.add_parser("paper", help="Start in paper-trade mode")

    return parser.parse_args()


async def _cmd_run(args: argparse.Namespace) -> None:
    """Execute the 'run' command."""
    config = load_config(args.config)

    # Override db path if specified
    if args.db:
        if "database" not in config:
            config["database"] = {}
        config["database"]["path"] = args.db

    # Setup logging
    log_level = "DEBUG" if args.verbose else get_nested(config, "logging.level", "INFO")
    log_dir = "data/logs"
    setup_logging(level=log_level, log_dir=log_dir)

    from strader3.core.trading_engine import TradingEngine

    engine = TradingEngine(config=config)

    try:
        await engine.run()
    except KeyboardInterrupt:
        pass
    finally:
        await engine.shutdown()


async def _cmd_backtest(args: argparse.Namespace) -> None:
    """Execute the 'backtest' command."""
    config = load_config(args.config)

    log_level = "DEBUG" if args.verbose else get_nested(config, "logging.level", "INFO")
    setup_logging(level=log_level, log_dir="data/logs")

    logger = structlog.get_logger("straderv3.backtest")
    logger.info("backtest.starting", start=args.start, end=args.end)

    # Placeholder: full backtesting implementation in T3d
    logger.info("backtest.placeholder", message="Backtesting engine coming in T3d")

    print("Backtest command scaffolded. Full implementation in T3d.")
    print(f"  Config: {args.config}")
    print(f"  Start: {args.start or 'N/A'}")
    print(f"  End: {args.end or 'N/A'}")


async def _cmd_paper(args: argparse.Namespace) -> None:
    """Execute the 'paper' command."""
    config = load_config(args.config)
    config["trade_mode"] = "paper"

    if args.db:
        if "database" not in config:
            config["database"] = {}
        config["database"]["path"] = args.db

    log_level = "DEBUG" if args.verbose else get_nested(config, "logging.level", "INFO")
    setup_logging(level=log_level, log_dir="data/logs")

    from strader3.core.trading_engine import TradingEngine

    engine = TradingEngine(config=config)

    try:
        await engine.run()
    except KeyboardInterrupt:
        pass
    finally:
        await engine.shutdown()


def main() -> None:
    """Main entry point."""
    args = _parse_args()

    if args.command is None:
        print("Usage: python -m strader3 <command> [options]")
        print("Commands: run, backtest, paper")
        sys.exit(1)

    if args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "backtest":
        asyncio.run(_cmd_backtest(args))
    elif args.command == "paper":
        asyncio.run(_cmd_paper(args))
    else:
        print(f"Unknown command: {args.command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
