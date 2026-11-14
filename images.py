"""Подготовка картинки к загрузке: Pinterest принимает только JPEG/PNG,
а webhook Make не любит большие запросы, поэтому тяжёлые файлы пережимаем."""

from io import BytesIO

from PIL import Image, ImageOps

# base64 раздувает данные на треть; держим запрос к Make заметно меньше 5 МБ.
MAX_BYTES = 2_500_000


def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Возвращает (байты, content_type). Бросает PIL.UnidentifiedImageError, если это не картинка."""
    with Image.open(BytesIO(data)) as img:
        if img.format in ("JPEG", "PNG") and len(data) <= MAX_BYTES:
            return data, f"image/{img.format.lower()}"

        img = ImageOps.exif_transpose(img).convert("RGB")
        while True:
            out = BytesIO()
            img.save(out, "JPEG", quality=90)
            if out.tell() <= MAX_BYTES:
                return out.getvalue(), "image/jpeg"
            img.thumbnail((int(img.width * 0.8), int(img.height * 0.8)))
