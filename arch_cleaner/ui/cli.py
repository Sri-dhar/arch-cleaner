import argparse
import logging
import sys
import os
import subprocess
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
from rich.prompt import Confirm

from ..core.controller import CoreController
from ..core.models import Suggestion, ActionResult
from ..modules.config_manager import ConfigManager
from ..utils.helpers import human_readable_size

logger = logging.getLogger(__name__)
console = Console()


def display_suggestions(suggestions: List[Suggestion], num_to_show: int):
    """Displays suggestions in a formatted table."""
    if not suggestions:
        console.print("[yellow]No suggestions generated.[/yellow]")
        return

    table = Table(title="Cleanup Suggestions", show_header=True, header_style="bold magenta", expand=True)
    table.add_column("ID", style="dim", width=12)
    table.add_column("Type", style="cyan", width=15)
    table.add_column("Description", style="green", no_wrap=False, ratio=1)
    table.add_column("Path / Details", style="dim", no_wrap=False, ratio=1)
    table.add_column("Size", style="yellow", justify="right", width=10)
    table.add_column("Confidence", style="blue", justify="right", width=10)

    total_potential_saving = 0
    for i, sugg in enumerate(suggestions):
        if i >= num_to_show:
            break
        path_details = sugg.details or "[dim]N/A[/dim]"
        table.add_row(
            sugg.id,
            sugg.suggestion_type,
            sugg.description,
            path_details,
            human_readable_size(sugg.estimated_size_bytes),
            f"{sugg.confidence:.2f}"
        )
        total_potential_saving += sugg.estimated_size_bytes

    console.print(table)
    if len(suggestions) > num_to_show:
        console.print(f"... and {len(suggestions) - num_to_show} more suggestions.")
    console.print(f"\nTotal potential savings from top {num_to_show} suggestions: [bold yellow]{human_readable_size(total_potential_saving)}[/bold yellow]")
    console.print("Run 'apply' to take action, or 'apply <ID1> <ID2>...' to apply specific suggestions.")


def display_results(results: List[ActionResult]):
    """Displays the results of applied actions."""
    if not results:
        console.print("[yellow]No actions were performed.[/yellow]")
        return

    table = Table(title="Action Results", show_header=True, header_style="bold magenta")
    table.add_column("ID", style="dim", width=12)
    table.add_column("Type", style="cyan")
    table.add_column("Outcome", style="bold")
    table.add_column("Details", style="green")
    table.add_column("Freed", style="yellow", justify="right")

    total_freed = 0
    success_count = 0
    fail_count = 0

    for res in results:
        outcome = "[green]Success[/green]" if res.success else "[red]Failed[/red]"
        if res.dry_run:
            outcome = "[blue]Dry Run[/blue]"
        table.add_row(
            res.suggestion.id,
            res.suggestion.suggestion_type,
            outcome,
            res.message,
            human_readable_size(res.bytes_freed) if res.success else "N/A"
        )
        if res.success and not res.dry_run:
            total_freed += res.bytes_freed
            success_count += 1
        elif not res.success:
            fail_count += 1

    console.print(table)
    if not results[0].dry_run:
        console.print(f"\nSummary: {success_count} succeeded, {fail_count} failed.")
        console.print(f"Total space freed: [bold yellow]{human_readable_size(total_freed)}[/bold yellow]")
    else:
        console.print("\nSummary: Dry run completed. No changes were made.")


def display_status(status: Dict[str, Any]):
    """Displays agent status information."""
    if status.get('error'):
        console.print(f"[bold red]Error retrieving status:[/bold red] {status['error']}")
        return

    table = Table(title="Agent Status", show_header=False, box=None)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")

    last_scan = status.get('last_scan_time')
    table.add_row("Last Scan Time", time.ctime(last_scan) if last_scan else "Never")
    db_size = status.get('database_size_bytes', -1)
    table.add_row("Database Size", human_readable_size(db_size) if db_size >= 0 else "N/A")
    table.add_row("Config Path", status.get('config_path', 'N/A'))
    table.add_row("Database Path", status.get('database_path', 'N/A'))

    console.print(table)

def display_report(report: Dict[str, Any]):
     """Displays a report (e.g., recent actions)."""
     if report.get('error'):
        console.print(f"[bold red]Error generating report:[/bold red] {report['error']}")
        return

     console.print("[bold magenta]Recent Actions:[/bold magenta]")
     actions = report.get('recent_actions', [])
     if not actions:
         console.print("  No recent actions found.")
         return

     table = Table(show_header=True, header_style="bold blue")
     table.add_column("Timestamp", style="dim")
     table.add_column("Suggestion ID", style="dim")
     table.add_column("Type")
     table.add_column("Action")
     table.add_column("Details")
     table.add_column("Comment")

     for action in actions:
         ts = time.ctime(action.timestamp) if action.timestamp else "N/A"
         table.add_row(
             ts,
             action.suggestion_id,
             action.suggestion_type,
             action.action_taken,
             action.item_details,
             action.user_comment or ""
         )
     console.print(table)
     # console.print(f"\nTotal Saved Estimate: {report.get('total_saved_estimate', 'N/A')}")


# --- Command Handlers ---

def handle_scan(args: argparse.Namespace, controller: CoreController):
    """Handles the 'scan' command."""
    scan_target = args.directory or "default locations"
    console.print(f"Starting storage scan for {scan_target}...")
    with Progress(SpinnerColumn(), "[progress.description]{task.description}", TimeElapsedColumn(), console=console, transient=True) as progress:
        progress.add_task(f"Scanning {scan_target}...", total=None)
        controller.scan(force=args.force, directory=args.directory)
    console.print("[green]Scan complete.[/green]")
    console.print("Run 'suggest' to see recommendations.")


def handle_suggest(args: argparse.Namespace, controller: CoreController):
    """Handles the 'suggest' command."""
    console.print("Generating suggestions...")
    suggestions = controller.suggest(limit=None)
    if args.json:
        import json
        def serialize_suggestion(s):
            data = s.__dict__.copy()
            if isinstance(data.get('data'), (ScannedItem, PackageInfo, DuplicateSet)):
                data['data'] = data['data'].__dict__.copy()
                if 'path' in data['data'] and isinstance(data['data']['path'], Path):
                    data['data']['path'] = str(data['data']['path'])
                if 'paths' in data['data'] and isinstance(data['data']['paths'], list):
                    data['data']['paths'] = [str(p) for p in data['data']['paths']]
            elif isinstance(data.get('data'), list) and data['data'] and isinstance(data['data'][0], PackageInfo):
                data['data'] = [pkg.__dict__ for pkg in data['data']]
            return data
        output_data = [serialize_suggestion(s) for s in suggestions[:args.num_suggestions]]
        console.print(json.dumps(output_data, indent=2))
    else:
        display_suggestions(suggestions, args.num_suggestions)


def handle_apply(args: argparse.Namespace, controller: CoreController):
    """Handles the 'apply' command, including interactive selection."""
    suggestions_to_consider = controller.get_last_suggestions()
    if not suggestions_to_consider:
        console.print("[yellow]No suggestions available. Run 'suggest' first.[/yellow]")
        return

    suggestions_to_apply = suggestions_to_consider
    if args.suggestion_ids:
        suggestions_map = {s.id: s for s in suggestions_to_consider}
        suggestions_to_apply = [suggestions_map[sid] for sid in args.suggestion_ids if sid in suggestions_map]
        missing_ids = set(args.suggestion_ids) - set(suggestions_map.keys())
        if missing_ids:
            console.print(f"[yellow]Warning: Could not find suggestion IDs:[/yellow] {', '.join(missing_ids)}")

    if not suggestions_to_apply:
        console.print("[yellow]No matching suggestions to apply.[/yellow]")
        return

    console.print("\n[bold]Selected Suggestions for Apply:[/bold]")
    display_suggestions(suggestions_to_apply, num_to_show=len(suggestions_to_apply))

    if args.dry_run:
        console.print("\n[bold blue]--- DRY RUN MODE ---[/bold blue]")
        results = controller.apply(suggestion_ids=[s.id for s in suggestions_to_apply], dry_run=True, auto_approve=True)
        display_results(results)
        return

    if not args.yes and not Confirm.ask(f"\nApply these {len(suggestions_to_apply)} suggestions?", default=False):
        console.print("Apply cancelled.")
        return

    console.print("\nApplying suggestions...")
    with Progress(console=console) as progress:
        task = progress.add_task("Applying...", total=len(suggestions_to_apply))
        results = controller.apply(suggestion_ids=[s.id for s in suggestions_to_apply], dry_run=False, auto_approve=True)
        progress.update(task, completed=len(suggestions_to_apply))

    console.print("[green]Apply process finished.[/green]")
    display_results(results)


def handle_auto(args: argparse.Namespace, controller: CoreController):
    """Handles the 'auto' command."""
    if args.dry_run:
        console.print("[bold blue]--- AUTO MODE (DRY RUN) ---[/bold blue]")
    else:
        console.print("[bold yellow]--- AUTO MODE ---[/bold yellow]")
        if not Confirm.ask("Run automated cleanup? This will apply suggestions without further prompts.", default=False):
            console.print("Auto mode cancelled.")
            return

    console.print("Running scan, suggest, and apply based on configuration...")
    results = controller.run_auto(dry_run=args.dry_run)
    display_results(results)


def handle_config(args: argparse.Namespace, config_manager: ConfigManager):
    """Handles the 'config' command."""
    if args.list:
        import json
        console.print("[bold magenta]Current Configuration:[/bold magenta]")
        console.print_json(data=config_manager.config)
    elif args.edit:
        editor = os.environ.get('EDITOR', 'vim')
        config_path = config_manager.config_path
        if not config_path.exists():
            if Confirm.ask(f"Create a default config file at {config_path}?", default=True):
                try:
                    example_path = Path(__file__).parent.parent.parent / 'config.toml.example'
                    if example_path.exists():
                        shutil.copy(example_path, config_path)
                        console.print(f"[green]Copied example config to {config_path}[/green]")
                    else:
                        config_path.write_text("# Arch Cleaner Config\n")
                        console.print(f"[green]Created empty config file at {config_path}[/green]")
                except Exception as e:
                    console.print(f"[red]Error creating config file: {e}[/red]")
                    return
            else:
                return
        try:
            console.print(f"Opening config file '{config_path}' in '{editor}'...")
            subprocess.run([editor, str(config_path)], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            console.print(f"[red]Error with editor: {e}[/red]")
    elif args.key:
        value = config_manager.get(args.key)
        console.print(f"{args.key}: {value}" if value is not None else f"Key '{args.key}' not found.")
    else:
        console.print("[yellow]Usage: config [--list] [--edit] [key][/yellow]")


def handle_report(args: argparse.Namespace, controller: CoreController):
    """Handles the 'report' command."""
    console.print("Generating report...")
    report_data = controller.generate_report()
    display_report(report_data)


def handle_status(args: argparse.Namespace, controller: CoreController):
    """Handles the 'status' command."""
    console.print("Checking agent status...")
    status_data = controller.get_status()
    display_status(status_data)


# --- Main CLI Handler ---

def handle_cli_command(args: argparse.Namespace, controller: CoreController, config_manager: ConfigManager):
    """Dispatches CLI commands to their respective handlers."""
    command_handlers = {
        "scan": handle_scan,
        "suggest": handle_suggest,
        "apply": handle_apply,
        "auto": handle_auto,
        "config": lambda args, _, cfg: handle_config(args, cfg),
        "report": handle_report,
        "status": handle_status,
    }
    command = args.command
    handler = command_handlers.get(command)

    try:
        if handler:
            if command == "config":
                handler(args, None, config_manager)
            else:
                if not hasattr(args, 'suggestion_ids'):
                    args.suggestion_ids = None
                handler(args, controller)
        else:
            console.print(f"[red]Unknown command: {command}[/red]")
            sys.exit(1)
    except Exception as e:
        logger.error(f"An error occurred executing command '{command}': {e}", exc_info=True)
        console.print(f"\n[bold red]An unexpected error occurred:[/bold red]")
        console.print_exception(show_locals=False)
        sys.exit(1)
