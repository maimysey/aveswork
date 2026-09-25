from datetime import date, datetime
from collections import defaultdict
from typing import Any
from config import get_minsk_now


class MetricsService:
    def __init__(self) -> None:
        self._current_date: date = get_minsk_now().date()
        self._boot_time: datetime = get_minsk_now()

        # Суточные метрики активности (сбрасываются в полночь)
        self._daily_unique_users: set[int] = set()
        self._daily_requests: int = 0
        self._daily_hourly_distribution: dict[int, int] = defaultdict(int)

        # Суточные бизнес-метрики
        self._daily_vacancies: int = 0
        self._daily_deliveries: int = 0
        self._daily_applications: int = 0

        # Общие метрики с момента запуска инстанса
        self._total_requests: int = 0
        self._total_messages: int = 0
        self._total_callbacks: int = 0
        self._total_vacancies: int = 0
        self._total_deliveries: int = 0
        self._total_applications: int = 0

    def _check_and_rollover_day(self) -> None:
        today = get_minsk_now().date()
        if today != self._current_date:
            self._current_date = today
            self._daily_unique_users.clear()
            self._daily_requests = 0
            self._daily_hourly_distribution.clear()
            self._daily_vacancies = 0
            self._daily_deliveries = 0
            self._daily_applications = 0

    def track(
        self,
        user_id: int | None,
        chat_id: int | None = None,
        chat_type: str = "private",
        is_callback: bool = False,
    ) -> None:
        self._check_and_rollover_day()
        now = get_minsk_now()

        self._total_requests += 1
        self._daily_requests += 1
        self._daily_hourly_distribution[now.hour] += 1

        if user_id:
            self._daily_unique_users.add(user_id)

        if is_callback:
            self._total_callbacks += 1
        else:
            self._total_messages += 1

    # ==================== БИЗНЕС-ТРЕКЕРЫ ====================

    def track_vacancy_created(self) -> None:
        self._check_and_rollover_day()
        self._daily_vacancies += 1
        self._total_vacancies += 1

    def track_deliveries_sent(self, count: int) -> None:
        self._check_and_rollover_day()
        self._daily_deliveries += count
        self._total_deliveries += count

    def track_application(self) -> None:
        self._check_and_rollover_day()
        self._daily_applications += 1
        self._total_applications += 1

    # ==================== ВЫГРУЗКА СТАТИСТИКИ ====================

    def get_stats(self) -> dict[str, Any]:
        self._check_and_rollover_day()

        peak_hour_str = "—"
        if self._daily_hourly_distribution:
            peak_hour, peak_count = max(
                self._daily_hourly_distribution.items(), key=lambda x: x[1]
            )
            peak_hour_str = f"{peak_hour:02d}:00–{peak_hour:02d}:59 ({peak_count} запр.)"

        return {
            "boot_time": self._boot_time.strftime("%d.%m.%Y %H:%M"),
            "today_date": self._current_date.strftime("%d.%m.%Y"),
            "dau": len(self._daily_unique_users),
            "daily_requests": self._daily_requests,
            "peak_hour": peak_hour_str,
            "daily_vacancies": self._daily_vacancies,
            "daily_deliveries": self._daily_deliveries,
            "daily_applications": self._daily_applications,
            "total_requests": self._total_requests,
            "total_messages": self._total_messages,
            "total_callbacks": self._total_callbacks,
            "total_vacancies": self._total_vacancies,
            "total_deliveries": self._total_deliveries,
            "total_applications": self._total_applications,
        }


metrics_service = MetricsService()