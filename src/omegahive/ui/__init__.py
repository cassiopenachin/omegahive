"""The read-only, operator-facing OmegaHive web projection.

No eager `from .app import create_app` here: `ui.app` imports `api` (the versioned
JSON API it mounts), and `api.service` imports `ui.presenters` (the board-shape
helper it shares with the HTML UI) — an eager re-export at this level would make
that a circular import. Import `omegahive.ui.app` directly (as `cli.py`'s
`uvicorn.run("omegahive.ui.app:app", ...)` and every test already do).
"""
