"""Vstupní bod API serveru GEXLens."""

from fastapi import FastAPI

app = FastAPI(title="GEXLens API")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check pro monitoring a smoke testy."""
    return {"status": "ok"}
