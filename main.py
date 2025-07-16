#!/usr/bin/env python3

import argparse
import sys
import os
import sys
import logging
from pathlib import Path

from arch_cleaner.core.controller import CoreController
from arch_cleaner.ui.cli import handle_cli_command
from arch_cleaner.modules.config_manager import ConfigManager
from arch_cleaner.db.database import DatabaseManager

XDG_CONFIG_HOME = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
XDG_DATA_HOME = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))

APP_NAME = "arch-cleaner"
DEFAULT_CONFIG_PATH = XDG_CONFIG_HOME / APP_NAME / "config.toml"
DEFAULT_DB_PATH = XDG_DATA_HOME / APP_NAME / "data.db"

log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setFormatter(log_formatter)
logging.basicConfig(level=log_level, handlers=[stream_handler])
logger = logging.getLogger(__name__)


def setup_environment():
    """Ensure necessary directories exist."""
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def main():
    """Main entry point for the Arch Linux AI Storage Agent."""
    setup_environment()

    parser = argparse.ArgumentParser(description="An AI-powered storage cleaner for Arch Linux.")
    subparsers = parser.add_subparsers(dest="command", title="Available Commands", required=True)

    parser_scan = subparsers.add_parser("scan", help="Scan for potential cleanup opportunities.")
    parser_scan.add_argument("-f", "--force", action="store_true", help="Force a re-scan.")
    parser_scan.add_argument("-d", "--directory", type=str, help="Scan a specific directory.")

    parser_suggest = subparsers.add_parser("suggest", help="Generate cleanup suggestions.")
    parser_suggest.add_argument("-n", "--num-suggestions", type=int, default=20, help="Number of suggestions to display.")
    parser_suggest.add_argument("--json", action="store_true", help="Output in JSON format.")

    parser_apply = subparsers.add_parser("apply", help="Apply suggestions.")
    parser_apply.add_argument("--dry-run", action="store_true", help="Simulate actions without making changes.")
    parser_apply.add_argument("-y", "--yes", action="store_true", help="Auto-approve all suggestions.")
    parser_apply.add_argument('suggestion_ids', nargs='*', help='Specific suggestion IDs to apply.')

    parser_auto = subparsers.add_parser("auto", help="Run the full scan-suggest-apply cycle automatically.")
    parser_auto.add_argument("--dry-run", action="store_true", help="Simulate auto mode.")

    parser_config = subparsers.add_parser("config", help="Manage configuration.")
    parser_config.add_argument("key", nargs='?', help="The configuration key to manage.")
    parser_config.add_argument("--list", action="store_true", help="List all settings.")
    parser_config.add_argument("--edit", action="store_true", help="Edit the configuration file.")

    subparsers.add_parser("report", help="Generate a report of past actions.")
    subparsers.add_parser("status", help="Show the current status of the agent.")

    args = parser.parse_args()

    db_manager = None
    try:
        config_manager = ConfigManager(DEFAULT_CONFIG_PATH)
        db_manager = DatabaseManager(DEFAULT_DB_PATH)
        controller = CoreController(config_manager, db_manager)

        handle_cli_command(args, controller, config_manager)
        sys.exit(0)
    except Exception as e:
        logger.critical(f"A critical error occurred: {e}", exc_info=True)
        try:
            from rich.console import Console
            console = Console(stderr=True)
            console.print("\n[bold red]A critical error occurred:[/bold red]")
            console.print_exception(show_locals=False)
        except ImportError:
            print(f"\nA critical error occurred: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if db_manager:
            db_manager.close()


if __name__ == "__main__":
    main()
