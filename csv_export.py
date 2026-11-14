"""Готовит архив фото к массовой загрузке в Pinterest через CSV.

Запуск (на ПК):
    .venv/Scripts/python -I csv_export.py <папка или .zip> --board "yeee"

Что делает:
- переводит фото в JPEG (с учётом поворота из EXIF), пропускает дубли и не-картинки;
- кладёт их в export/<секретная папка>/ со случайными именами;
- собирает export/pins_001.csv, pins_002.csv, ... по 100 пинов в каждом
  с названиями «Фото 1», «Фото 2», ... (Pinterest требует непустые и неповторяющиеся названия).

Потом папку с фото нужно залить на сервер в /opt/pinterest-bot/public/ (команда печатается в конце),
а CSV по одному загрузить в Pinterest: Создать → Создать пины массово.
"""

import argparse
import csv
import hashlib
import os
import secrets
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # фото с айфона
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
ROWS_PER_CSV = 100
MIN_SIDE = 200  # совсем мелкие картинки Pinterest отклоняет («image is too small»)
MAX_SIDE = 4000
HEADER = ["Title", "Media URL", "Pinterest board", "Thumbnail", "Description", "Link", "Publish date", "Keywords"]


def iter_files(source: Path, workdir: Path):
    if source.is_dir():
        yield from sorted(p for p in source.rglob("*") if p.is_file())
        return
    with zipfile.ZipFile(source) as zf:
        for n, info in enumerate( sorted(zf.infolist(), key=lambda i: i.filename)):
            if info.is_dir():
                continue
            # Имена из архива не используем как пути — только содержимое.
            target = workdir / f"{n}.bin"
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            yield target


def to_jpeg(path: Path) -> bytes | None:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if min(im.size) < MIN_SIDE:
                return None
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = BytesIO()
            im.save(buf, "JPEG", quality=90)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--board", required=True, help="название доски в Pinterest, как на сайте")
    parser.add_argument("--start", type=int, default=1, help="с какого номера начинать «Фото N»")
    parser.add_argument("--out", type=Path, default=Path("export"))
    parser.add_argument("--base-url", default=BASE_URL, help="Public image base URL, including /p")
    args = parser.parse_args()
    if not args.base_url.startswith(("https://", "http://")):
        parser.error("Set --base-url or PUBLIC_BASE_URL to an HTTP(S) URL ending with /p")
    base_url = args.base_url.rstrip("/")

    token = secrets.token_urlsafe(16)
    photos_dir = args.out / token
    photos_dir.mkdir(parents=True)
    seen, names, skipped, dupes = set(), [], 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        for path in iter_files(args.source, Path(tmp)):
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                dupes += 1
                continue
            seen.add(digest)
            jpeg = to_jpeg(path)
            if jpeg is None:
                skipped += 1
                continue
            name = f"{secrets.token_urlsafe(8)}.jpg"
            (photos_dir / name).write_bytes(jpeg)
            names.append(name)

    csv_files = []
    for i in range(0, len(names), ROWS_PER_CSV):
        csv_path = args.out / f"pins_{i // ROWS_PER_CSV + 1:03d}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(HEADER)
            for n, name in enumerate(names[i:i + ROWS_PER_CSV], start=args.start + i):
                writer.writerow([f"Фото {n}", f"{base_url}/{token}/{name}", args.board, "", "", "", "", ""])
        csv_files.append(csv_path)

    print(f"Фото: {len(names)}, пропущено (не картинки или слишком мелкие): {skipped}, дублей: {dupes}")
    print("CSV:", ", ".join(str(p) for p in csv_files))
    print(f"Следующий --start: {args.start + len(names)}")
    print("Залить фото на сервер:")
    print(f"  scp -r {photos_dir} user@YOUR_SERVER:/opt/pinterest-bot/public/")
    print(f"  ssh user@YOUR_SERVER chown -R pinbot:pinbot /opt/pinterest-bot/public/{token}")


if __name__ == "__main__":
    main()
