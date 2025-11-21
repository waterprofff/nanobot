import logging
import os
from io import BytesIO
from uuid import uuid4
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types


# ----------------- ЛОГИРОВАНИЕ -----------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------- НАСТРОЙКИ -----------------

ZENMUX_BASE_URL = "https://zenmux.ai/api/vertex-ai"
IMAGE_MODEL_ID = "google/gemini-3-pro-image-preview-free"

_genai_client: Optional[genai.Client] = None

# Чат владельца бота (опционально)
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")

# Память: последняя картинка на чат (для "отредактируй...")
LAST_IMAGE_BY_CHAT: dict[int, bytes] = {}


def get_genai_client() -> genai.Client:
    """Ленивая инициализация клиента Google GenAI через Zenmux."""
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    api_key = os.getenv("ZENMUX_API_KEY")
    if not api_key:
        raise RuntimeError("ZENMUX_API_KEY не задан")

    logger.info("Инициализирую GenAI клиент %s", ZENMUX_BASE_URL)

    _genai_client = genai.Client(
        api_key=api_key,
        vertexai=True,
        http_options=types.HttpOptions(
            api_version="v1",
            base_url=ZENMUX_BASE_URL,
        ),
    )
    return _genai_client


def _extract_image_from_response(response) -> BytesIO:
    """Достаём изображение из ответа Gemini (через Zenmux)."""
    for part in response.parts:
        if part.inline_data:
            img = part.as_image()

            tmp_path = f"/tmp/zenmux_{uuid4().hex}.png"
            img.save(tmp_path)  # save ждёт путь, а не BytesIO

            with open(tmp_path, "rb") as f:
                data = f.read()

            buf = BytesIO(data)
            buf.seek(0)
            return buf

    raise RuntimeError("API не вернул изображение (inline_data отсутствует)")


def generate_image_from_text(prompt: str) -> BytesIO:
    """Генерация картинки только по тексту."""
    client = get_genai_client()

    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL_ID,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )
    except Exception as e:
        logger.exception("Ошибка Zenmux API (text->image)")
        raise RuntimeError(f"Ошибка API: {e}")

    return _extract_image_from_response(response)


def generate_image_from_image(prompt: str, image_bytes: bytes) -> BytesIO:
    """Генерация вариации по картинке + тексту."""
    client = get_genai_client()

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/jpeg",  # фото из Telegram обычно JPEG
    )

    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL_ID,
            contents=[
                prompt,
                image_part,
            ],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )
    except Exception as e:
        logger.exception("Ошибка Zenmux API (image+text->image)")
        raise RuntimeError(f"Ошибка API: {e}")

    return _extract_image_from_response(response)


# ----------------- TELEGRAM HANDLERS -----------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот на Zenmux + Gemini 3 Pro 🖼\n\n"
        "Я умею:\n"
        "• генерировать картинки по тексту;\n"
        "• делать вариации картинки по описанию.\n\n"
        "1️⃣ Просто напиши текст — я нарисую картинку.\n"
        "2️⃣ Пришли фото с подписью — сделаю вариацию по подписи.\n"
        "3️⃣ Напиши: «отредактируй полученное изображение: …» — "
        "я возьму последнюю картинку и сделаю новую версию."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться ботом:\n\n"
        "📝 Текст → новая картинка\n"
        "  «кот-бариста в стиле неонового киберпанка»\n\n"
        "🖼 Фото + подпись → вариация картинки\n"
        "  [фото] + «сделай поп-арт версию»\n\n"
        "✏️ Редактирование последней картинки\n"
        "  «отредактируй полученное изображение: сделай версию в стиле аниме»"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст: либо генерация, либо 'редактирование' последней картинки."""
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    prompt = update.message.text.strip()
    lower = prompt.lower()

    has_last_image = chat_id in LAST_IMAGE_BY_CHAT

    is_edit_command = (
        lower.startswith("отредактируй")
        or lower.startswith("измени картинку")
        or lower.startswith("сделай вариацию")
    )

    if is_edit_command and has_last_image:
        base_image_bytes = LAST_IMAGE_BY_CHAT[chat_id]
        await handle_generation(update, context, prompt, base_image_bytes)
    elif is_edit_command and not has_last_image:
        await update.message.reply_text(
            "Мне нечего редактировать — у меня пока нет сохранённого изображения.\n"
            "Сначала сгенерируй или пришли картинку 🙂"
        )
    else:
        # обычная генерация с нуля
        await handle_generation(update, context, prompt, base_image_bytes=None)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Фото:
    - если есть подпись — вариация по подписи,
    - если нет — мягкая художественная вариация.
    """
    message = update.message
    if not message or not message.photo:
        return

    chat_id = update.effective_chat.id

    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    data = await file.download_to_memory()
    image_bytes = bytes(data)

    caption = (message.caption or "").strip()
    if caption:
        prompt = caption
    else:
        prompt = (
            "Сделай более интересную художественную вариацию этого изображения, "
            "сохранив основную композицию."
        )

    await handle_generation(update, context, prompt, base_image_bytes=image_bytes)


async def handle_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    base_image_bytes: Optional[bytes],
):
    """Общая логика генерации."""
    chat_id = update.effective_chat.id

    wait = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Генерирую картинку через Zenmux + Gemini 3 Pro…\n\n"
            f"Запрос:\n`{prompt}`"
        ),
        parse_mode="Markdown",
    )

    try:
        if base_image_bytes is None:
            img_buf = generate_image_from_text(prompt)
        else:
            img_buf = generate_image_from_image(prompt, base_image_bytes)
    except Exception as e:
        logger.error("Ошибка генерации: %s", e)
        await wait.edit_text(f"Не удалось сгенерировать картинку 😔\nОшибка: {e}")
        return

    # сохраняем последнюю картинку для этого чата
    LAST_IMAGE_BY_CHAT[chat_id] = img_buf.getvalue()

    # 1) отправляем пользователю
    try:
        img_buf.name = "generated.png"
        img_buf.seek(0)

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=img_buf,
            caption=f"Картинка по запросу:\n`{prompt}`",
            parse_mode="Markdown",
        )
        try:
            await wait.delete()
        except Exception:
            pass
    except Exception as e:
        logger.exception("Ошибка отправки изображения пользователю")
        await wait.edit_text(
            f"Картинка сгенерирована, но не удалось отправить её в чат.\nОшибка: {e}"
        )
        return

    # 2) копия владельцу без данных пользователя
    if OWNER_CHAT_ID:
        try:
            owner_buf = BytesIO(LAST_IMAGE_BY_CHAT[chat_id])
            owner_buf.seek(0)
            owner_buf.name = "generated.png"

            await context.bot.send_photo(
                chat_id=OWNER_CHAT_ID,
                photo=owner_buf,
                caption=f"Новая сгенерированная картинка.\nПромпт:\n`{prompt}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Ошибка отправки владельцу: %s", e)


# ----------------- WEBHOOK (Render) -----------------


async def on_startup(app: Application):
    logger.info("Bot is ready.")


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN не задан")

    base_webhook_url = os.getenv("WEBHOOK_URL")
    if not base_webhook_url:
        raise RuntimeError("WEBHOOK_URL не задан (например, https://nanobot-92lp.onrender.com)")

    port = int(os.getenv("PORT", "8443"))

    application = Application.builder().token(token).build()

    # handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.post_init = on_startup

    webhook_path = f"/webhook/{token}"
    webhook_url = base_webhook_url.rstrip("/") + webhook_path

    logger.info("Запуск webhook-сервера на порту %s, webhook_url=%s", port, webhook_url)

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
