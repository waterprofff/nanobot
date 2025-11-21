import os
import logging
import requests

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Логирование (полезно смотреть в логах Render)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ====== НАСТРОЙКА ВНЕШНЕГО API ГЕНЕРАЦИИ КАРТИНОК ======

IMAGE_API_URL = os.getenv("IMAGE_API_URL")  # обязательная переменная окружения
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY")  # если у вас нет ключа — оставьте пустым


def generate_image(prompt: str) -> str:
    """
    Вызывает ваш API для генерации картинки.

    Ожидается, что API вернет JSON вида:
    {
        "image_url": "https://...."
    }

    Вернем строку image_url, чтобы отправить ее в Telegram.
    """

    if not IMAGE_API_URL:
        raise RuntimeError("Не задана переменная окружения IMAGE_API_URL")

    headers = {
        "Content-Type": "application/json",
    }

    # Если нужен ключ авторизации — добавляем.
    if IMAGE_API_KEY:
        # Поменяйте под свой API: иногда нужен "Authorization: Bearer <KEY>"
        headers["Authorization"] = f"Bearer {IMAGE_API_KEY}"

    payload = {
        "prompt": prompt,
        # сюда можно добавить другие параметры, если нужны:
        # "steps": 30,
        # "size": "1024x1024",
    }

    try:
        resp = requests.post(
            IMAGE_API_URL,
            json=payload,
            headers=headers,
            timeout=120,  # вдруг генерация долгая
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Ошибка запроса к API генерации: %s", e)
        raise RuntimeError(f"Ошибка запроса к API: {e}")

    try:
        data = resp.json()
    except ValueError as e:
        logger.error("Не удалось разобрать JSON ответа: %s", e)
        raise RuntimeError("API вернул не-JSON ответ")

    # Здесь подстроитесь под свой реальный ответ
    image_url = data.get("image_url")
    if not image_url:
        logger.error("В ответе API нет поля image_url: %s", data)
        raise RuntimeError("API не вернул поле image_url")

    return image_url


# ====== ОБРАБОТЧИКИ ТЕЛЕГРАМ-БОТА ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start."""
    text = (
        "Привет! Я бот для генерации картинок по текстовому запросу.\n\n"
        "Отправь мне промпт (описание картинки) — и я попробую сгенерировать изображение.\n\n"
        "Например:\n"
        "  кот-астронавт на фоне туманности, реалистичный стиль\n\n"
        "Или используй команду:\n"
        "  /imagine кот-астронавт на фоне туманности"
    )
    await update.message.reply_text(text)


async def imagine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /imagine <prompt>."""
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Напиши после /imagine, что нужно нарисовать 🙂")
        return

    await handle_generation(update, context, prompt)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любой обычный текст — воспринимаем как промпт."""
    if not update.message or not update.message.text:
        return

    prompt = update.message.text.strip()
    # Можно добавить фильтр: например, требовать хотя бы N символов
    if len(prompt) < 3:
        await update.message.reply_text("Слишком короткий запрос, попробуй описать подробнее 🙌")
        return

    await handle_generation(update, context, prompt)


async def handle_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    """Общий код генерации картинки и отправки результата пользователю."""
    chat_id = update.effective_chat.id

    # Сообщение-заглушка, чтобы пользователь видел, что что-то происходит
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Генерирую картинку по запросу:\n\n`{prompt}`\n\nЭто может занять немного времени…",
        parse_mode="Markdown",
    )

    try:
        image_url = generate_image(prompt)
    except Exception as e:
        logger.error("Ошибка генерации картинки: %s", e)
        await msg.edit_text(f"Не удалось сгенерировать картинку 😔\nОшибка: {e}")
        return

    # Пытаемся отправить картинку
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_url,
            caption=f"Картинка по запросу:\n`{prompt}`",
            parse_mode="Markdown",
        )
        # Удалим/обновим сообщение-заглушку
        await msg.delete()
    except Exception as e:
        logger.error("Ошибка отправки картинки в Telegram: %s", e)
        await msg.edit_text(f"Картинка сгенерирована, но не удалось отправить её в чат.\nОшибка: {e}")


# ====== ЗАПУСК БОТА ЧЕРЕЗ WEBHOOK (Render Web Service) ======

async def on_startup(app: Application):
    logger.info("Бот запущен и готов принимать обновления.")


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная окружения TELEGRAM_TOKEN")

    # URL вашего сервиса на Render, например:
    # https://my-image-bot.onrender.com
    base_webhook_url = os.getenv("WEBHOOK_URL")
    if not base_webhook_url:
        raise RuntimeError("Не задана переменная окружения WEBHOOK_URL")

    port = int(os.getenv("PORT", "8443"))

    # Создаем приложение Telegram
    application = Application.builder().token(token).build()

    # Хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("imagine", imagine_command))
    # Все текстовые сообщения — как промпты
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Хук на старт
    application.post_init = on_startup

    # Секретный путь webhook (используем токен, чтобы никто посторонний не дергал)
    webhook_path = f"/webhook/{token}"

    # Полный URL вебхука
    webhook_url = base_webhook_url.rstrip("/") + webhook_path

    logger.info("Запуск бота на порту %s, webhook_url=%s", port, webhook_url)

    # Запускаем встроенный веб-сервер и регистрируем webhook в Telegram
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
