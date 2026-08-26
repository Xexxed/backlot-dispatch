"""Backlot Dispatch entrypoint. Run: uvicorn app.main:app --reload"""
import os

from app.web import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
