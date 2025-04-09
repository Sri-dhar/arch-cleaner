import re
import subprocess
import time
import logging
import fnmatch
from pathlib import Path
from typing import Optional, Tuple, List, Union

logger = logging.getLogger(__name__)

SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}

TIME_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 86400 * 7,
    # Approximate month/year - use carefully
    "month": 86400 * 30,
    "y": 86400 * 365,
}

def parse_size(size_str: str) -> Optional[int]:
    """
    Parses a human-readable size string (e.g., "500M", "2G", "1024") into bytes.
    Returns None if parsing fails.
    """
    size_str = str(size_str).strip().upper()
    match = re.match(r'^(\d+(\.\d+)?)\s*([BKMGT])?B?$', size_str)
    if not match:
        # Check if it's just a number (assume bytes)
        if size_str.isdigit():
            return int(size_str)
        logger.warning(f"Could not parse size string: '{size_str}'")
        return None

    value = float(match.group(1))
    unit = match.group(3)

    if unit:
        multiplier = SIZE_UNITS.get(unit, 1)
    else:
        # If no unit, assume bytes if it looks like an integer, otherwise fail
        if '.' not in match.group(1):
             multiplier = 1
        else:
             logger.warning(f"Could not parse size string without unit: '{size_str}'")
             return None


    return int(value * multiplier)

def parse_duration(duration_str: str) -> Optional[int]:
    """
    Parses a human-readable duration string (e.g., "3m", "2w", "1y") into seconds.
    Handles units: s, m, h, d, w, month, y.
    Returns None if parsing fails.
    """
    duration_str = str(duration_str).strip().lower()
    match = re.match(r'^(\d+(\.\d+)?)\s*([smhdw]|month|y)?$', duration_str)
    if not match:
        logger.warning(f"Could not parse duration string: '{duration_str}'")
        return None

    value = float(match.group(1))
    unit = match.group(3) if match.group(3) else 's' # Default to seconds if no unit

    multiplier = TIME_UNITS.get(unit)
    if multiplier is None:
        logger.warning(f"Unknown time unit '{unit}' in duration string: '{duration_str}'")
        return None

    return int(value * multiplier)

def get_age_seconds(timestamp: float) -> float:
    """Calculates the age of a timestamp in seconds."""
    return time.time() - timestamp

def run_command(command: List[str], capture_output: bool = True, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """
    Runs an external command safely using subprocess.run.

    Args:
        command: A list of command arguments (e.g., ['ls', '-l']).
        capture_output: If True, capture stdout and stderr.
        check: If True, raise CalledProcessError if the command returns non-zero exit code.
        **kwargs: Additional arguments to pass to subprocess.run.

    Returns:
        A subprocess.CompletedProcess object.
    """
    logger.debug(f"Running command: {' '.join(command)}")
    try:
        # Ensure text=True for readable output if capturing
        if capture_output and 'text' not in kwargs:
            kwargs['text'] = True
        if capture_output and 'capture_output' not in kwargs:
             kwargs['capture_output'] = True

        result = subprocess.run(command, check=check, **kwargs)
        if result.returncode != 0:
            logger.warning(f"Command '{' '.join(command)}' exited with code {result.returncode}")
            if capture_output and result.stderr:
                 logger.warning(f"Stderr: {result.stderr.strip()}")
        return result
    except FileNotFoundError:
        logger.error(f"Command not found: {command[0]}")
        # Create a dummy CompletedProcess to indicate failure
        return subprocess.CompletedProcess(command, -1, stdout="", stderr=f"Command not found: {command[0]}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Command '{' '.join(command)}' failed with error: {e}")
        if capture_output:
            logger.error(f"Stdout: {e.stdout}")
            logger.error(f"Stderr: {e.stderr}")
        raise # Re-raise if check=True was used
    except Exception as e:
        logger.error(f"An unexpected error occurred running command '{' '.join(command)}': {e}", exc_info=True)
        # Create a dummy CompletedProcess
        return subprocess.CompletedProcess(command, -1, stdout="", stderr=str(e))


def is_path_excluded(path: Path, exclude_patterns: List[str]) -> bool:
    """
    Checks if a given path matches any of the exclusion glob patterns.

    Args:
        path: The Path object to check.
        exclude_patterns: A list of glob patterns (e.g., ["*.tmp", "/path/to/ignore/*"]).

    Returns:
        True if the path matches any pattern, False otherwise.
    """
    path_str = str(path.resolve()) # Use resolved absolute path for matching
    for pattern in exclude_patterns:
        # fnmatch needs string paths
        if fnmatch.fnmatch(path_str, pattern):
            logger.debug(f"Path '{path_str}' excluded by pattern '{pattern}'")
            return True
        # Also check if any parent directory matches a pattern ending in /*
        # This handles cases like excluding 'node_modules/*'
        if pattern.endswith('/*'):
            base_pattern = pattern[:-2] # Remove '/*'
            current = path
            while current != current.parent: # Stop at root
                 if fnmatch.fnmatch(str(current.resolve()), base_pattern):
                      logger.debug(f"Path '{path_str}' excluded because parent '{current}' matches pattern '{pattern}'")
                      return True
                 current = current.parent

    return False

def human_readable_size(size_bytes: int) -> str:
    """Converts bytes to a human-readable string (KB, MB, GB)."""
    if size_bytes < 0: return "N/A"
    if size_bytes < SIZE_UNITS["K"]:
        return f"{size_bytes} B"
    elif size_bytes < SIZE_UNITS["M"]:
        return f"{size_bytes / SIZE_UNITS['K']:.1f} KB"
    elif size_bytes < SIZE_UNITS["G"]:
        return f"{size_bytes / SIZE_UNITS['M']:.1f} MB"
    elif size_bytes < SIZE_UNITS["T"]:
        return f"{size_bytes / SIZE_UNITS['G']:.1f} GB"
    else:
        return f"{size_bytes / SIZE_UNITS['T']:.1f} TB"

def calculate_hash(path: Path, algorithm: str = 'sha256') -> Optional[str]:
    """Calculates the hash of a file."""
    import hashlib
    hasher = hashlib.new(algorithm)
    try:
        with open(path, 'rb') as file:
            while True:
                chunk = file.read(4096) # Read in chunks
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except FileNotFoundError:
        logger.warning(f"File not found for hashing: {path}")
        return None
    except IOError as e:
        logger.error(f"Error reading file for hashing {path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error hashing file {path}: {e}", exc_info=True)
        return None


# Example Usage
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)

    print("--- Size Parsing ---")
    print(f"'1024' -> {parse_size('1024')}")
    print(f"'2k' -> {parse_size('2k')}")
    print(f"'1.5MB' -> {parse_size('1.5MB')}")
    print(f"' 3 G B ' -> {parse_size(' 3 G B ')}")
    print(f"'invalid' -> {parse_size('invalid')}")
    print(f"'10.5 ZB' -> {parse_size('10.5 ZB')}") # Invalid unit
    print(f"'10.5' -> {parse_size('10.5')}") # Invalid without unit

    print("\n--- Duration Parsing ---")
    print(f"'60s' -> {parse_duration('60s')}")
    print(f"'5m' -> {parse_duration('5m')}")
    print(f"'2h' -> {parse_duration('2h')}")
    print(f"'3d' -> {parse_duration('3d')}")
    print(f"'1w' -> {parse_duration('1w')}")
    print(f"'2month' -> {parse_duration('2month')}")
    print(f"'1y' -> {parse_duration('1y')}")
    print(f"'10' -> {parse_duration('10')}") # Assumes seconds
    print(f"'invalid' -> {parse_duration('invalid')}")
    print(f"'5 fortnights' -> {parse_duration('5 fortnights')}") # Invalid unit

    print("\n--- Command Execution ---")
    result_ls = run_command(['ls', '-l', '/nonexistent'], check=False)
    print(f"ls /nonexistent: code={result_ls.returncode}, stderr='{result_ls.stderr.strip()}'")
    result_echo = run_command(['echo', 'Hello World'])
    print(f"echo: code={result_echo.returncode}, stdout='{result_echo.stdout.strip()}'")

    print("\n--- Path Exclusion ---")
    exclusions = ["*.tmp", "/home/user/secrets/*", "*/node_modules/*"]
    print(f"Exclude '/tmp/file.tmp'? {is_path_excluded(Path('/tmp/file.tmp'), exclusions)}")
    print(f"Exclude '/home/user/secrets/key'? {is_path_excluded(Path('/home/user/secrets/key'), exclusions)}")
    print(f"Exclude '/home/user/project/file.txt'? {is_path_excluded(Path('/home/user/project/file.txt'), exclusions)}")
    print(f"Exclude '/home/user/project/node_modules/lib/index.js'? {is_path_excluded(Path('/home/user/project/node_modules/lib/index.js'), exclusions)}")

    print("\n--- Human Readable Size ---")
    print(f"123 -> {human_readable_size(123)}")
    print(f"2048 -> {human_readable_size(2048)}")
    print(f"1500000 -> {human_readable_size(1500000)}")
    print(f"5000000000 -> {human_readable_size(5000000000)}")

    print("\n--- Hashing ---")
    # Create a dummy file to hash
    dummy_file = Path("./temp_hash_test.txt")
    dummy_file.write_text("This is a test file for hashing.")
    print(f"Hash for {dummy_file}: {calculate_hash(dummy_file)}")
    dummy_file.unlink()
    print(f"Hash for non-existent file: {calculate_hash(Path('./nonexistent_file.txt'))}")
