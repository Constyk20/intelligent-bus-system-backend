from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PassengerCreate(BaseModel):
    full_name: str = Field(..., min_length=2)
    phone: str = Field(..., min_length=7)
    terminal_id: str
    gender: Optional[str] = None
    address: Optional[str] = None


class PassengerResponse(BaseModel):
    id: str
    full_name: str
    phone: str
    terminal_id: str
    gender: Optional[str] = None
    address: Optional[str] = None
    is_active: bool
    created_at: datetime


class CardIssue(BaseModel):
    passenger_id: str
    initial_balance: str = "0.00"
    rfid_uid: Optional[str] = None


class CardResponse(BaseModel):
    id: str
    card_number: str
    passenger_id: str
    passenger_name: Optional[str] = None
    rfid_uid: Optional[str] = None
    balance: str
    status: Literal["active", "blocked", "inactive"]
    issued_at: datetime


class CardStatusUpdate(BaseModel):
    status: Literal["active", "blocked", "inactive"]


class WalletTopUp(BaseModel):
    amount: str = Field(..., description="Decimal string, e.g. 2000.00")
    note: Optional[str] = None


class TransactionResponse(BaseModel):
    id: str
    reference: str
    card_id: str
    passenger_id: str
    type: Literal["top_up", "debit", "refund"]
    amount: str
    balance_before: str
    balance_after: str
    note: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None