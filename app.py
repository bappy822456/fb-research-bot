"""Compatibility entry point for Render's default Python start command: gunicorn app:app"""
from webhook_app import app

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
