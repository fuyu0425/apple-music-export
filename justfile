default: check

export:
    uv run python apple_music_export.py

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyrefly check apple_music_export.py tests
    uv run python -m unittest discover -s tests
