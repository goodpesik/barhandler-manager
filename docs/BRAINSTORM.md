# Brainstorm — повний контекст прийнятих рішень

Цей документ фіксує всі рішення прийняті під час brainstorm-сесії. Агент який буде реалізовувати проект має прочитати його повністю перед початком роботи.

---

## Що це за проект

**Barhandler Manager** — локальний HTTP-сервер на `localhost:9999`. Запускається на машині клієнта (кафе, фітнес-клуб). Приймає команди від веб-застосунків через REST API і керує фізичним залізом: термо-принтер чеків, label-принтер (пізніше), POS-термінал (пізніше).

**Чому існує:** BarHandler, FitStudio і майбутні продукти — всі потребують принтера чеків і POS-терміналу. Замість окремого драйвера в кожному продукті — одна платформа для всіх.

**Інтерфейс:** тільки API. Ніякого UI в цьому проекті — UI живе у веб-застосунках.

---

## Конкретний принтер (Phase 1)

**Модель:** Термопринтер E 582 R  
**Ширина паперу:** 58mm  
**Протокол:** ESC/POS (стандартний)  
**Підключення:** USB (основне) + LAN/Ethernet (опціонально)  
**Касовий ящик:** є порт, підтримується

---

## Всі прийняті рішення

### Мова і стек
- **Python** (не Node.js) — зрілі бібліотеки для USB/ESC/POS (`pyusb`, `python-escpos`), Node.js порти нестабільні
- **FastAPI + uvicorn** — async, легкий, гарна валідація через Pydantic
- **pyyaml** — конфіг
- **pydantic v2** — моделі запитів

### Авторизація
- Статичний `X-Api-Key` в HTTP-хедері
- `GET /health` — БЕЗ авторизації (веб-застосунок пінгує без ключа)
- Ключ зберігається в `config.yaml`

### Конфіг (`config.yaml`)
- YAML-файл з USB vendor/product ID, paper width, тип з'єднання
- Підтримує два типи connection для принтера: `usb` і `network` (host + port)
- Один запис на тип пристрою (receipt, label, terminal)

### Payload формат
- **Структурований JSON** — веб-застосунок передає дані (items, total, footer)
- Сервер сам рендерить в ESC/POS — веб-застосунок не знає нічого про протокол принтера
- `header` і `footer` передає веб-застосунок (кожен продукт має свій брендинг)

### Health check і моніторинг
- `GET /health` повертає статус сервера + статус кожного пристрою
- Веб-застосунок матиме toggle "Увімкнути Device Manager" в Settings
- При увімкненому toggle: пінгує `/health` при логіні і кожні 30 хвилин
- Якщо менеджер не відповідає — показує error modal

### Черга
- **FIFO черга** (`asyncio.Queue`) на кожен пристрій
- Відповідь клієнту ПІСЛЯ фізичного друку (синхронно з точки зору HTTP)
- Захищає від подвійного натискання і паралельних запитів

### Касовий ящик
- **Окремий ендпоінт** `POST /drawer/open` + опціональний прапор `"open_drawer": true` в payload чеку
- Якщо ящика немає — **graceful, не падає**, тихо ігнорує

### POS термінал (Phase 2)
- Monobank еквайєр
- Підключення: **Ethernet/WiFi** (не USB)
- Протокол: TBD (вивчити Monobank acquiring API)
- Зараз: заглушка `POST /terminal/charge` → `{ "status": "not_implemented" }`

### Label принтер (Phase 2)
- Конкретна модель не вибрана
- Протокол: TSPL або ZPL (залежить від моделі)
- Зараз: `enabled: false` в конфізі

### Пакування (Phase 3)
- **PyInstaller** → Windows `.exe` + macOS `.dmg`
- Основний виклик: `libusb` (C-бібліотека) треба бандлити
- Windows: потрібен **Zadig** для WinUSB драйвера (одноразово)
- macOS: Apple Silicon vs Intel — окремі білди або universal binary
- Крос-платформенність через `libusb` реальна — GPT помилявся про Mac

---

## Повний API

```
GET  /health              — без авторизації, статус сервера + пристроїв
GET  /devices             — список USB пристроїв (для налаштування)
GET  /devices/scan        — ресканувати USB
POST /print/receipt       — надрукувати чек
POST /drawer/open         — відкрити касовий ящик
POST /terminal/charge     — POS платіж (Phase 2, зараз заглушка)
```

---

## Приклади запитів

### POST /print/receipt
```json
{
  "header": "BarHandler — Кафе Ромашка",
  "items": [
    { "name": "Бурбон Jack Daniel's", "qty": 2, "price": 120.0 },
    { "name": "Реберця", "qty": 1, "price": 185.0 }
  ],
  "total": 425.0,
  "payment": "cash",
  "footer": "Дякуємо за візит!",
  "open_drawer": true
}
```

### GET /health response
```json
{
  "status": "ok",
  "version": "0.1.0",
  "devices": {
    "receipt": "connected",
    "label": "not_configured",
    "terminal": "not_configured"
  }
}
```

---

## Структура проекту

```
barhandler-manager/
├── main.py                  # entry point
├── config.yaml              # конфіг пристроїв
├── requirements.txt
└── src/
    ├── config.py            # завантаження config.yaml
    ├── server.py            # FastAPI app + auth middleware
    ├── routes/
    │   ├── health.py        # GET /health
    │   ├── devices.py       # GET /devices, /scan
    │   ├── print.py         # POST /print/receipt
    │   └── drawer.py        # POST /drawer/open
    ├── services/
    │   └── receipt.py       # JSON → ESC/POS рендеринг
    ├── devices/
    │   └── printer.py       # USB/LAN + asyncio FIFO queue
    └── models/
        └── receipt.py       # Pydantic моделі
```
