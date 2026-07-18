import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from nexacard_otp.errors import InvalidLookupInput
from nexacard_otp.matching import parse_lookup_input, route_for, select_nearest_otp
from nexacard_otp.models import CardType, OtpRow


class NexaCardMatchingTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo("Asia/Shanghai")

    def test_aliases_map_to_confirmed_routes(self):
        b = parse_lookup_input("6500-0000-0000-0037", "nexacardb", "2026-07-19 03:00:00", self.zone)
        three_d = parse_lookup_input("6500 0000 0000 0037", "3d-1", "2026-07-19 03:00:00", self.zone)

        self.assertEqual(b.card_number, "6500000000000037")
        self.assertEqual(route_for(b.card_type), "/nova-v-card-b/verify-code")
        self.assertEqual(route_for(three_d.card_type), "/3d-1-card/verify-code")

    def test_naive_order_time_uses_page_timezone(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-19 03:00:00", self.zone)

        self.assertEqual(lookup.order_created_at.utcoffset().total_seconds(), 28800)

    def test_aware_order_time_converts_to_page_timezone(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-18T19:00:00Z", self.zone)

        self.assertEqual(lookup.order_created_at.hour, 3)

    def test_invalid_lookup_values_raise_invalid_lookup_input(self):
        for card_number, card_type, order_created_at in (
            ("6500", "NexaCardB", "2026-07-19 03:00:00"),
            ("6500000000000037", "unsupported", "2026-07-19 03:00:00"),
            ("6500000000000037", "NexaCardB", "not-a-time"),
        ):
            with self.subTest(card_number=card_number, card_type=card_type, order_created_at=order_created_at):
                with self.assertRaises(InvalidLookupInput):
                    parse_lookup_input(card_number, card_type, order_created_at, self.zone)

    def test_equal_time_is_rejected_and_nearest_strictly_later_row_wins(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-19 03:00:00", self.zone)
        rows = [
            OtpRow(1, "111111", "6500000000000037", datetime(2026, 7, 19, 3, 0, tzinfo=self.zone)),
            OtpRow(2, "222222", "6500000000000037", datetime(2026, 7, 19, 3, 0, 3, tzinfo=self.zone)),
            OtpRow(3, "333333", "6500000000000037", datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone)),
            OtpRow(4, "444444", "6500000000009999", datetime(2026, 7, 19, 3, 0, 0, 500000, tzinfo=self.zone)),
        ]

        self.assertEqual(select_nearest_otp(rows, lookup).otp, "333333")

    def test_same_timestamp_prefers_highest_record_id(self):
        lookup = parse_lookup_input("6500000000000037", "3D-1卡", "2026-07-19 03:00:00", self.zone)
        created = datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone)
        rows = [
            OtpRow(8, "888888", lookup.card_number, created),
            OtpRow(9, "999999", lookup.card_number, created),
        ]

        self.assertEqual(select_nearest_otp(rows, lookup).otp, "999999")

    def test_no_exact_later_card_match_returns_none(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-19 03:00:00", self.zone)
        rows = [
            OtpRow(1, "111111", "6500000000009999", datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone)),
            OtpRow(2, "222222", lookup.card_number, datetime(2026, 7, 19, 2, 59, 59, tzinfo=self.zone)),
        ]

        self.assertIsNone(select_nearest_otp(rows, lookup))
