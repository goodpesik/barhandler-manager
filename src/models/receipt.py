from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ReceiptItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qty: int = Field(ge=1)
    price: float = Field(ge=0)


class ReceiptPayload(BaseModel):
    header: Optional[str] = None
    items: list[ReceiptItem] = Field(min_length=1)
    total: float = Field(ge=0)
    payment: str = "cash"   # "cash" | "card" | "acquiring" — unknown values are printed verbatim
    footer: Optional[str] = None
    open_drawer: bool = False

    @field_validator("payment")
    @classmethod
    def _normalise_payment(cls, v: str) -> str:
        return v.strip().lower() or "cash"
