from pathlib import Path
import subprocess


def active_music_library() -> Path:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-w", "-a", "-c", "AMPLibraryAgent", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    databases = {
        Path(line[1:])
        for line in result.stdout.splitlines()
        if line.startswith("n") and line.endswith("/Library.musicdb")
    }

    if len(databases) != 1:
        raise RuntimeError(f"Expected one active Music library, found {len(databases)}")

    return databases.pop().parent


if __name__ == "__main__":
    print(active_music_library())
