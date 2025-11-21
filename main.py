import logging
import os
from io import BytesIO

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

_genai_client: genai.Client | None = None

OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")  # опционально


def get_genai_client() -> genai.Client:
    """
    Ленивая инициализация клиента Google GenAI через Zenmux.
    """
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    api_key = os.getenv("ZENMUX_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Не задана переменная окружения ZENMUX_API_KEY "
            "(сюда нужно положить ваш sk-ai-v1-ключ от Zenmux)"
        )

    logger.info("Инициализирую GenAI клиент с кастомным base_url %s", ZENMUX_BASE_URL)

    _genai_client = genai.Client(
        api_key=api_key,
        vertexai=True,
        http_options=types.HttpOptions(
            api_version="v1",
            base_url=ZENMUX_BASE_URL,
        ),
    )
    return _genai_client


def generate_image(prompt: str) -> BytesIO:
    """
    Генерация картинки через Zenmux / Google Gemini 3 Pro Image Preview.

    Возвращает BytesIO с изображением, готовым к отправке в Telegram.
    """
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
        logger.exception("Ошибка при вызове Zenmux / GenAI API")
        raise RuntimeError(f"Ошибка при обращении к API генерации: {e}")

    image_bytes_io: BytesIO | None = None

    for part in response.parts:
        if part.inline_data is not None:
            img = part.as_image()
            buf = BytesIO()
            img.save(buf)  # важно — без format=
            buf.seek(0)
            image_bytes_io = buf
            break

    if image_bytes_io is None:
        logger.error("API не вернул картинку. Полный ответ: %s", response)
        raise RuntimeError("API не вернул изображение (inline_data отсутствует)")

    return image_bytes_io


# ----------------- ОБРАБОТЧИКИ ТЕЛЕГРАМ -----------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот, который генерирует картинки через Zenmux + Gemini 3 Pro 🖼\n\n"
        "Просто отправь текст с описанием изображения — и я попробую его нарисовать.\n\n"
        "Например:\n"
        "  кот-астронавт в неоновом городе, фотореализм\n"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Просто напиши текстовый запрос, и я сгенерирую картинку.\n\n"
        "Примеры:\n"
        "  кот-бариста в стиле неонового киберпанка\n"
        "  домик в лесу на рассвете, реалистичный стиль\n"
    )
    await update.message.reply_text(text)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    prompt = update.message.text.strip()
    if len(prompt) < 3:
        await update.message.reply_text("Слишком короткий запрос, попробуй описать подробнее 🙌")
        return

    await handle_generation(update, context, prompt)


async def handle_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    chat_id = update.effective_chat.id

    wait_message = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Генерирую картинку через Zenmux + Gemini 3 Pro…\n\n"
            f"Запрос:\n`{prompt}`"
        ),
        parse_mode="Markdown",
    )

    try:
        image_io = generate_image(prompt)
    except Exception as e:
        logger.error("Ошибка генерации: %s", e)
        await wait_message.edit_text(f"Не удалось сгенерировать картинку 😔\nОшибка: {e}")
        return

    # отправка пользователю
    try:
        image_io.name = "generated.png"
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_io,
            caption=f"Картинка по запросу:\n`{prompt}`",
            parse_mode="Markdown",
        )
        try:
            await wait_message.delete()
        except Exception:
            pass
    except Exception as e:
        logger.exception("Ошибка отправки изображения пользователю")
        await wait_message.edit_text(
            f"Картинка сгенерирована, но не удалось отправить её в чат.\nОшибка: {e}"
        )
        return

    # отправка владельцу
    if OWNER_CHAT_ID:
        try:
            buf2 = BytesIO(image_io.getvalue())
            buf2.name = "generated.png"
            buf2.seek(0)

            await context.bot.send_photo(
                chat_id=OWNER_CHAT_ID,
                photo=buf2,
                caption=f"Новая сгенерированная картинка.\nПромпт:\n`{prompt}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Ошибка отправки владельцу: %s", e)


# ----------------- ЗАПУСК ЧЕРЕЗ WEBHOOK (Render) -----------------


async def on_startup(app: Application):
    logger.info("Бот запущен и готов принимать обновления.")


def main():
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    if not telegram_token:
        raise RuntimeError("Не задана переменная окружения TELEGRAM_TOKEN")

    base_webhook_url = os.getenv("WEBHOOK_URL")
    if not base_webhook_url:
        raise RuntimeError(
            "Не задана переменная окружения WEBHOOK_URL.\n"
            "Пример: https://my-zenmux-bot.onrender.com"
        )

    port = int(os.getenv("PORT", "8443"))

    application = Application.builder().token(telegram_token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.post_init = on_startup

    webhook_path = f"/webhook/{telegram_token}"
    webhook_url = base_webhook_url.rstrip("/") + webhook_path

    logger.info(
        "Запуск webhook-сервера на порту %s, webhook_url=%s",
        port,
        webhook_url,
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
