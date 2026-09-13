"""BH-150 — перший запуск мак-збірки показує, що вона працює.

Проблема, з якою це написано (слова власника): застосунок із .dmg
перетягнули в Applications, запустили — і нічого. Іконки в доку немає
(``LSUIElement: true``, це фоновий агент), вікна немає, дашборд ніхто не
пропонує відкрити. Людина не знає, встановилось воно чи ні.

Тож після ПЕРШОГО успішного старту відкриваємо дашборд у типовому браузері.

Саме першого, а не кожного: агент піднімається при кожному вході в систему,
і браузер, що сам розкривається щоранку, — це вже не допомога, а нахабство.
Позначку ставимо файлом у теці даних; вона переживає оновлення (тека не
чіпається), тож удруге вкладка не відкриється навіть після переустановки.
"""

from __future__ import annotations

import logging
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

_MARKER_NAME = ".dashboard-opened"
# Скільки чекати, поки сервер почне відповідати. Старт frozen-збірки — це
# розпакування у тимчасову теку плюс підняття uvicorn; на слабкій машині це
# кілька секунд, тому запас великий, а крок дрібний.
_ATTEMPTS = 40
_STEP_SECONDS = 0.5


def open_dashboard_once(app_dir: Path, port: int) -> None:
    """Відкрити дашборд у браузері один раз за життя інсталяції. Не блокує.

    No-op усюди, крім frozen-збірки на macOS: на вінді установку показує
    інсталятор Inno зі своїм фінальним екраном, а запуск із джерел — це
    робота розробника, якому вкладка в браузері ні до чого.
    """
    if not (getattr(sys, "frozen", False) and sys.platform == "darwin"):
        return

    marker = app_dir / _MARKER_NAME
    if marker.exists():
        return

    url = f"http://localhost:{port}"

    def _wait_and_open() -> None:
        # Чекаємо саме відповіді сервера, а не просто паузу: відкрити
        # вкладку раніше, ніж він підніметься, означає показати людині
        # «не вдається встановити зʼєднання» — гірше, ніж не відкривати.
        for _ in range(_ATTEMPTS):
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                pass
            threading.Event().wait(_STEP_SECONDS)
        else:
            logger.warning("first run: сервер не відповів, дашборд не відкриваю")
            return

        # Позначку ставимо ПЕРЕД відкриттям: якщо webbrowser кине, другої
        # спроби при наступному вході бути не має — це та сама нав'язливість,
        # від якої нас береже позначка.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("opened\n")
        except OSError as exc:
            logger.warning("first run: не змогли поставити позначку: %s", exc)
        try:
            webbrowser.open(url)
            logger.info("first run: відкрив дашборд %s", url)
        except Exception as exc:  # noqa: BLE001 — вкладка не варта падіння
            logger.warning("first run: не вдалося відкрити браузер: %s", exc)

    threading.Thread(target=_wait_and_open, name="first-run-dashboard", daemon=True).start()
