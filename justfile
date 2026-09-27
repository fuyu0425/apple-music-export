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
    uv run --group metadata pyrefly check apple_music_export.py apple_music_listenbrainz.py apple_music_recover.py apple_music_apply.py active_music_library.py apple_music_metadata_match.py apple_music_metadata_apply.py apple_music_alac.py apple_music_strawberry.py app.py tests
    uv run --group metadata python -m unittest discover -s tests
    emacs --batch --quick -L . -L tests -l apple-music-metadata-review-test -f ert-run-tests-batch-and-exit
