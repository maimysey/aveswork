from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from services.metrics import metrics_service


class MetricsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user_id = None
        chat_id = None
        chat_type = "private"
        is_callback = False

        if isinstance(event, Message):
            if event.from_user:
                user_id = event.from_user.id
            if event.chat:
                chat_id = event.chat.id
                chat_type = event.chat.type
            is_callback = False

        elif isinstance(event, CallbackQuery):
            if event.from_user:
                user_id = event.from_user.id
            if event.message and event.message.chat:
                chat_id = event.message.chat.id
                chat_type = event.message.chat.type
            is_callback = True

        if user_id is not None:
            metrics_service.track(
                user_id=user_id,
                chat_id=chat_id,
                chat_type=chat_type,
                is_callback=is_callback
            )

        return await handler(event, data)