"""Раздаёт фото по ссылкам, чтобы Pinterest мог забрать их при массовой загрузке из CSV.

Файлы лежат в public/<секретная папка>/; список файлов не показывается, поэтому без
ссылки из CSV до фото не добраться.
"""

import logging
import os
from pathlib import Path

from aiohttp import web

PUBLIC_DIR = Path(os.getenv("PUBLIC_DIR", "public"))
PORT = int(os.getenv("FILES_PORT", "80"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app.router.add_static("/p/", PUBLIC_DIR, show_index=False, follow_symlinks=False)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
