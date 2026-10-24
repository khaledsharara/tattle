import logging

import uvicorn

from .api import create_app
from .config import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = load_settings()
uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False, access_log=False)
