> **Note:** This project is currently under development. Not all features may be fully functional. This project was made for fun and self utility.

# Arch Cleaner

A simple tool to help clean up your Arch Linux system.

## Setup

1.  **Make sure you have Python installed.** You can usually install it through your system's package manager.
2.  **Download the project files.** If you have `git` installed, you can clone the repository. Otherwise, download the source code as a ZIP file and extract it.
3.  **Open your terminal** and navigate to the project directory (the folder containing this README file).
4.  **Install the required libraries:**
    ```bash
    pip install -r requirements.txt
    ```
5.  **Install the application:**
    ```bash
    python setup.py install --user
    ```
    *(Using `--user` installs it just for your user account, which is often simpler.)*

## How to Use

After installation, you should be able to run the cleaner from your terminal:

```bash
arch-cleaner --help
```

This command will show you the available options and how to use them.

For a basic scan and clean, you might run a command similar to this (refer to the `--help` output for exact commands):

```bash
arch-cleaner scan
arch-cleaner clean
```

**Note:** Always review what the tool suggests removing before confirming any cleaning actions.
