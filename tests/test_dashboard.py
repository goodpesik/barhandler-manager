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


def test_the_offline_modal_links_to_the_till_only_while_it_runs() -> None:
    """PET-1055 — без цього заклад мусив би знати http://127.0.0.1:9898
    напамʼять. А посилання на порт, де ніхто не слухає, відкрило б помилку
    браузера, і це читається як «каса зламалась», а не «сервіс не працює»."""
    src = _dashboard_js()
    assert 'id="offline-open"' in src
    assert 'open.href = "http://127.0.0.1:" + (svc.port || 9898) + "/";' in src
    assert 'open.style.display = on && svc.running ? "" : "none";' in src
    # A new tab, and one that cannot reach back into the dashboard.
    assert 'id="offline-open" class="btn btn-default" target="_blank" rel="noopener"' in src
    # And hidden before the state is asked for: a link left from the last
    # time the window was open would point at a till we know nothing about,
    # and the failure branch never touches it.
    opened = src.split("async function openOfflineModal()", 1)[1]
    before_try = opened.split("try {", 1)[0]
    assert '$("offline-open").style.display = "none";' in before_try


# --- BH-195 --------------------------------------------------------------
#
# The Device Handler serves two products, and the offline window is opened for
# one row at a time. `offlineKey` picks the product's own string when the uk
# dictionary carries one and silently falls back to the shared (Petshandler)
# string when it does not -- so a missing override is not a crash, it is the
# other product's settings path shown to a FitStudioCRM shop.


def _i18n_entries(lang: str) -> dict[str, str]:
    """key -> value for one I18N dictionary, top indent level only."""
    src = _dashboard_js()
    body = src.split(f"    {lang}: {{", 1)[1].split("\n    },", 1)[0]
    return dict(re.findall(r'^      ([a-z0-9_]+): "(.*)",$', body, re.MULTILINE))


def _offline_products() -> list[str]:
    src = _dashboard_js()
    body = src.split("const OFFLINE_PRODUCTS = [", 1)[1].split("];", 1)[0]
    return re.findall(r'id: "([a-z0-9_]+)"', body)


def _product_titles() -> dict[str, str]:
    """product id -> the name the window prints for it."""
    src = _dashboard_js()
    body = src.split("const OFFLINE_PRODUCTS = [", 1)[1].split("];", 1)[0]
    titles = dict(re.findall(r'id: "([a-z0-9_]+)", title: "([^"]+)"', body))
    assert titles, "OFFLINE_PRODUCTS no longer lists id and title together"
    return titles


def _offline_override_bases() -> set[str]:
    """The keys `offlineKey` is asked for, taken from its call sites.

    Read from the source rather than listed here, so that a fourth key
    routed through the helper is covered the day it is added.
    """
    bases = set(re.findall(r'offlineKey\("([a-z0-9_]+)"', _dashboard_js()))
    assert len(bases) >= 3, f"offlineKey call sites not found: {bases}"
    return bases


#: The product the shared, un-suffixed keys belong to. `offlineKey` falls
#: back to those keys, so they are what every other product gets by default.
_SHARED_PRODUCT = "petshandler"


def _navigation_steps(hint: str) -> list[str]:
    """The steps a hint sends the person through, product name dropped.

    A hint reads «lead-in, Product -> step -> step -> button», so the first
    chunk carries the name and the rest is the path. Comparing paths keeps
    the check off the wording: a copy edit moves text, not steps.
    """
    steps = [part.strip() for part in hint.split("\u2192")]
    assert len(steps) > 2, f"not a navigation path: {hint}"
    return steps[1:]


def _offline_strings(lang: str) -> dict[str, dict[str, str]]:
    """product -> {base key: the string the window actually shows for it}.

    Mirrors `offlineKey`: the product's own entry when the dictionary has
    one, the shared entry otherwise.
    """
    entries = _i18n_entries(lang)
    shown: dict[str, dict[str, str]] = {}
    for product in _offline_products():
        own = {}
        for base in _offline_override_bases():
            assert base in entries, f"{lang}: no shared {base} to fall back to"
            own[base] = entries.get(f"{base}_{product}", entries[base])
        shown[product] = own
    return shown


def test_each_product_has_its_own_offline_hint() -> None:
    """A product with no entry of its own is shown Petshandler's text, and
    nothing fails while it happens -- so every key the helper is asked for
    needs an override, not just the hint. And an override that walks the
    same steps as the string it replaces is not an override: it is
    Petshandler's settings path under another product's name.
    """
    products = _offline_products()
    assert _SHARED_PRODUCT in products, "the shared keys are Petshandler's"
    bases = _offline_override_bases()
    for lang in ("uk", "en"):
        entries = _i18n_entries(lang)
        for product in products:
            if product == _SHARED_PRODUCT:
                continue
            for base in bases:
                assert f"{base}_{product}" in entries, (
                    f"{lang}: {base} falls back to {_SHARED_PRODUCT} for {product}"
                )
            assert _navigation_steps(
                entries[f"offline_hint_{product}"]
            ) != _navigation_steps(entries["offline_hint"]), (
                f"{lang}: the {product} hint repeats {_SHARED_PRODUCT}'s path"
            )


def test_a_product_specific_string_never_names_another_product() -> None:
    """A string shown for one product must point at that product and no
    other -- by the name it prints and by the path it sends the person
    down. The shared keys count as Petshandler's own, so rewriting one of
    them to name FitStudioCRM fails here as well.
    """
    titles = _product_titles()
    for lang in ("uk", "en"):
        shown = _offline_strings(lang)
        for product, strings in shown.items():
            for base, value in strings.items():
                for other, other_title in titles.items():
                    if other == product:
                        continue
                    assert other_title not in value, (
                        f"{lang}: the {base} shown for {product} names {other_title}"
                    )
        # Two products cannot both be reached by the same steps, so a hint
        # carrying another product's path is as wrong as one carrying its
        # name -- and that is the form the defect took.
        paths = {p: _navigation_steps(s["offline_hint"]) for p, s in shown.items()}
        for product, steps in paths.items():
            for other, other_steps in paths.items():
                if other != product:
                    assert steps != other_steps, (
                        f"{lang}: {product} and {other} are sent down the same path"
                    )
