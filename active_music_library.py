import subprocess
from pathlib import Path


def active_music_library() -> Path:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-w", "-a", "-c", "AMPLibraryAgent", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "lsof returned no error message."
        raise RuntimeError(f"Could not identify the active Music library: {detail}")

    databases = {
        Path(line[1:]).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("n") and line.endswith("/Library.musicdb")
    }

    if len(databases) != 1:
        raise RuntimeError(f"Expected one active Music library, found {len(databases)}")

    database = databases.pop()
    if not database.is_file() or database.stat().st_size == 0:
        raise RuntimeError(f"Active Music database is empty or missing: {database}")

    return database.parent


if __name__ == "__main__":
    print(active_music_library())
