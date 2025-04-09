#!/usr/bin/env python3

import argparse
import sys
import os
import sys
import logging
from pathlib import Path

# Project imports
from arch_cleaner.core.controller import CoreController
from arch_cleaner.ui.cli import handle_cli_command
from arch_cleaner.modules.config_manager import ConfigManager
from arch_cleaner.db.database import DatabaseManager

# Define default paths based on XDG Base Directory Specification
XDG_CONFIG_HOME = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
XDG_DATA_HOME = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))

APP_NAME = "arch-cleaner"
DEFAULT_CONFIG_PATH = XDG_CONFIG_HOME / APP_NAME / "config.toml"
DEFAULT_DB_PATH = XDG_DATA_HOME / APP_NAME / "data.db"
DEFAULT_EXAMPLE_CONFIG_PATH = Path(__file__).parent / "config.toml.example"

# --- Logging Setup ---
# Basic configuration, can be enhanced (e.g., file logging, rotation)
log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
# Simple format for console output
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# Log to stderr
stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setFormatter(log_formatter)

# Configure root logger
logging.basicConfig(level=log_level, handlers=[stream_handler])
# Silence noisy libraries if needed
# logging.getLogger("some_library").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def setup_environment():
    """Ensure necessary directories exist and inform user."""
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Copy example config if main config doesn't exist
    # This should ideally be handled by packaging/installation
    # if not DEFAULT_CONFIG_PATH.exists() and DEFAULT_EXAMPLE_CONFIG_PATH.exists():
    #     try:
    #         import shutil
    #         shutil.copy(DEFAULT_EXAMPLE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
    #         print(f"Default configuration copied to {DEFAULT_CONFIG_PATH}")
    #     except Exception as e:
    #         print(f"Warning: Could not copy default configuration: {e}", file=sys.stderr)


def main():
    """Main entry point for the Arch Linux AI Storage Agent."""
    setup_environment()

    parser = argparse.ArgumentParser(
        description="Arch Linux AI Storage Agent: Manage and optimize system storage.",
        epilog="Run '<command> --help' for more information on a specific command."
    )
    subparsers = parser.add_subparsers(dest="command", title="Available Commands", required=True)

    # --- Scan Command ---
    parser_scan = subparsers.add_parser("scan", help="Collect data and analyze storage.")
    parser_scan.add_argument("-f", "--force", action="store_true", help="Force re-scan even if data seems fresh.")
    parser_scan.add_argument("-d", "--directory", type=str, help="Scan a specific directory instead of default locations.")
    # Add other scan-specific options if needed

    # --- Suggest Command ---
    parser_suggest = subparsers.add_parser("suggest", help="Show cleanup recommendations based on the last scan.")
    parser_suggest.add_argument("-n", "--num-suggestions", type=int, default=20, help="Maximum number of suggestions to display.")
    parser_suggest.add_argument("--json", action="store_true", help="Output suggestions in JSON format.")
    # Add filtering/sorting options

    # --- Apply Command ---
    parser_apply = subparsers.add_parser("apply", help="Apply selected or all recommendations.")
    parser_apply.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes.")
    parser_apply.add_argument("-y", "--yes", action="store_true", help="Automatically approve all suggestions (use with caution!).")
    parser_apply.add_argument('suggestion_ids', nargs='*', help='Optional list of suggestion IDs to apply.') # Accept multiple IDs

    # --- Auto Command ---
    parser_auto = subparsers.add_parser("auto", help="Run scan and apply based on configured automation rules.")
    parser_auto.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes.")
    # Add confidence level threshold?

    # --- Config Command ---
    parser_config = subparsers.add_parser("config", help="View or modify configuration settings.")
    parser_config.add_argument("key", nargs='?', help="The configuration key to view or set.")
    parser_config.add_argument("value", nargs='?', help="The value to set for the configuration key.")
    parser_config.add_argument("--list", action="store_true", help="List all configuration settings.")
    parser_config.add_argument("--edit", action="store_true", help="Open the configuration file in $EDITOR.")

    # --- Report Command ---
    parser_report = subparsers.add_parser("report", help="Show summary of last actions or storage savings.")
    # Add options for time range, etc.

    # --- Status Command ---
    parser_status = subparsers.add_parser("status", help="Show agent status, database size, etc.")

    args = parser.parse_args()

    # --- Initialize Core Components ---
    logger.info(f"Using configuration file: {DEFAULT_CONFIG_PATH}")
    logger.info(f"Using database file: {DEFAULT_DB_PATH}")

    db_manager = None # Ensure it's defined in outer scope for finally block
    try:
        config_manager = ConfigManager(DEFAULT_CONFIG_PATH)
        db_manager = DatabaseManager(DEFAULT_DB_PATH)
        controller = CoreController(config_manager, db_manager)

        logger.debug(f"Executing command: {args.command} with args: {vars(args)}")

        # --- Handle Commands via CLI module ---
        handle_cli_command(args, controller, config_manager)

        logger.info("Operation completed successfully.")
        sys.exit(0)

    except Exception as e:
        # Catch-all for major initialization errors or unhandled exceptions
        logger.critical(f"A critical error occurred: {e}", exc_info=True)
        # Use Rich console for error output if available
        try:
            from rich.console import Console
            console = Console(stderr=True)
            console.print("\n[bold red]A critical error occurred:[/bold red]")
            console.print_exception(show_locals=False)
        except ImportError:
            print(f"\nA critical error occurred: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
        sys.exit(1)
    finally:
        # Ensure database connection is closed
        if db_manager:
            db_manager.close()


if __name__ == "__main__":
    main()
