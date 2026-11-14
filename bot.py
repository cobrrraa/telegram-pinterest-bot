"""Telegram-бот: принимает фото и заливает их пинами на выбранную доску Pinterest.

Фото не публикуются сразу, а встают в очередь: фоновая задача выкладывает их по одному
с паузой, а если Pinterest включает антиспам — ждёт и продолжает сама.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from dotenv import load_dotenv
from PIL import UnidentifiedImageError

from images import prepare_image
from pin_queue import PinQueue
from pinterest import MakeStoppedError, PinterestClient, PinterestError

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_PATH = DATA_DIR / "state.json"
# Фото из альбома приходят отдельными сообщениями — ждём, пока придут все.
ALBUM_WAIT_SECONDS = 1.5
# В inline-клавиатуре Telegram не больше 100 кнопок; одна уходит на «Отмена».
MAX_BOARD_BUTTONS = 99
# Список досок перечитываем не чаще раза в 10 минут: каждый запрос тратит кредиты Make.
BOARDS_TTL_SECONDS = 600
# Пауза между пинами. Если постить часто, Pinterest включает антиспам (ошибка 429).
PIN_INTERVAL_SECONDS = float(os.getenv("PIN_INTERVAL_MINUTES", "5")) * 60
# При антиспаме ждём сначала час, потом каждый раз вдвое дольше, но не больше 6 часов.
BACKOFF_START_SECONDS = float(os.getenv("BACKOFF_START_MINUTES", "60")) * 60
BACKOFF_MAX_SECONDS = float(os.getenv("BACKOFF_MAX_HOURS", "6")) * 3600
# Если Telegram не отдал файл из-за сетевой ошибки — пробуем снова через минуту.
NETWORK_RETRY_SECONDS = 60

log = logging.getLogger("bot")

pinterest = PinterestClient(
    webhook_url=os.environ["MAKE_WEBHOOK_URL"],
    secret=os.environ["MAKE_SECRET"],
    path_prefix=os.getenv("PINTEREST_PATH_PREFIX", "/v5"),
)
queue = PinQueue(DATA_DIR / "queue.json")
queue_event = asyncio.Event()  # будит фоновую задачу, когда в пустую очередь что-то добавили

router = Router()
router.message.filter(F.from_user.id == ALLOWED_USER_ID)
router.callback_query.filter(F.from_user.id == ALLOWED_USER_ID)

boards_cache: list[dict] = []
boards_fetched_at = 0.0
pending: dict[str, list[str]] = {}  # id выбора -> file_id фотографий, ждущих выбора доски
albums: dict[str, list[str]] = {}  # media_group_id -> file_id
background_tasks: set[asyncio.Task] = set()


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def human_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def wait_left() -> float:
    return max(0.0, queue.next_attempt_at - time.time())


async def get_boards(force: bool = False) -> list[dict]:
    global boards_cache, boards_fetched_at
    if force or not boards_cache or time.monotonic() - boards_fetched_at > BOARDS_TTL_SECONDS:
        boards_cache = await pinterest.list_boards()
        boards_fetched_at = time.monotonic()
    return boards_cache


def boards_keyboard(choice_id: str, boards: list[dict]) -> InlineKeyboardMarkup:
    last_id = load_state().get("last_board_id")
    # Последняя использованная доска — первой, остальные в исходном порядке.
    ordered = sorted(boards, key=lambda b: b["id"] != last_id)[:MAX_BOARD_BUTTONS]
    rows = [
        [InlineKeyboardButton(text=b["name"], callback_data=f"pin:{choice_id}:{b['id']}")]
        for b in ordered
    ]
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data=f"cancel:{choice_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def batch_text(batch_id: str, note: str = "") -> str:
    batch = queue.batches[batch_id]
    remaining = queue.remaining(batch_id)
    done = len(batch["links"])
    if remaining:
        header = f"📥 «{batch['board_name']}»: выложено {done} из {batch['total']}, в очереди ещё {remaining}"
    else:
        header = f"✅ «{batch['board_name']}»: выложено {done} из {batch['total']}"
    lines = [header] + batch["links"]
    if batch["errors"]:
        lines += ["", "❌ Не загружены:"] + batch["errors"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


async def render_batch(bot: Bot, batch_id: str, note: str = "") -> None:
    """Обновляет сообщение пачки; когда в ней ничего не осталось — забывает её."""
    batch = queue.batches.get(batch_id)
    if batch is None:
        return
    try:
        await bot.edit_message_text(
            batch_text(batch_id, note),
            chat_id=batch["chat_id"],
            message_id=batch["message_id"],
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramAPIError as e:
        # Например, «message is not modified» или сообщение удалено — на очередь это не влияет.
        log.warning("Не удалось обновить сообщение пачки %s: %s", batch_id, e)
    if not queue.remaining(batch_id):
        del queue.batches[batch_id]
        queue.save()


async def publish_next(bot: Bot) -> None:
    """Пытается выложить первое фото из очереди и сдвигает время следующей попытки."""
    item = queue.items[0]
    batch_id = item["batch"]
    batch = queue.batches[batch_id]
    try:
        raw = (await bot.download(item["file_id"])).read()
        image, content_type = prepare_image(raw)
        pin = await pinterest.create_pin(batch["board_id"], image, content_type)
    except TelegramNetworkError as e:
        log.warning("Telegram не отдал файл, повторю позже: %s", e)
        queue.next_attempt_at = time.time() + NETWORK_RETRY_SECONDS
        queue.save()
        return
    except MakeStoppedError as e:
        # Ждём, пока человек починит Make: иначе очередь сгорит, помечая каждое фото как незагруженное.
        log.warning("Очередь на паузе: %s", e)
        queue.paused = True
        if e.queued_in_make:
            # Повторно не шлём: Make мог сохранить запрос и выложить фото сам, получился бы дубль.
            batch["errors"].append(f"#{item['n']}: Make не запустил сценарий — фото может появиться позже само")
            queue.pop_first()
        queue.save()
        await render_batch(bot, batch_id, "⏸ Очередь на паузе, см. сообщение ниже.")
        await bot.send_message(
            batch["chat_id"],
            f"⏸ Очередь на паузе: {e}.\n\n"
            f"В очереди {len(queue.items)} фото, они никуда не денутся. Проверь кредиты и сценарий в Make, "
            "потом отправь /resume.",
        )
        return
    except PinterestError as e:
        if e.retryable:
            queue.backoff = min(max(queue.backoff * 2, BACKOFF_START_SECONDS), BACKOFF_MAX_SECONDS)
            queue.next_attempt_at = time.time() + queue.backoff
            queue.save()
            log.warning("Пауза %s: %s", human_duration(queue.backoff), e)
            await render_batch(bot, batch_id, f"⏸ {e}\nПродолжу сам примерно через {human_duration(queue.backoff)}.")
            return
        batch["errors"].append(f"#{item['n']}: {e}")
        queue.next_attempt_at = time.time() + PIN_INTERVAL_SECONDS
    except UnidentifiedImageError:
        # До Pinterest дело не дошло, поэтому следующее фото можно пробовать сразу.
        batch["errors"].append(f"#{item['n']}: файл не похож на картинку")
    except Exception as e:  # noqa: BLE001 — фото с непредвиденной ошибкой пропускаем, очередь не останавливаем
        log.exception("Не удалось загрузить фото %s", item["file_id"])
        batch["errors"].append(f"#{item['n']}: {e}")
    else:
        batch["links"].append(f"https://www.pinterest.com/pin/{pin['id']}/")
        queue.backoff = 0
        queue.next_attempt_at = time.time() + PIN_INTERVAL_SECONDS
    queue.pop_first()
    queue.save()
    note = f"Следующее фото — примерно через {human_duration(wait_left())}." if queue.remaining(batch_id) else ""
    await render_batch(bot, batch_id, note)


async def queue_worker(bot: Bot) -> None:
    while True:
        try:
            if not queue.items or queue.paused:
                queue_event.clear()
                await queue_event.wait()
                continue
            if wait_left() > 0:
                await asyncio.sleep(wait_left())
                continue
            await publish_next(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — фоновая задача не должна умирать
            log.exception("Сбой в обработчике очереди")
            await asyncio.sleep(60)


async def offer_boards(message: Message, file_ids: list[str]) -> None:
    try:
        boards = await get_boards()
    except PinterestError as e:
        await message.reply(f"Не смог получить доски: {e}")
        return
    if not boards:
        await message.reply("В аккаунте Pinterest нет ни одной доски — создай хотя бы одну и отправь /boards.")
        return
    choice_id = secrets.token_hex(4)
    pending[choice_id] = file_ids
    count = f"{len(file_ids)} фото" if len(file_ids) > 1 else "фото"
    await message.reply(f"Куда загрузить {count}?", reply_markup=boards_keyboard(choice_id, boards))


async def flush_album(message: Message, group_id: str) -> None:
    await asyncio.sleep(ALBUM_WAIT_SECONDS)
    await offer_boards(message, albums.pop(group_id))


async def accept_image(message: Message, file_id: str) -> None:
    group_id = message.media_group_id
    if not group_id:
        await offer_boards(message, [file_id])
        return
    if group_id not in albums:
        albums[group_id] = []
        task = asyncio.create_task(flush_album(message, group_id))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    albums[group_id].append(file_id)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Пришли фото (можно альбомом) и выбери доску — я поставлю их в очередь и буду выкладывать "
        f"в Pinterest по одному примерно раз в {human_duration(PIN_INTERVAL_SECONDS)}, "
        "чтобы не злить антиспам. Ссылки появятся в сообщении по мере публикации.\n\n"
        "Чтобы не терять качество, отправляй фото файлом (без сжатия).\n"
        "/queue — что сейчас в очереди\n"
        "/resume — продолжить, если очередь встала на паузу\n"
        "/clearqueue — очистить очередь\n"
        "/boards — обновить список досок"
    )


@router.message(Command("boards"))
async def cmd_boards(message: Message) -> None:
    try:
        boards = await get_boards(force=True)
    except PinterestError as e:
        await message.answer(f"Не смог получить доски: {e}")
        return
    names = "\n".join(f"• {b['name']}" for b in boards) or "(досок нет)"
    await message.answer(f"Досок: {len(boards)}\n{names}")


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    if not queue.items:
        await message.answer("Очередь пуста.")
        return
    lines = [f"В очереди {len(queue.items)} фото."]
    if queue.paused:
        lines.append("Очередь на паузе: Make не запускал сценарий. Когда починишь — /resume.")
    elif queue.backoff:
        lines.append(f"Pinterest включил антиспам — продолжу примерно через {human_duration(wait_left())}.")
    elif wait_left() > 0:
        lines.append(f"Следующее фото — примерно через {human_duration(wait_left())}.")
    else:
        lines.append("Выкладываю прямо сейчас.")
    await message.answer("\n".join(lines))


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not queue.paused:
        await message.answer("Очередь и так не на паузе. /queue — что в ней.")
        return
    queue.paused = False
    queue.next_attempt_at = 0
    queue.save()
    queue_event.set()
    await message.answer(f"Продолжаю. В очереди {len(queue.items)} фото.")


@router.message(Command("clearqueue"))
async def cmd_clear_queue(message: Message) -> None:
    count = len(queue.items)
    for batch_id in queue.clear():
        await render_batch(message.bot, batch_id, "🗑 Очередь очищена, остальные фото не выложены.")
    await message.answer(f"Убрал из очереди {count} фото." if count else "Очередь и так пуста.")


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    await accept_image(message, message.photo[-1].file_id)


@router.message(F.document.mime_type.startswith("image/"))
async def on_image_document(message: Message) -> None:
    await accept_image(message, message.document.file_id)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Я понимаю только фотографии и картинки, отправленные файлом.")


@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: CallbackQuery) -> None:
    pending.pop(callback.data.split(":", 1)[1], None)
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("pin:"))
async def on_board_chosen(callback: CallbackQuery) -> None:
    _, choice_id, board_id = callback.data.split(":", 2)
    file_ids = pending.pop(choice_id, None)
    if file_ids is None:
        await callback.answer("Эта кнопка устарела — пришли фото ещё раз.", show_alert=True)
        return
    await callback.answer()
    board_name = next((b["name"] for b in boards_cache if b["id"] == board_id), board_id)
    ahead = len(queue.items)
    batch_id = queue.add_batch(
        callback.message.chat.id, callback.message.message_id, board_id, board_name, file_ids
    )
    queue_event.set()

    state = load_state()
    state["last_board_id"] = board_id
    save_state(state)

    if queue.paused:
        note = "Очередь на паузе: Make не запускал сценарий. Когда починишь — /resume."
    elif ahead:
        note = f"Перед ними в очереди {ahead} фото."
    elif wait_left() > 0:
        note = f"Начну примерно через {human_duration(wait_left())}."
    else:
        note = "Начинаю."
    await render_batch(callback.bot, batch_id, note)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    worker = asyncio.create_task(queue_worker(bot))
    if queue.items:
        log.info("В очереди с прошлого запуска: %d фото", len(queue.items))
        queue_event.set()
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        await pinterest.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
