"""Dashboard route serves the operator HTML at GET /."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY


def test_root_returns_dashboard_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # Sanity check that the template actually rendered (caught a stale
    # __API_KEY__ placeholder bug during early development).
    assert "<table" in body
    assert "barhandler-manager" in body
    assert "POS-термінали" in body


def test_root_embeds_api_key_for_js_fetch(client: TestClient) -> None:
    """The JS in the page calls the gated /devices + /terminal routes
    with X-Api-Key. The key is substituted into the page at render
    time — verify the placeholder is gone and the real key is there."""
    body = client.get("/").text
    assert "__API_KEY__" not in body
    assert DEFAULT_API_KEY in body


def test_root_does_not_require_api_key(client: TestClient) -> None:
    """Dashboard is unauthenticated by design — operator hits
    http://localhost:9999 in a browser. The handshake key is for the
    JSON endpoints the page itself calls, not the page."""
    # No X-Api-Key header.
    response = client.get("/")
    assert response.status_code == 200


# --- BH-161 --------------------------------------------------------------
#
# Дашборд читає `interactive` із відповіді /system/update, бо там, де
# відкривається майстер установки (мак-застосунок, вінда-збірка), годинник іде
# по ЛЮДИНІ, а не по установці. Доти кнопка казала «Перезапуск…» і через 5
# хвилин зʼявлялось «не вдалось оновитись» — на цілком успішному оновленні,
# яке людина просто ще не дотиснула.

import re
from pathlib import Path

_DASHBOARD_PY = Path(__file__).resolve().parent.parent / "src" / "routes" / "dashboard.py"


def _dashboard_js() -> str:
    return _DASHBOARD_PY.read_text(encoding="utf-8")


def _i18n_keys(lang: str) -> set[str]:
    """Ключі одного словника I18N — рівно ті, що на першому рівні відступу."""
    src = _dashboard_js()
    body = src.split(f"    {lang}: {{", 1)[1].split("\n    },", 1)[0]
    return set(re.findall(r"^      ([a-z0-9_]+):", body, re.MULTILINE))


def test_every_dashboard_string_exists_in_both_languages() -> None:
    """Ключ, доданий лише в один словник, на іншій мові рендериться як сам
    ключ. Ні збірка, ні лінтер про це не скажуть."""
    uk, en = _i18n_keys("uk"), _i18n_keys("en")
    assert uk == en, (
        f"лише в uk: {sorted(uk - en)}; лише в en: {sorted(en - uk)}"
    )


def test_the_dashboard_reads_the_interactive_flag() -> None:
    """Без цього дашборд мусив би вгадувати стан за українським текстом
    повідомлення."""
    js = _dashboard_js()
    assert "watchUpdate(beforeVer, !!res.interactive)" in js
    assert 'res.interactive ? "btn_wizard_open" : "btn_restarting"' in js


def test_the_wizard_deadline_is_longer_than_the_automatic_one() -> None:
    """Суть фікса: 5 хвилин — це той самий фальшивий провал. Перевіряємо ЗНАК
    різниці, а не наявність тернарника."""
    js = _dashboard_js()
    m = re.search(
        r"const DEADLINE_MS = interactive \? (\d+) : (\d+);", js
    )
    assert m, "дедлайн більше не залежить від interactive"
    wizard, automatic = int(m.group(1)), int(m.group(2))
    assert wizard > automatic, (
        f"майстер ({wizard}мс) мусить мати БІЛЬШЕ часу за автоматичне "
        f"оновлення ({automatic}мс) — його тисне людина"
    )
    assert wizard >= 600000, "менше 10 хвилин на майстер — це знову фальшивий провал"


def test_the_wizard_timeout_says_the_wizard_was_not_finished() -> None:
    """«Оновлення не завершилось» на непройденому майстрі посилає людину
    шукати ваду там, де її немає."""
    js = _dashboard_js()
    assert 'interactive ? "update_wizard_timeout" : "update_timeout"' in js
    for lang in ("uk", "en"):
        assert "update_wizard_timeout" in _i18n_keys(lang)


def test_the_dashboard_shows_a_refusal_text_not_a_status_code() -> None:
    """BH-164 — сервер відмовляє текстом для людини («зараз іде оплата
    карткою, спробуйте за хвилину»). Доти `api()` викидав тіло відповіді й
    показував «/system/update → 409»."""
    js = _dashboard_js()
    body = js.split("async function api(", 1)[1].split("\n  }", 1)[0]
    assert "detail.message" in body, "текст відмови не читається з detail.message"
    assert "res.status" in body, "фолбек на код відповіді має лишитись"
