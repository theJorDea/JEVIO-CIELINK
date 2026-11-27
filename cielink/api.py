"""Local training API. Run with: uv run uvicorn cielink.api:app."""
import os
from fastapi import FastAPI


def create_app() -> FastAPI:
    if os.getenv("CIELINK_MODE", "training") != "training":
        raise ValueError("Only CIELINK_MODE=training is implemented")
    app = FastAPI(title="CIELINK", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "training"}

    return app


app = create_app()
