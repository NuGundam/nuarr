"""Start the nuarr web UI.  python serve.py  ->  http://localhost:8770"""
import uvicorn

from app.config import SETTINGS

if __name__ == "__main__":
    print(f"nuarr -> http://localhost:{SETTINGS.port}")
    uvicorn.run("app.web:app", host=SETTINGS.host, port=SETTINGS.port,
                log_level="info")
