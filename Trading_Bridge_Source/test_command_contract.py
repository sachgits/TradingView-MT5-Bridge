"""Smoke tests for the bridge command contract. Run from Trading_Bridge_Source."""
import unittest

from command_schema import normalize_command
from command_validator import validate_command


class CommandContractTests(unittest.TestCase):
    def assert_valid(self, payload):
        command = normalize_command(payload)
        self.assertNotIn("error", command)
        result = validate_command(command)
        self.assertTrue(result["valid"], result["errors"])

    def test_legacy_signal(self):
        self.assert_valid({"signal": "BUY SL=50 TP=100 LOT=0.01"})

    def test_minimal_structured_command(self):
        self.assert_valid({"symbol": "EURUSD", "action": "buy", "size": 0.1})

    def test_rich_atr_command(self):
        self.assert_valid({
            "schema_version": "2.0",
            "symbol": "XAUUSD",
            "action": "sell",
            "size": {"value": 0.05, "unit": "lots"},
            "stop_loss": {"unit": "atr", "period": 14, "multiplier": 2},
            "take_profits": [
                {"distance": {"unit": "price_distance", "value": 2}, "close_percent": 50},
                {"distance": {"unit": "price_distance", "value": 4}, "close_percent": 50},
            ],
        })

    def test_incomplete_entry_is_rejected_for_live_queue(self):
        command = normalize_command({"action": "buy", "symbol": "EURUSD"})
        result = validate_command(command)
        self.assertFalse(result["valid"])

    def test_selective_close(self):
        self.assert_valid({"action": "close_position", "position_ticket": "123"})


if __name__ == "__main__":
    unittest.main()
