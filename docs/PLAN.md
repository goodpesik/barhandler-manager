# Implementation Plan — Phase 1

Цей план для агента який буде реалізовувати проект. Читай BRAINSTORM.md і spec перед початком.

**Scope Phase 1:** HTTP сервер + ESC/POS чек-принтер (USB і LAN) + касовий ящик.

---

## Крок 1 — Залежності і конфіг

- [ ] Встановити залежності: `pip install -r requirements.txt`
- [ ] Перевірити що `pyusb` бачить USB пристрої: `python -c "import usb.core; print(list(usb.core.find(find_all=True)))"`
- [ ] Заповнити `config.yaml`: знайти vendor_id/product_id принтера через scan

## Крок 2 — Core сервер

- [ ] `src/config.py` — завантаження `config.yaml`
- [ ] `src/server.py` — FastAPI app, auth middleware (`X-Api-Key`), реєстрація роутерів
- [ ] `main.py` — запуск uvicorn на порту з конфігу

## Крок 3 — Health endpoint

- [ ] `src/routes/health.py` — `GET /health` без авторизації
- [ ] Повертає статус сервера + статус кожного пристрою (`connected` / `not_configured` / `unavailable`)

## Крок 4 — Device layer

- [ ] `src/devices/printer.py` — клас `PrinterDevice`:
  - підключення через USB (`pyusb`) або мережу (socket TCP port 9100)
  - `asyncio.Queue` для FIFO черги
  - методи: `connect()`, `is_connected()`, `enqueue(job)`, `open_drawer()`
  - graceful: якщо принтер не підключений — `is_connected()` повертає False, не кидає exception

## Крок 5 — Receipt service

- [ ] `src/models/receipt.py` — Pydantic моделі (`ReceiptItem`, `ReceiptPayload`)
- [ ] `src/services/receipt.py` — рендеринг JSON → ESC/POS байти через `python-escpos`:
  - header (якщо є) — жирний, по центру
  - items — назва ліворуч, ціна×кількість праворуч, 58mm = 32 символи на рядок
  - роздільник
  - total — жирний
  - payment type (Готівка / Картка)
  - footer (якщо є) — по центру
  - cut

## Крок 6 — Print route

- [ ] `src/routes/print.py` — `POST /print/receipt`:
  - валідація payload (Pydantic)
  - якщо принтер недоступний → 503
  - рендер через receipt service
  - enqueue job → чекаємо результату
  - якщо `open_drawer: true` → відкрити ящик після друку
  - відповідь `{ "status": "printed" }`

## Крок 7 — Drawer route

- [ ] `src/routes/drawer.py` — `POST /drawer/open`:
  - викликає `printer.open_drawer()`
  - якщо ящика немає — тихо повертає `{ "status": "opened" }`

## Крок 8 — Devices route

- [ ] `src/routes/devices.py` — `GET /devices` і `GET /devices/scan`:
  - сканує USB через `usb.core.find(find_all=True)`
  - повертає список з vendor_id, product_id

## Крок 9 — Terminal stub

- [ ] `src/routes/terminal.py` — `POST /terminal/charge`:
  - повертає `{ "status": "not_implemented" }`

## Крок 10 — Тестування

- [ ] Запустити: `python main.py`
- [ ] `GET http://localhost:9999/health` — має відповісти
- [ ] `GET http://localhost:9999/devices` — має показати принтер
- [ ] `POST http://localhost:9999/print/receipt` з тестовим payload — має надрукувати

---

## Важливі нюанси

- На Linux (Raspberry Pi): може знадобитись `sudo` або udev rule для USB доступу без sudo
  ```bash
  # /etc/udev/rules.d/99-printer.rules
  SUBSYSTEM=="usb", ATTR{idVendor}=="XXXX", ATTR{idProduct}=="YYYY", MODE="0666"
  ```
- ESC/POS для 58mm: 32 символи на рядок в стандартному шрифті
- LAN принтер: стандартний raw port **9100** (більшість ESC/POS принтерів)
- Не використовувати системний Python на Mac — краще venv
