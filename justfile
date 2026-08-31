default: check

export:
    uv run python apple_music_export.py

build:
    npm --prefix frontend run build

serve: build
    uv run python app.py

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyrefly check apple_music_export.py apple_music_recover.py apple_music_apply.py active_music_library.py app.py tests
    uv run python -m unittest discover -s tests
