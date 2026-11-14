"""Переносит фото из очереди бота в CSV для массовой загрузки в Pinterest.

Запуск на сервере при остановленном боте (иначе он продолжит выкладывать те же фото):
    systemctl stop pinterest-bot
    cd /opt/pinterest-bot && sudo -u pinbot .venv/bin/python queue_export.py --start 353
    systemctl start pinterest-bot

Скачивает фото из Telegram в public/<секретная папка>/, пишет public/csv/pins_queue_NNN.csv
и очищает очередь. Сообщения пачек в Telegram помечаются, что фото ушли в CSV.
"""

import argparse
import asyncio
import csv
import os
import secrets
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError

from pin_queue import PinQueue

load_dotenv()

BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
ROWS_PER_CSV = 100
HEADER = ["Title", "Media URL", "Pinterest board", "Thumbnail", "Description", "Link", "Publish date", "Keywords"]


def to_jpeg(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        buf = BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="с какого номера начинать «Фото N»")
    parser.add_argument("--base-url", default=BASE_URL, help="Public image base URL, including /p")
    args = parser.parse_args()
    if not args.base_url.startswith(("https://", "http://")):
        parser.error("Set --base-url or PUBLIC_BASE_URL to an HTTP(S) URL ending with /p")
    base_url = args.base_url.rstrip("/")

    data_dir = Path(os.getenv("DATA_DIR", "data"))
    public_dir = Path(os.getenv("PUBLIC_DIR", "public"))
    queue = PinQueue(data_dir / "queue.json")
    token = secrets.token_urlsafe(16)
    photos_dir = public_dir / token
    photos_dir.mkdir(parents=True)

    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
    rows, failed = [], []
    try:
        for item in queue.items:
            board = queue.batches[item["batch"]]["board_name"]
            try:
                jpeg = to_jpeg((await bot.download(item["file_id"])).read())
            except (TelegramAPIError, UnidentifiedImageError, OSError) as e:
                failed.append(f"{item['file_id']}: {e}")
                continue
            name = f"{secrets.token_urlsafe(8)}.jpg"
            (photos_dir / name).write_bytes(jpeg)
            rows.append((f"{base_url}/{token}/{name}", board))

        csv_dir = public_dir / "csv"
        csv_dir.mkdir(exist_ok=True)
        for i in range(0, len(rows), ROWS_PER_CSV):
            path = csv_dir / f"pins_queue_{i // ROWS_PER_CSV + 1:03d}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(HEADER)
                for n, (url, board) in enumerate(rows[i:i + ROWS_PER_CSV], start=args.start + i):
                    writer.writerow([f"Фото {n}", url, board, "", "", "", "", ""])
            print("CSV:", path)

        if failed:
            print("Не скачались (остались в очереди):", *failed, sep="\n  ")
            return
        for batch_id in queue.clear():
            batch = queue.batches.pop(batch_id)
            try:
                await bot.edit_message_text(
                    f"📄 «{batch['board_name']}»: выложено через бота {len(batch['links'])} из {batch['total']}, "
                    "остальные перенесены в CSV для массовой загрузки.",
                    chat_id=batch["chat_id"],
                    message_id=batch["message_id"],
                )
            except TelegramAPIError:
                pass
        queue.backoff = 0
        queue.next_attempt_at = 0
        queue.save()
        print(f"Фото: {len(rows)}, очередь очищена")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
