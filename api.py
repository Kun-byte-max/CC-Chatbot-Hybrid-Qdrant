"""
api.py — Unified Search & Chatbot Entrypoint.
Delegates to backend.main:app for unified single-server operation.
"""

from backend.main import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
