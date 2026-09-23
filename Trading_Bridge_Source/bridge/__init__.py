"""TradingBridge - signal intake, dispatch, and observability for Bot_Trader.

The bridge is a local relay between signal producers (a Chrome extension,
a file drop folder, or any HTTP client) and the Bot_Trader MT5 expert
advisor. It validates commands against the EA's real capabilities,
dispatches them one at a time, records every outcome, and serves a web
signal board.

`contract` is the single source of truth for the wire protocol. Validation,
the dashboard, and the documentation all derive from it, so adding an EA
capability means editing that one module.
"""

EA_VERSION = "6.5"
SCHEMA_VERSION = "2.1"

__all__ = ["EA_VERSION", "SCHEMA_VERSION"]
