"""PET-921 — QR закладу на ЗВИЧАЙНОМУ (нефіскальному) чеку.

Фіскальний чек малює менеджер сам і знає, де в ньому бренди — вони приходять
окремим полем. Звичайний приходить готовим переліком рядків, і бренд у ньому
просто останній. Тому код, доданий у кінець, друкувався б ПІД «petshandler»,
хоч власник просив над ним — саме це й знайшло ревʼю.

Каса тепер надсилає бренд окремо (`tail_lines`), а тут перевіряємо, що менеджер
справді кладе його ПІСЛЯ коду. Тест ходить через справжній HTTP-маршрут, бо
вада жила саме в порядку всередині нього.

Порядок ловимо на виклику `_print_qr`, а не на байтах картинки. На TSPL шар
заліза складає і рядки, і коди в один високий бітмап (`_finalize_tspl`) — саме
в тому порядку, в якому їх кликали, тож черговість там не втрачається, просто
її не видно окремими викликами. Тому беремо ESC/POS: на ньому кожен крок іде
на залізо окремо, і порядок видно прямо.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from src.devices.printer import PrinterDevice
from src.models.printer import (
    PrinterDescriptor,
    PrinterTransport,
    UsbAddress,
    make_id,
)
from src.server import create_app
import src.routes.print_routes as print_routes

BOT = "https://t.me/petshandler_clients_bot?start=a3f91c"


class RecordingEscpos:
    """Записує все, що пішло б на залізо, зберігаючи ПОРЯДОК."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def text(self, value: str) -> None:
        self.calls.append(("text", value))

    def set(self, **_kw) -> None:
        pass

    def cut(self, **_kw) -> None:
        pass

    def close(self) -> None:
        pass

    def _raw(self, *_a, **_kw) -> None:
        self.calls.append(("image", None))

    def _bh_emit_image(self, image: Image.Image) -> None:
        self.calls.append(("image", image))

    def mark(self, what: str) -> None:
        self.calls.append(("mark", what))

    def order(self) -> list[str]:
        out: list[str] = []
        for kind, value in self.calls:
            out.append("QR" if kind == "mark" else str(value).strip())
        return [x for x in out if x]


def _descriptor() -> PrinterDescriptor:
    return PrinterDescriptor(
        id=make_id(PrinterTransport.usb, "0456", "0808", ""),
        transport=PrinterTransport.usb,
        label="STMicro POS Printer",
        usb=UsbAddress(vendor_id=0x0456, product_id=0x0808, in_ep=0x81, out_ep=0x03),
    )


def _print(config: dict, auth_headers: dict, payload: dict) -> RecordingEscpos:
    descriptor = _descriptor()
    fake = RecordingEscpos()
    real_print_qr = print_routes._print_qr

    def spy(printer, data: str, width: int) -> None:
        printer.mark("QR")
        real_print_qr(printer, data, width)

    with patch("src.devices.scan.discover_usb", return_value=[descriptor]), patch(
        "src.devices.scan.discover_network", return_value=[]
    ), patch("src.devices.scan.discover_bluetooth", return_value=[]), patch.object(
        print_routes, "_print_qr", spy
    ), patch.object(PrinterDevice, "_build_printer", lambda _self: fake):
        app = create_app(config)
        with TestClient(app) as client:
            client.post("/devices/discover", headers=auth_headers)
            client.post(
                "/devices/register",
                headers=auth_headers,
                json={
                    "id": descriptor.id,
                    "kind": "receipt",
                    "render_mode": "native",
                    # ESC/POS: кожен крок іде на залізо окремим викликом, тож
                    # порядок видно прямо. На TSPL він теж зберігається, але
                    # всередині одного склеєного бітмапа — там його довелось би
                    # вичитувати з пікселів.
                    "protocol": "escpos",
                },
            )
            res = client.post("/print/lines", headers=auth_headers, json=payload)
            assert res.status_code == 200, res.text
    return fake


def test_promo_qr_is_printed_before_the_brand(config, auth_headers):
    fake = _print(
        config,
        auth_headers,
        {
            "lines": [{"text": "СУМА 260.95"}, {"text": "ДЯКУЄМО ЗА ПОКУПКУ"}],
            "qr": BOT,
            "qr_caption": "Підписатися на новини",
            "tail_lines": [{"text": "petshandler", "align": "center"}],
        },
    )

    order = fake.order()
    assert "QR" in order, f"коду немає взагалі: {order}"
    assert order.index("Підписатися на новини") < order.index("QR")
    assert order.index("QR") < order.index("petshandler"), (
        f"код надрукувався ПІД брендом: {order}"
    )


def test_receipt_without_a_qr_is_printed_exactly_as_before(config, auth_headers):
    fake = _print(
        config,
        auth_headers,
        {"lines": [{"text": "СУМА 260.95"}, {"text": "petshandler"}]},
    )

    order = fake.order()
    assert "QR" not in order
    assert order[-1] == "petshandler"
