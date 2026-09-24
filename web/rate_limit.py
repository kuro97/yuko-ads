"""In-memory rate limiter для защиты provider-backed эндпоинтов дашборда.

Зачем: ряд GET-ручек ходит во внешние API (Facebook, Trello, AMO, CDP, Google),
а тяжёлые POST-рефреши запускают дорогие пересчёты. Без лимита один клиент с
валидным ключом может выбить нам квоту провайдера или деньги на LLM. Лимитер
режет частоту ДО вызова провайдера (429 возвращается middleware до роутинга),
поэтому 429 никогда не приводит к обращению во внешний сервис.

Ограничение (важно): счётчики живут в памяти процесса. При запуске uvicorn с
workers>1 каждый воркер считает независимо — фактический лимит станет
workers * limit. В однопроцессном запуске (один воркер) лимит точный. При
масштабировании воркеров нужен общий стор (Redis) или documented-эквивалент.
"""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window-log лимитер. Потокобезопасен (общий lock).

    enabled=False → лимитер прозрачен (всегда пропускает). Под pytest выключен
    (иначе окно копилось бы между тестами и ловило ложные 429); в проде
    включается из web.app по флагу RATE_LIMIT_ENABLED (по умолчанию True).
    """

    def __init__(self) -> None:
        self.enabled: bool = False
        self._lock = threading.Lock()
        # key -> очередь monotonic-таймстемпов разрешённых запросов
        self._hits: dict[str, deque] = defaultdict(deque)

    def reset(self) -> None:
        """Полный сброс счётчиков (для тестов и ручной очистки)."""
        with self._lock:
            self._hits.clear()

    def allow(
        self,
        key: str,
        limit: int,
        window: float = 60.0,
        now: float | None = None,
    ) -> bool:
        """True — запрос в пределах лимита (и учтён). False — лимит превышен.

        При превышении таймстемп НЕ добавляется: отклонённые запросы не
        продлевают окно (защита от «залипания» на границе).
        """
        if not self.enabled or limit <= 0:
            return True
        ts = time.monotonic() if now is None else now
        cutoff = ts - window
        with self._lock:
            dq = self._hits[key]
            # Выкидываем всё, что старше окна.
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(ts)
            return True


# Единый общий экземпляр на процесс.
limiter = RateLimiter()
