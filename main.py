"""Точка входа Yuko."""

import uvicorn
from config import WEB_HOST, WEB_PORT

if __name__ == "__main__":
    uvicorn.run("web.app:app", host=WEB_HOST, port=WEB_PORT, reload=True)
