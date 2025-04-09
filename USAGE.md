pli# Arch Linux AI Storage Agent - Usage Guide

This document explains how to install, configure, and use the Arch Linux AI Storage Agent CLI application.

## 1. Installation

There are two main ways to install and run the application:

**Method 1: Running Directly from Source (for development/testing)**

1.  **Clone the Repository:**
    ```bash
    git clone <repository_url> # Replace with actual URL if applicable
    cd arch-cleaner
    ```
2.  **Install Dependencies:**
    Make sure you have Python 3 (preferably 3.8+) and `pip` installed. Then, install the required Python packages:
    ```bash
    python -m pip install -r requirements.txt
    ```
3.  **Run:** Execute the application using `python main.py <command>`.

**Method 2: Installing as a Command-Line Tool (Recommended for regular use)**

1.  **Clone the Repository:** (If not already done)
    ```bash
    git clone <repository_url> # Replace with actual URL if applicable
    cd arch-cleaner
    ```
2.  **Install the Package:** This uses the `setup.py` file to install the application and its dependencies, making the `arch-cleaner` command available system-wide (for your user). The `-e` flag installs it in "editable" mode, meaning code changes are reflected without reinstalling.
    ```bash
    python -m pip install -e .
    ```
3.  **Run:** You can now run the application from any directory using the `arch-cleaner` command:
    ```bash
    arch-cleaner scan
    arch-cleaner suggest
    arch-cleaner apply --dry-run
    # etc.
    ```

**Optional System Dependencies (for both methods):**
    *   For safe file deletion using the system trash (recommended):
        ```bash
        sudo pacman -S trash-cli
        ```
    *   For duplicate file detection (if enabled in config):
        ```bash
        sudo pacman -S fdupes # Or configure the agent to use internal hashing
        ```

## 2. Configuration

1.  **Configuration File Location:**
    The agent looks for its configuration file at:
    `~/.config/arch-cleaner/config.toml`

2.  **Create Initial Configuration:**
    If the configuration file doesn't exist, you can copy the example file:
    ```bash
    mkdir -p ~/.config/arch-cleaner
    cp config.toml.example ~/.config/arch-cleaner/config.toml
    ```
    Alternatively, the `config --edit` command might offer to create it for you.

3.  **Edit Configuration:**
    Open `~/.config/arch-cleaner/config.toml` in your favorite text editor to customize settings:
    *   `scan_paths`: Directories to scan.
    *   `exclude_patterns`: Files/directories to ignore.
    *   `thresholds`: Define "old" and "large" files.
    *   `arch`: Toggle Arch-specific cleanups (pacman cache, orphans, journal).
    *   `duplicates`: Enable/disable duplicate detection.
    *   `safety`: Configure `use_trash` and `default_dry_run`.
    *   `automation`: Configure automated runs (schedule, thresholds).
    *   `learning`: Enable/disable adaptive learning.

    You can also view or edit the configuration using the `config` command:
    ```bash
    python main.py config --list   # View current settings
    python main.py config --edit   # Open config in $EDITOR
    python main.py config safety.use_trash # View a specific key
    ```
    *(Setting keys via the CLI (`config <key> <value>`) is not yet implemented).*

## 3. Basic Workflow

The typical workflow involves three main steps:

1.  **Scan:** Collect data about your system based on the configuration.
    ```bash
    python main.py scan
    ```
    Use `python main.py scan --force` to ignore any potential caching and force a full rescan.

2.  **Suggest:** Analyze the collected data and display cleanup recommendations.
    ```bash
    python main.py suggest
    ```
    *   Use `-n <number>` (e.g., `python main.py suggest -n 5`) to limit the number of suggestions shown.
    *   Use `--json` to get output in JSON format for scripting.

3.  **Apply:** Execute the suggested cleanup actions.
    *   **Dry Run (Recommended First):** See what actions *would* be taken without making changes.
        ```bash
        python main.py apply --dry-run
        ```
    *   **Apply Specific Suggestions:** Apply only certain suggestions by providing their IDs (shown in the `suggest` output).
        ```bash
        python main.py apply <ID1> <ID2> ...
        # Example: python main.py apply a1b2c3d4e5 f6g7h8i9j0
        ```
    *   **Apply All Last Suggestions (Interactive):** Apply all suggestions shown in the last `suggest` command, with a confirmation prompt.
        ```bash
        python main.py apply
        ```
    *   **Apply All Last Suggestions (Force):** Apply all suggestions without confirmation (use with caution!).
        ```bash
        python main.py apply --yes
        ```

## 4. Commands Overview

*   `python main.py --help`: Show the main help message.
*   `python main.py <command> --help`: Show help for a specific command.

*   **`scan [-f, --force]`**:
    Collects storage data. `--force` ignores potential caching (if implemented).

*   **`suggest [-n NUM] [--json]`**:
    Analyzes data and shows recommendations. `-n` limits output count, `--json` outputs JSON.

*   **`apply [--dry-run] [-y, --yes] [suggestion_ids ...]`**:
    Applies suggestions.
    *   `--dry-run`: Simulate actions.
    *   `--yes`: Skip confirmation prompt when applying all suggestions.
    *   `[suggestion_ids ...]`: Optional list of specific suggestion IDs to apply. If omitted, applies all from the last `suggest` run.

*   **`auto [--dry-run]`**:
    Runs the full `scan -> suggest -> apply` cycle automatically based on configuration rules (e.g., confidence threshold). Requires confirmation unless run non-interactively.

*   **`config [--list] [--edit] [key] [value]`**:
    Manage configuration.
    *   `--list`: Show all settings.
    *   `--edit`: Open config file in `$EDITOR`.
    *   `[key]`: View a specific setting.
    *   `[key] [value]`: *Not yet implemented.*

*   **`report`**:
    Shows a summary report (e.g., recent actions).

*   **`status`**:
    Shows agent status (last scan time, DB size, etc.).

## 5. Optional: Background Scanning (using systemd)

If you have installed the application using Method 2 (pip install), you can set up automatic background scanning using systemd user services.

1.  **Create Service File:** Create a file named `~/.config/systemd/user/arch-cleaner-scan.service` with the following content:
    ```ini
    [Unit]
    Description=Run Arch Cleaner Scan

    [Service]
    Type=oneshot
    ExecStart=/usr/bin/env arch-cleaner scan
    # Optional: Add --force if needed:
    # ExecStart=/usr/bin/env arch-cleaner scan --force

    [Install]
    WantedBy=default.target
    ```

2.  **Create Timer File:** Create a file named `~/.config/systemd/user/arch-cleaner-scan.timer` to schedule the scan (this example runs daily):
    ```ini
    [Unit]
    Description=Run Arch Cleaner Scan Daily
    Requires=arch-cleaner-scan.service

    [Timer]
    OnBootSec=15min
    OnCalendar=daily
    Persistent=true

    [Install]
    WantedBy=timers.target
    ```
    *(Adjust `OnCalendar` or other Timer settings as desired. See `man systemd.timer`.)*

3.  **Enable and Start Timer:** Run the following commands in your terminal:
    ```bash
    systemctl --user daemon-reload
    systemctl --user enable --now arch-cleaner-scan.timer
    ```

4.  **Check Status (Optional):** You can check the timer's status and last run time:
    ```bash
    systemctl --user status arch-cleaner-scan.timer
    journalctl --user -u arch-cleaner-scan.service # View logs from the service
    ```

## 6. Important Notes

*   **Permissions (`sudo`):** Some actions require root privileges (e.g., removing orphan packages, cleaning pacman cache, vacuuming journal logs). The application (whether run directly or via the installed command) will invoke `sudo` for these commands, and you may be prompted for your password.
*   **Trash:** Using the system trash (`safety.use_trash = true` in config and `trash-cli` installed) is highly recommended for safety.
*   **Database:** The agent stores data in `~/.local/share/arch-cleaner/data.db`.
*   **Learning:** The learning module currently only records feedback.
