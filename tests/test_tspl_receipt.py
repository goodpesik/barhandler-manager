"""BH-160 — TSPL-принтер мусить друкувати чек, а не лише етикетку.

Вада: на `reg.protocol` дивився ЛИШЕ `/print/label`. Решта маршрутів
(`receipt`, `fiscal`, `text`, `lines`, `kitchen`) жорстко слали ESC/POS, і на
етикетковому залізі це означало тишу: прошивка приймає байти й викидає їх, а
менеджер рапортує успіх. У клієнта це коштувало виїзду.
"""

from pathlib import Path

import pytest
from PIL import Image

from src.devices.printer import PrinterDevice
from src.devices.printer_models import protocol_for_model


class _FakeEscpos:
    """Мінімальний принтер: збирає все, що в нього пишуть."""

    def __init__(self) -> None:
        self.raw: list[bytes] = []
        self.cuts = 0

    def _raw(self, data: bytes) -> None:
        self.raw.append(data)

    def cut(self, *_a, **_k) -> None:
        self.cuts += 1

    def set(self, **_k) -> None:
        pass

    def text(self, s: str) -> None:
        # Записуємо, а не ковтаємо. Заглушка, що мовчки викидає текст, робить
        # фіктивним будь-який тест про те, ЯК той текст поїхав: «сирих байтів
        # немає» стає правдою просто тому, що немає нічого.
        self.raw.append(str(s).encode("utf-8", errors="replace"))


def _device(protocol: str, paper_width: int = 80) -> tuple[PrinterDevice, _FakeEscpos]:
    dev = PrinterDevice("t", {"enabled": True, "paper_width": paper_width, "protocol": protocol})
    esc = _FakeEscpos()
    dev._printer = esc
    return dev, esc


# ---------- таблиця моделей ----------

def test_the_clients_printer_is_recognised_under_its_own_brand():
    """Те саме залізо продають під різними марками: у клієнта воно зветься
    `4BARCODE 3B-365B`, а в драйверах виробника — `XP-365B`. Збіг має бути
    за ЯДРОМ моделі, інакше найголовніший випадок проходить повз."""
    assert protocol_for_model("4BARCODE 3B-365B") == "tspl"
    assert protocol_for_model("XP-365B") == "tspl"
    assert protocol_for_model("GEOSYS 365B") == "tspl"
    assert protocol_for_model("Xprinter XP-246B") == "tspl"


def test_unknown_model_says_i_do_not_know_rather_than_guessing():
    """None означає «не знаю». Мовчазна підміна — це та сама вада, з якої
    все почалось; рішення має ухвалити викликач."""
    assert protocol_for_model("Каса на кухні") is None
    assert protocol_for_model("USB printer 1fc9:2016") is None
    assert protocol_for_model("") is None
    assert protocol_for_model(None) is None


def test_table_holds_no_dangerously_short_token():
    """Токен на три символи збігся б із випадковим шматком чужої назви."""
    from src.devices.printer_models import TSPL_MODEL_CORES

    assert TSPL_MODEL_CORES, "таблиця порожня"
    assert min(len(c) for c in TSPL_MODEL_CORES) >= 4


# ---------- реєстрація ----------

def test_protocol_comes_from_the_device_not_from_the_role(client, auth_headers, monkeypatch):
    """Першопричина: оператор обрав роль «чек» на етикетковому залізі й
    мовчки отримав ESC/POS."""
    from src.devices import scan
    from src.models.printer import PrinterDescriptor, PrinterTransport, UsbAddress

    desc = PrinterDescriptor(
        id="usb:1fc9:2016",
        transport=PrinterTransport.usb,
        label="4BARCODE 3B-365B",
        usb=UsbAddress(vendor_id=0x1FC9, product_id=0x2016, in_ep=0x81, out_ep=0x03),
    )
    monkeypatch.setattr(scan, "discover_usb", lambda: [desc])
    client.post("/devices/discover", headers=auth_headers)

    r = client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": "receipt", "paper_width": 80},
    )
    assert r.status_code == 200, r.text
    body = r.json()["printer"]
    assert body["protocol"] == "tspl", "роль «чек» на TSPL-залізі має лишитись TSPL"
    assert body["protocol_source"] == "model-table"


def test_operator_choice_beats_the_table(client, auth_headers, monkeypatch):
    from src.devices import scan
    from src.models.printer import PrinterDescriptor, PrinterTransport, UsbAddress

    desc = PrinterDescriptor(
        id="usb:1fc9:2017",
        transport=PrinterTransport.usb,
        label="4BARCODE 3B-365B",
        usb=UsbAddress(vendor_id=0x1FC9, product_id=0x2017, in_ep=0x81, out_ep=0x03),
    )
    monkeypatch.setattr(scan, "discover_usb", lambda: [desc])
    client.post("/devices/discover", headers=auth_headers)

    r = client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": "receipt", "paper_width": 80, "protocol": "escpos"},
    )
    assert r.status_code == 200, r.text
    printer = r.json()["printer"]
    assert printer["protocol"] == "escpos"
    assert printer["protocol_source"] == "operator"


# ---------- обгортання ----------

def test_a_tspl_device_emits_one_job_not_one_label_per_line():
    """`PRINT` на кожен рядок виплюнув би окрему етикетку на рядок."""
    dev, esc = _device("tspl")
    dev._tspl_pages = [Image.new("1", (576, 40), 1) for _ in range(5)]
    dev._finalize_tspl()

    assert len(esc.raw) == 1, "на джоб має піти рівно один TSPL-блоб"
    blob = esc.raw[0]
    assert blob.count(b"PRINT") == 1
    assert blob.count(b"BITMAP") == 1


@pytest.mark.parametrize(("lines", "expected_mm"), [(3, 15), (7, 35), (20, 100)])
def test_receipt_height_follows_the_content(lines, expected_mm):
    """Чек — не етикетка фіксованої висоти: скільки вмісту, стільки й
    паперу. 8 точок/мм при 203 dpi.

    Кілька різних довжин навмисне: перша редакція цього тесту брала рівно
    ту довжину, що збігалася з дефолтною висотою етикетки (25 мм), і тому
    проходила навіть із намертво зашитою висотою.
    """
    dev, esc = _device("tspl")
    dev._tspl_pages = [Image.new("1", (576, 40), 1) for _ in range(lines)]
    dev._finalize_tspl()
    assert f"SIZE 80 mm, {expected_mm} mm".encode() in esc.raw[0]


def test_continuous_tape_means_gap_zero():
    """З ненульовим зазором принтер шукав би проміжок, якого на чековій
    стрічці немає, і гнав би порожнє."""
    dev, esc = _device("tspl")
    dev._tspl_pages = [Image.new("1", (576, 40), 1)]
    dev._finalize_tspl()
    assert b"GAP 0 mm, 0 mm" in esc.raw[0]


def test_escpos_device_is_untouched():
    """Зворотний бік: на чековому принтері нічого не змінилось."""
    dev, _esc = _device("escpos")
    assert dev._is_tspl() is False
    dev._tspl_pages = [Image.new("1", (576, 40), 1)]
    dev._finalize_tspl()
    # ESC/POS-пристрій сюди не потрапляє взагалі — рядки йдуть одразу GS v 0.


def test_a_failed_job_does_not_leak_its_lines_into_the_next_receipt():
    """Якщо джоб упав на півдорозі, його рядки НЕ мають вилізти зверху
    наступного чека — це чужі дані в чужому документі."""
    dev, esc = _device("tspl")
    dev._tspl_pages = [Image.new("1", (576, 40), 1) for _ in range(3)]
    dev._finalize_tspl()
    assert dev._tspl_pages == []
    esc.raw.clear()
    dev._finalize_tspl()
    assert esc.raw == [], "порожній накопичувач не має нічого друкувати"


def test_tspl_device_never_cuts():
    """Етикеткові принтери не мають ножа — команда розрізу в кращому разі
    нічого не значить, у гіршому клинить механізм."""
    dev, esc = _device("tspl")
    dev._install_bitmap_patch()
    esc.cut()
    assert esc.cuts == 0


def test_escpos_device_still_cuts():
    dev, esc = _device("escpos")
    dev._install_bitmap_patch()
    esc.cut()
    assert esc.cuts == 1


# ---------- тест-друк ----------

def _register_tspl_printer(client, auth_headers, monkeypatch, kind: str, pid: int):
    from src.devices import scan
    from src.models.printer import PrinterDescriptor, PrinterTransport, UsbAddress

    desc = PrinterDescriptor(
        id=f"usb:1fc9:{pid:04x}",
        transport=PrinterTransport.usb,
        label="4BARCODE 3B-365B",
        usb=UsbAddress(vendor_id=0x1FC9, product_id=pid, in_ep=0x81, out_ep=0x03),
    )
    monkeypatch.setattr(scan, "discover_usb", lambda: [desc])
    client.post("/devices/discover", headers=auth_headers)
    r = client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": kind, "paper_width": 80},
    )
    assert r.status_code == 200, r.text
    return desc.id


@pytest.mark.parametrize("kind", ["receipt", "kitchen", "label"])
def test_test_print_speaks_the_devices_language_whatever_the_role(
    client, auth_headers, monkeypatch, kind,
):
    """Саме це коштувало виїзду до клієнта: у ролі «чек» тест ішов
    ESC/POS-шляхом, повертав «відправлено на друк» і не друкував нічого.

    Гілку обирає МОВА ПРИСТРОЮ, а не роль. Перевіряємо всі три ролі: з
    прив'язкою до `kind == label` дві з них провалюються.
    """
    sent: list[bytes] = []

    class _Dev:
        def is_connected(self):
            return True

        async def enqueue(self, job):
            class _Esc:
                def _raw(self, data):
                    sent.append(data)

                def set(self, **_k):
                    pass

                def text(self, _s):
                    pass

                def cut(self, *_a, **_k):
                    pass

            await job(_Esc())

    printer_id = _register_tspl_printer(client, auth_headers, monkeypatch, kind, 0x3000 + len(kind))

    from src.devices.registry import PrinterRegistry

    async def _get_device(self, _id):
        return _Dev()

    monkeypatch.setattr(PrinterRegistry, "get_device", _get_device)

    r = client.post(f"/devices/{printer_id}/test-print", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["protocol"] == "tspl"
    blob = b"".join(sent)
    assert b"BITMAP" in blob and b"PRINT" in blob, "на TSPL-залізо пішов не TSPL"
    assert b"\x1dv0" not in blob, "на TSPL-залізо пішов ESC/POS-растр"


# ---------- перереєстрація ----------

def test_reregistering_rebuilds_the_device(client, auth_headers, monkeypatch):
    """Знайдено ЖИВИМ прогоном через емулятор, не тестами.

    `register()` не скидав закешований `PrinterDevice`, а той тримає конфіг:
    ширину паперу, протокол, режим рендеру, пін каси. Тож оператор міняв
    налаштування, тиснув тест — і отримував СТАРУ поведінку аж до перезапуску
    менеджера. Реєстрація на 58 мм лишала джоби 80-міліметровими.

    Це знецінило б увесь BH-160: вибрав TSPL у дашборді — нічого не змінилось.
    """
    from src.devices import scan
    from src.models.printer import PrinterDescriptor, PrinterTransport, NetworkAddress

    desc = PrinterDescriptor(
        id="net:1",
        transport=PrinterTransport.network,
        label="Network printer 10.0.0.5",
        network=NetworkAddress(host="10.0.0.5", port=9100),
    )
    monkeypatch.setattr(scan, "discover_network", lambda: [desc])
    client.post("/devices/discover", headers=auth_headers)

    registry = client.app.state.registry

    client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": "receipt", "paper_width": 80, "protocol": "escpos"},
    )
    stale = registry._build_device(registry.get_registration(desc.id))
    registry._devices[desc.id] = stale
    assert stale.paper_width == 80
    assert stale._is_tspl() is False

    client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": "receipt", "paper_width": 58, "protocol": "tspl"},
    )
    assert desc.id not in registry._devices, "старий пристрій лишився в кеші"

    fresh = registry._build_device(registry.get_registration(desc.id))
    assert fresh.paper_width == 58
    assert fresh._is_tspl() is True


def test_dropping_a_cached_device_works_without_an_event_loop(tmp_path):
    """Синхронний виклик (тест, CLI) не має падати на `asyncio.create_task`."""
    from src.devices.registry import PrinterRegistry

    registry = PrinterRegistry(path=tmp_path / "printers.json")

    class _Dev:
        async def disconnect(self):
            pass

    registry._devices["x"] = _Dev()
    registry._drop_cached_device("x")   # без запущеного циклу
    assert "x" not in registry._devices


# ---------- знайдене другим колом ревʼю ----------

def test_a_generic_usb_label_never_masquerades_as_a_known_model():
    """Найнебезпечніша регресія з усіх: PID — це чотири шістнадцяткові цифри,
    і більшість ядер у таблиці теж (`410B`, `330B`, `246B`). Тобто звичайний
    ESC/POS-принтер на чипі STMicro міг «упізнатись» як TSPL через власний
    PID — і перестати різати чеки, друкуючи сміття.
    """
    assert protocol_for_model("USB printer 0483:410b") is None
    assert protocol_for_model("USB printer 1a86:330b") is None
    assert protocol_for_model("USB printer 1fc9:2016") is None
    # Справжня назва поруч із парою vid:pid усе одно має впізнаватись.
    assert protocol_for_model("Xprinter XP-246B 0483:410b") == "tspl"


def test_known_escpos_brands_are_left_alone():
    """Зворотний бік: жоден звичайний чековий принтер не має раптом стати
    TSPL — це зламало б переважну більшість інсталяцій."""
    for name in [
        "EPSON TM-T20III", "Star TSP143III", "Citizen CT-S310II",
        "BIXOLON SRP-350III", "Network printer 192.168.0.86", "Каса на барі",
    ]:
        assert protocol_for_model(name) is None, name


def test_a_tall_receipt_is_split_instead_of_one_giant_job():
    """Стелі висоти не було: довгий кухонний квиток дав би скільки завгодно
    високий `SIZE`, а дешеві TSPL-плати мають межу довжини етикетки. Вийшло б
    рівно те, заради чого тікет і заведено — 200 OK і порожній папір."""
    from src.devices.printer import _TSPL_MAX_DOTS

    # Кількість рядків СТАЛА, а не похідна від стелі. Перша редакція рахувала
    # `_TSPL_MAX_DOTS // 40` рядків — і мутація «прибрати стелю» вимагала
    # 25 мільйонів зображень, тобто вбивала процес замість того, щоб зробити
    # тест червоним. Сто рядків по 40 точок = 4000 точок: помітно вище за
    # реальну стелю 2540 і дешево за памʼяттю.
    lines = 100
    assert lines * 40 > _TSPL_MAX_DOTS, "тестовий чек мусить бути вищим за стелю"
    dev, esc = _device("tspl")
    dev._tspl_pages = [Image.new("1", (576, 40), 1) for _ in range(lines)]
    dev._finalize_tspl()

    blob = b"".join(esc.raw)
    assert blob.count(b"PRINT") >= 2, "високий чек мав розрізатись на шматки"

    # `SIZE <ширина> mm, <висота> mm`. Стеля в точках, а SIZE в міліметрах і
    # округлений УГОРУ, тож і порівнюємо в міліметрах.
    max_mm = -(-_TSPL_MAX_DOTS // 8)
    heights = [
        int(h.split(b", ")[1].split(b" mm")[0])
        for h in blob.split(b"SIZE ")[1:]
    ]
    assert heights, "не знайшлось жодного SIZE"
    for mm in heights:
        assert mm <= max_mm, f"шматок {mm} мм вищий за стелю {max_mm} мм"
    # Разом шматки мусять покрити весь чек, нічого не загубивши.
    assert sum(heights) * 8 >= lines * 40


def test_a_short_receipt_is_still_one_job():
    from src.devices.printer import _TSPL_MAX_DOTS

    dev, esc = _device("tspl")
    dev._tspl_pages = [Image.new("1", (576, 40), 1) for _ in range(5)]
    dev._finalize_tspl()
    assert b"".join(esc.raw).count(b"PRINT") == 1


def test_an_unfinished_line_does_not_leak_into_the_next_receipt():
    """`buffer` живе в замиканні, яке ставиться ОДИН раз на пристрій. Джоб,
    що впав після `text("Разом")` і до `\\n`, лишав хвіст, який приклеювався
    до першого рядка наступного чека. На TSPL це гірше: розрізу між джобами
    немає, тож чужий уламок стає частиною того самого документа."""
    dev, esc = _device("tspl")
    dev._install_bitmap_patch()

    esc.text("недописаний хвіст")          # без \n — осідає в буфері замикання
    dev._reset_render_state()

    # `cut()` зливає буфер. Якщо хвіст там лишився — він зараз стане
    # сторінкою; якщо буфер очищено — не стане нічого.
    #
    # Перша редакція цього тесту дивилась на КІЛЬКІСТЬ сторінок після
    # наступного рядка, і хвіст просто приклеювався до нього спереду: сторінка
    # лишалась одна, тест зеленів із поверненою вадою.
    esc.cut()
    assert dev._tspl_pages == [], "недописаний хвіст пережив скидання стану"


@pytest.mark.asyncio
async def test_dropping_a_busy_printer_does_not_hang_the_request():
    """`CancelledError` — це BaseException, тож він летів повз обидва
    `except` у воркері, і `done` не отримував нічого: HTTP-запит, що чекав
    на друк, висів до таймауту клієнта без жодної помилки. А тепер пристрій
    скидають на КОЖНІЙ перереєстрації, не лише на видаленні."""
    import asyncio

    from src.devices.printer import PrinterDevice, PrinterUnavailable

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()
    started = asyncio.Event()

    async def _slow(_esc):
        started.set()
        await asyncio.sleep(30)

    dev._worker_task = asyncio.create_task(dev._worker())
    waiter = asyncio.create_task(dev.enqueue(_slow))
    await asyncio.wait_for(started.wait(), timeout=2)

    await dev.disconnect()

    with pytest.raises(PrinterUnavailable):
        await asyncio.wait_for(waiter, timeout=2)


@pytest.mark.asyncio
async def test_jobs_still_waiting_in_the_queue_are_woken_too():
    """Скасований воркер лишав чергу з невиконаними елементами — кожен із
    них теж чийсь HTTP-запит."""
    import asyncio

    from src.devices.printer import PrinterDevice, PrinterUnavailable

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()
    started = asyncio.Event()

    async def _slow(_esc):
        started.set()
        await asyncio.sleep(30)

    async def _second(_esc):
        pass

    dev._worker_task = asyncio.create_task(dev._worker())
    first = asyncio.create_task(dev.enqueue(_slow))
    await asyncio.wait_for(started.wait(), timeout=2)
    queued = asyncio.create_task(dev.enqueue(_second))
    await asyncio.sleep(0)

    await dev.disconnect()

    for t in (first, queued):
        with pytest.raises(PrinterUnavailable):
            await asyncio.wait_for(t, timeout=2)


# ---------- знайдене ТРЕТІМ поглядом (друге коло ревʼю на виправленнях) ----------

def test_windows_hardware_id_in_the_label_does_not_fake_a_model():
    """Перша редакція вирізала лише форму `0483:410B`. Друге коло показало,
    що цього мало: Windows Device Manager дає ідентифікатор як
    `USB\\VID_0483&PID_410B&REV_0100`, і такий рядок цілком може потрапити в
    поле назви при ручній реєстрації — саме в тому випадку «скан не бачить
    принтер», заради якого ручна реєстрація й існує."""
    for label in [
        r"USB\VID_0483&PID_410B&REV_0100",
        "Generic USB Printer (VID_0483&PID_410B)",
        "STM32 Composite Device 0483-410B",
        "USB printer 0483:410b",
    ]:
        assert protocol_for_model(label) is None, label
    # Справжня модель поруч з ідентифікатором усе одно впізнається.
    assert protocol_for_model(r"XP-246B USB\VID_0483&PID_410B") == "tspl"


def test_style_does_not_leak_from_one_receipt_to_the_next():
    """Скидати треба не лише буфер, а й стан: інакше жирний підсумок одного
    чека лишав би жирним перший рядок наступного."""
    # Еталон — з ОКРЕМОГО пристрою, якому стиль ніколи не ставили. Перша
    # редакція брала еталон із того самого пристрою після ще одного скидання,
    # тож із поверненою вадою обидві картинки виходили однаково жирними й
    # тест зеленів.
    clean_dev, clean_esc = _device("tspl")
    clean_dev._install_bitmap_patch()
    clean_esc.text("звичайний рядок\n")
    reference = clean_dev._tspl_pages[0]

    dev, esc = _device("tspl")
    dev._install_bitmap_patch()
    esc.set(bold=True, align="center", double_height=True)
    dev._reset_render_state()
    esc.text("звичайний рядок\n")
    after_reset = dev._tspl_pages[0]

    assert after_reset.size == reference.size, "стиль пережив скидання стану"
    assert after_reset.tobytes() == reference.tobytes(), "стиль пережив скидання стану"


def test_the_split_never_cuts_through_a_line():
    """Розріз за кратністю стелі міг лягти ПОСЕРЕД рядка, і літера виявлялась
    розділеною між двома окремими `PRINT`. Два послідовні проходи по суцільній
    стрічці мають люфт кроку — шов проходив би зазубриною крізь текст.

    Перевіряємо по-справжньому: розбираємо `BITMAP`-и назад і звіряємо з тим,
    що подали. Попередні тести дивились лише на кількість `PRINT` і заголовки
    `SIZE`, тож дублікат чи втрачений ряд на шві лишались невидимими.
    """
    from src.devices.printer import _TSPL_MAX_DOTS

    dev, esc = _device("tspl")
    # Висота рядка НЕ ділить стелю націло — саме тут раніше й падав шов.
    line_h = 37
    lines = (_TSPL_MAX_DOTS // line_h) + 3
    pages = []
    for i in range(lines):
        img = Image.new("1", (576, line_h), 1)
        # Мітка, унікальна для рядка: чорна точка на власній позиції.
        img.putpixel((i % 576, line_h // 2), 0)
        pages.append(img)
    dev._tspl_pages = list(pages)
    dev._finalize_tspl()

    blob = b"".join(esc.raw)
    assert blob.count(b"PRINT") >= 2, "чек мав розрізатись"

    # Розбираємо кожен BITMAP і склеюємо назад.
    rows: list[bytes] = []
    rest = blob
    while b"BITMAP " in rest:
        head = rest.split(b"BITMAP ", 1)[1]
        params, payload = head.split(b",", 5)[:5], head.split(b",", 5)[5]
        bpr, h = int(params[2]), int(params[3])
        data = payload[: bpr * h]
        rows.extend(data[i * bpr:(i + 1) * bpr] for i in range(h))
        rest = payload[bpr * h:]

    expected = [
        img.tobytes()[i * ((576 + 7) // 8):(i + 1) * ((576 + 7) // 8)]
        for img in pages
        for i in range(line_h)
    ]
    assert len(rows) == len(expected), "на шві загубились або задвоїлись ряди"
    assert rows == expected, "склеєний назад чек не збігається з поданим"


# ---------- BH-162: картинка, що не йде через text() ----------

def test_a_standalone_image_reaches_a_tspl_device():
    """QR фіскального чека — єдина картинка, яку рендерять окремо й писали
    сирим `_raw(image_to_gs_v_0(...))`. На TSPL-залізі прошивка такий растр
    мовчки викидає, і чек виходив БЕЗ QR — а порожній рядок перед ним (він
    іде через `text()`) друкувався справно, тому втрата була непомітна.

    Моя ж дірка з BH-160: полагодив текстовий шлях і пропустив те, що через
    текст не йде.
    """
    dev, esc = _device("tspl")
    dev._install_image_emitter()
    dev._install_bitmap_patch()

    esc.text("Касир: Super Admin\n")
    esc._bh_emit_image(Image.new("1", (384, 200), 1))
    dev._finalize_tspl()

    blob = b"".join(esc.raw)
    assert b"BITMAP" in blob, "картинка не дійшла до TSPL-пристрою"
    assert b"\x1dv0" not in blob, "картинка пішла ESC/POS-растром повз протокол"


def test_the_image_keeps_its_place_in_the_stream():
    """Картинка мусить лягти ПІСЛЯ рядків, які їй передували, а не поперед
    недописаного буфера."""
    dev, esc = _device("tspl")
    dev._install_image_emitter()
    dev._install_bitmap_patch()

    esc.text("перший рядок\n")
    esc.text("другий без переносу")          # осідає в буфері
    marker = Image.new("1", (384, 200), 1)
    marker.putpixel((5, 5), 0)
    esc._bh_emit_image(marker)

    assert len(dev._tspl_pages) == 3, "буфер не злився перед картинкою"
    assert dev._tspl_pages[-1] is marker, "картинка стала не в кінець"


def test_an_escpos_device_still_gets_the_raster():
    """Зворотний бік: на чековому принтері нічого не змінилось."""
    dev, esc = _device("escpos")
    dev._install_image_emitter()
    dev._install_bitmap_patch()
    esc._bh_emit_image(Image.new("1", (384, 200), 1))
    blob = b"".join(esc.raw)
    assert b"\x1dv0" in blob
    assert dev._tspl_pages == []


def _fiscal_receipt_with_qr():
    from datetime import datetime

    from src.models.fiscal_receipt import FiscalReceipt, FiscalReceiptItem

    return FiscalReceipt(
        business_name="ФОП Левинець Максим Сергійович",
        items=[FiscalReceiptItem(
            name="Курточка мембранна утеплена",
            quantity=1, price=3300.0, sum=3300.0, tax_symbol="З",
        )],
        paid_sum=3300.0, total_sum=3300.0,
        fiscal_number="TEST-g9qaC3",
        fiscal_date=datetime(2026, 9, 16, 15, 47, 2),
        cashier="Super Admin",
        qr_url="https://cabinet.tax.gov.ua/cashregs/check?id=TEST-g9qaC3",
    )


@pytest.mark.parametrize("render_mode", ["bitmap", "native"])
def test_a_real_fiscal_receipt_carries_its_qr_to_a_tspl_printer(render_mode):
    """НАСКРІЗНО, через справжній `render_fiscal_receipt`.

    Перша редакція цих тестів кликала `_bh_emit_image` напряму й жодного разу
    не заходила в те місце, де вада й була. Ревʼю показало, що навіть
    перевернута гілка (`if not callable(emit)`) їх проходила.

    `render_mode` тут обидва навмисне: віддавання картинки не має залежати
    від того, як рендериться ТЕКСТ. Доти хук жив усередині bitmap-шима, і при
    `native` фіскальний QR знову йшов сирим ESC/POS — тобто зникав.
    """
    from src.services.fiscal_receipt import render_fiscal_receipt

    dev, esc = _device("tspl")
    dev._install_image_emitter()
    if render_mode == "bitmap":
        dev._install_bitmap_patch()

    render_fiscal_receipt(esc, _fiscal_receipt_with_qr(), chars_per_line=32)
    dev._finalize_tspl()

    blob = b"".join(esc.raw)
    assert blob, "на принтер не пішло нічого"
    assert b"BITMAP" in blob, "QR не дійшов до TSPL-пристрою"
    assert b"\x1dv0" not in blob, "QR пішов ESC/POS-растром повз протокол"


def test_a_real_fiscal_receipt_still_prints_its_qr_on_escpos():
    """Зворотний бік: на чековому принтері байти ті самі, що й до фікса."""
    from src.services.fiscal_receipt import render_fiscal_receipt

    dev, esc = _device("escpos")
    dev._install_image_emitter()
    dev._install_bitmap_patch()

    render_fiscal_receipt(esc, _fiscal_receipt_with_qr(), chars_per_line=32)

    blob = b"".join(esc.raw)
    assert b"\x1dv0" in blob, "QR не пішов ESC/POS-растром"
    assert dev._tspl_pages == []


def test_the_image_emitter_does_not_depend_on_the_text_render_mode():
    """Як рендериться ТЕКСТ і якою МОВОЮ говорить пристрій — різні осі.
    Хук мусить зʼявитись навіть там, де bitmap-шима немає взагалі."""
    dev, esc = _device("tspl")
    dev._install_image_emitter()          # БЕЗ _install_bitmap_patch
    assert callable(getattr(esc, "_bh_emit_image", None))

    esc._bh_emit_image(Image.new("1", (384, 200), 1))
    dev._finalize_tspl()
    assert b"BITMAP" in b"".join(esc.raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("render_mode", ["bitmap", "native"])
async def test_a_job_run_through_the_worker_gets_the_image_emitter(render_mode):
    """Проводка, а не самі деталі.

    Мутація «прибрати `_install_image_emitter()` з воркера» пережила всі
    попередні тести: кожен ставив емітер руками. Тобто перевірялось, що
    деталь працює, і НЕ перевірялось, що її взагалі вмикають.

    Тут джоб іде справжньою чергою пристрою — тим самим шляхом, яким ходить
    `/print/fiscal`. `native` серед параметрів навмисне: саме там раніше й
    зникав QR.
    """
    import asyncio

    from src.services.fiscal_receipt import render_fiscal_receipt

    dev, esc = _device("tspl")
    dev._config["render_mode"] = render_mode
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(printer):
        render_fiscal_receipt(printer, _fiscal_receipt_with_qr(), chars_per_line=32)

    await asyncio.wait_for(dev.enqueue(_job), timeout=5)
    await dev.disconnect()

    blob = b"".join(esc.raw)
    assert blob, "на принтер не пішло нічого"
    assert b"BITMAP" in blob, "QR не дійшов: емітер картинок не ввімкнули"
    assert b"\x1dv0" not in blob, "QR пішов ESC/POS-растром повз протокол"


@pytest.mark.asyncio
async def test_native_render_mode_is_overridden_on_tspl_hardware():
    """`native` на TSPL не працює й працювати не може: текст іде нативними
    ESC/POS-байтами, яких прошивка не розуміє. Живий прогін у цій парі дав
    чек, де приїхав сам QR без жодного рядка тексту.

    Комбінація дозволена моделлю й ніде не звіряється, тож лишити її мовчки
    означало б лишити конфігурацію, у якій принтер друкує пів-чека.
    """
    import asyncio

    from src.services.fiscal_receipt import render_fiscal_receipt

    dev, esc = _device("tspl")
    dev._config["render_mode"] = "native"
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(printer):
        render_fiscal_receipt(printer, _fiscal_receipt_with_qr(), chars_per_line=32)

    await asyncio.wait_for(dev.enqueue(_job), timeout=5)
    await dev.disconnect()

    blob = b"".join(esc.raw)
    assert b"BITMAP" in blob
    # Текст мусить бути НА КАРТИНЦІ, а не піти повз протокол сирими байтами.
    assert b"\x1dv0" not in blob
    assert b"TEST-g9qaC3" not in blob, (
        "текст пішов нативними байтами — TSPL-прошивка їх викине"
    )
