import asyncio
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import BOT_TOKEN, WORK_MODE, logger
from database import migrate_work_schema, dispose_all_engines
from models import WorkBase
from middlewares.metrics_middleware import MetricsMiddleware
from handlers import admin, employer, student, settings, start
from keyboards import FEED_WEBAPP_URL
from services.relay import relay_worker, setup_relay_routes
from services.webapp_work_api import setup_work_api

# Настройки Webhook и сервера
WEBHOOK_PATH = "/webhook"
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL", "https://aveswork.onrender.com").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", None)
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))

background_tasks: list[asyncio.Task] = []


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="AvesWork💼🚀", status=200)


async def on_startup(bot: Bot):
    logger.info("🔄 Инициализация сервиса AvesWork и проверка БД...")

    # 1. Таблицы AvesWork + новые колонки (статус смены, название, связка с ботами)
    await migrate_work_schema(WorkBase.metadata)

    # 2. Установка вебхука Telegram
    webhook_url = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"
    logger.info(f"🌐 Установка вебхука на {webhook_url}...")
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        secret_token=WEBHOOK_SECRET,
    )
    logger.info("🚀 Вебхук установлен. Сервис готов принимать смены и отклики!")

    # 3. Кнопка «Смены» слева от поля ввода — лента на сайте внутри Telegram
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Смены", web_app=WebAppInfo(url=FEED_WEBAPP_URL))
        )
    except Exception as e:
        logger.warning(f"⚠️ Не удалось установить кнопку ленты: {e}")

    # 4. Очередь коротких уведомлений через ботов факультетов
    background_tasks.append(asyncio.create_task(relay_worker()))
    logger.info(f"💼 Раздел «Смены» работает в режиме: {WORK_MODE}")


async def on_shutdown(bot: Bot):
    logger.info("🛑 Остановка сервиса AvesWork...")

    for task in background_tasks:
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)

    # Закрытие сессии бота и всех сетевых пулов к Supabase
    await bot.session.close()
    await dispose_all_engines()
    logger.info("✅ Все соединения закрыты. Сервис штатно остановлен")


def main():
    if not BOT_TOKEN:
        logger.critical("❌ Ошибка: BOT_TOKEN не задан в переменных окружения!")
        return

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Подключение сборщика метрик
    metrics_mw = MetricsMiddleware()
    dp.message.middleware(metrics_mw)
    dp.callback_query.middleware(metrics_mw)

    # Жизненный цикл aiogram
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Регистрация роутеров в порядке приоритета ААААААА
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(employer.router)
    dp.include_router(student.router)
    dp.include_router(settings.router)

    # Инициализация веб-приложения aiohttp
    app = web.Application()

    # Healthcheck
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    # Раздел «Смены» на сайте (/api/work/*) и связка с ботами факультетов (/internal/*)
    setup_work_api(app, bot)
    setup_relay_routes(app)

    # Обработчик вебхуков
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    logger.info(f"🚀 Запуск AvesWork на порту {WEB_SERVER_PORT}...")
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Сервис AvesWork остановлен")
