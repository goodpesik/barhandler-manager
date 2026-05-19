from pydantic import BaseModel
from typing import Optional

class ReceiptItem(BaseModel):
    name: str
    qty: int
    price: float

class ReceiptPayload(BaseModel):
    header: Optional[str] = None
    items: list[ReceiptItem]
    total: float
    payment: str        # "cash" | "card"
    footer: Optional[str] = None
