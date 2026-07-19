import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import InvalidLookupInput
from .models import CardType, LookupInput, OtpRow


ROUTES = {
    CardType.NEXACARD_B: "/nova-v-card-b/verify-code",
    CardType.THREE_D_1: "/3d-1-card/verify-code",
}


def normalize_card_type(value: str) -> CardType:
    key = re.sub(r"[\s_\-卡]+", "", value).lower()
    aliases = {
        "nexacardb": CardType.NEXACARD_B,
        "b": CardType.NEXACARD_B,
        "3d1": CardType.THREE_D_1,
        "3done": CardType.THREE_D_1,
    }
    try:
        return aliases[key]
    except KeyError as exc:
        raise InvalidLookupInput("unsupported card_type") from exc


def normalize_card_number(value: str) -> str:
    normalized = re.sub(r"[\s-]", "", value)
    if not re.fullmatch(r"[0-9]{12,19}", normalized):
        raise InvalidLookupInput("card_number must contain 12 to 19 digits")
    return normalized


def parse_order_time(value: str, timezone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidLookupInput("order_created_at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def parse_lookup_input(card_number: str, card_type: str, order_created_at: str, timezone: ZoneInfo) -> LookupInput:
    return LookupInput(
        card_number=normalize_card_number(card_number),
        card_type=normalize_card_type(card_type),
        order_created_at=parse_order_time(order_created_at, timezone),
    )


def route_for(card_type: CardType) -> str:
    return ROUTES[card_type]


def select_nearest_otp(rows: list[OtpRow], lookup: LookupInput) -> OtpRow | None:
    candidates = [
        row
        for row in rows
        if row.card_number == lookup.card_number and row.created_at > lookup.order_created_at
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row.created_at - lookup.order_created_at, -row.record_id))
