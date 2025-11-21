import logging
import os
from io import BytesIO
from uuid import uuid4

from telegram import Update, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types

from aiohttp import web


# ----------------- ЛОГИРОВАНИЕ -----------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------- НАСТРОЙКИ -----------------

ZENMUX_BASE_URL = "https://zenmux.ai/api/vertex-ai"
IMAGE_MODEL_ID = "google/gemini-3-pro-image-preview-free"

_genai_client: genai.Client | None = None

OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")  # опционально


def get_genai_client() -> genai.Client:
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
        http_options=types.HttpOptions(api_version="v1", base_url=ZENMUX_BASE_URL),
    )
    return _genai_client


def generate_image(prompt: str) -> BytesIO:
    client = get_genai_client()

    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL_ID,
            contents=[prompt],
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
    except Exception as e:
        logger.exception("Ошибка Zenmux API")
        raise RuntimeError(f"Ошибка API: {e}")

    for part in response.parts:
        if part.inline_data:
            img = part.as_image()

            tmp = f"/tmp/zen_{uuid4().hex}.png"
            img.save(tmp)

            with open(tmp, "rb") as f:
                data = f.read()

            buf = BytesIO(data)
            buf.seek(0)
            return buf

    raise RuntimeError("API не вернул изображение")


# ----------------- TELEGRAM HANDLERS -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне текст, и я сгенерирую картинку 🖼"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    prompt = update.message.text.strip()

    wait = await update.message.reply_text("Генерирую...")

    try:
        img = generate_image(prompt)
    except Exception as e:
        await wait.edit_text(f"Ошибка генерации: {e}")
        return

    img.name = "image.png"
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=img,
        caption=f"Запрос: `{prompt}`",
        parse_mode="Markdown",
    )

    if OWNER_CHAT_ID:
        buf = BytesIO(img.getvalue())
        buf.name = "image.png"
        buf.seek(0)
        await context.bot.send_photo(
            chat_id=OWNER_CHAT_ID,
            photo=buf,
            caption=f"Новый запрос:\n`{prompt}`",
            parse_mode="Markdown",
        )

    await wait.delete()


# ----------------- HEALTH ENDPOINT -----------------

async def health(request):
    return web.Response(text="OK", status=200)


# ----------------- MAIN -----------------

async def on_startup(app: Application):
    logger.info("Bot is ready.")


def main():
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")

    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL не задан")

    PORT = int(os.getenv("PORT", "8443"))

    application = Application.builder().token(TOKEN).build()

    # Telegram handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # AIOHTTP app for custom routes (health)
    aio_app = web.Application()
    aio_app.router.add_get("/", health)
    aio_app.router.add_get("/health", health)
    aio_app.router.add_get("/alive", health)

    application.post_init = on_startup

    webhook_path = f"/webhook/{TOKEN}"
    full_webhook_url = WEBHOOK_URL.rstrip("/") + webhook_path

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        web_app=aio_app,  # добавляем наши маршруты
    )


if __name__ == "__main__":
    main()
