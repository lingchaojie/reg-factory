from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CardType(str, Enum):
    NEXACARD_B = "NexaCardB"
    THREE_D_1 = "3D-1卡"


@dataclass(frozen=True)
class LookupInput:
    card_number: str
    card_type: CardType
    order_created_at: datetime


@dataclass(frozen=True)
class OtpRow:
    record_id: int
    otp: str
    card_number: str
    created_at: datetime


@dataclass(frozen=True)
class AuthStatus:
    state: str
    message: str
    authorized_email: str | None = None
    estimated_expires_at: datetime | None = None
    estimated: bool = False
