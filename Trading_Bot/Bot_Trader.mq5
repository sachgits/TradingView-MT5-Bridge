//+---------------------------------------------------------------------+
//| TradingView to MT5 Bridge - Trading Bot v6.6                         |
//| Universal command processor                                          |
//|                                                                      |
//| Wire protocol (bridge envelope):                                     |
//|   GET  ServerURL       -> {"status":"NEW", "normalized":{...},       |
//|                             "signal":"{...}", ...}                   |
//|   POST ProcessedURL    -> acknowledge a processed signal            |
//| Command fields (action/symbol/size/sl/tp) are read from "normalized"|
//| or from the top level for raw payloads. Plain-text "signal" values  |
//| (e.g. "BUY EURUSD SL=50") are handled by the legacy parser.         |
//|                                                                      |
//| Actions (case-insensitive, aliases allowed):                        |
//|   buy sellebi  -> "buy" "long" "open_buy"                           |
//|   sell         -> "sell" "short" "open_sell"                        |
//|   close symbol -> "close" "flat" "cancel" "close_symbol"            |
//|   close all    -> "close_all" "flat_all"                            |
//|   partial close-> "partial_close": close_percent % (omit = full close)|
//|   modify SL/TP -> "modify" "set_sltp" (applied to first matching    |
//|                   open position of the symbol, magic-filtered)      |
//|   breakeven    -> "breakeven" "be" (SL to entry +- lock_in pts)     |
//|   no-op        -> "hold" "none" "wait" "noop" (acknowledged)        |
//|                                                                      |
//| Size modes (size.value + size.unit):                                |
//|   lots   : fixed lot size (default)                                 |
//|   percent: % of account balance used as notional (approx)           |
//|   risk   : risk_percent % of balance risked, derived from SL pts    |
//|   money  : account-currency amount converted to lots (approx)       |
//| Risk inputs clamp the result; sizing is documented approximation.   |
//|                                                                      |
//| Resilience: symbol allow-list + availability checks, trade-allowed  |
//| and market-mode guards, lot normalization, duplicate-signal dedup   |
//| and a stale-aware startup (a fresh pending signal is processed      |
//| instead of being cleared).                                          |
//| v6.2a: locale-proof signal aging (no StringToTime) and a throttled  |
//|        journal note when a pending signal is skipped.               |
//| v6.2b: heartbeat liveness log, timer watchdog re-arm, and a clear   |
//|        offline-vs-disabled message in the trade guard.              |
//| v6.3:  posts the outcome of every processed signal back to the       |
//|        bridge (request_id, action, symbol, size, retcode, result)  |
//|        and appends the same line to a local results log file.       |
//| v6.4:  scenario & risk management: action aliases + new actions,    |
//| v6.5:  account snapshot & pending-order engine.                     |
//| v6.6:  trailing-stop action, cash-risk sizing, ticket targeting,    |
//|        exact partial-close volume, order deviation/comment, and a   |
//|        richer processed-result body.                               |
//|   NEW MANAGEMENT ACTION:                                           |
//|     {"action":"trailing","symbol":"EURUSD",                        |
//|      "trailing":{"distance":50,"activation":20}}                   |
//|     (aliases: trail, trailing_stop, update_trailing). Targets a    |
//|     position by symbol+magic, or by "position_ticket". distance is |
//|     the SL trail behind market; activation is the profit needed    |
//|     before trailing arms. Both take the standard distance schema.  |
//|   CASH-RISK SIZING:                                                |
//|     "size":{"unit":"risk_money","value":150} risks 150 in account  |
//|     currency, converted to lots from the stop_loss distance and    |
//|     the symbol tick value (aliases: risk_cash, risk_amount,        |
//|     risk_usd). Requires a stop_loss; the broker only accepts lots, |
//|     so the conversion happens here before OrderSend.               |
//|   TICKET TARGETING & EXACT VOLUME:                                 |
//|     "position_ticket" (alias position_id) aims close_position,     |
//|     modify, breakeven and partial_close at one position instead of |
//|     every position on the symbol. partial_close accepts            |
//|     "close_volume" (alias volume) for an exact lot size, which     |
//|     takes priority over close_percent.                            |
//|   ORDER METADATA: "deviation" (max slippage in points) and         |
//|     "comment" are passed through to OrderSend.                     |
//|   RICHER RESULT BODY: an execution ack now also carries            |
//|     "position_ticket", "deal_ticket", "filled_price" and           |
//|     "filled_volume" (alongside "order_ticket" for pendings), so    |
//|     the bridge can reconcile a fill against the broker's objects.  |
//|   NEW local guard retcode: -50 unparseable command or invalid      |
//|     symbol, rejected locally before any OrderSend.                 |
//|        (end of v6.6 notes; v6.5 pending/account detail below.)     |
//|                                                                     |
//|   NEW READ-ONLY ACTION (works even when trading is disabled):       |
//|     {"action":"account"}   (aliases: status, snapshot, info)        |
//|   The processed-result POST gains a "snapshot" object:              |
//|     account, name, server, company, currency, margin_mode,          |
//|     trade_mode, leverage, balance, equity, margin, margin_free,     |
//|     margin_level, credit, profit, trade_allowed, positions[],       |
//|     pending_orders[]. Use it to answer "what is my balance, equity  |
//|     and are there open positions / pending orders?" in one call.    |
//|                                                                     |
//|   NEW PENDING-ORDER ACTIONS (price required, absolute by default):  |
//|     {"action":"buy_limit","symbol":"EURUSD","size":0.10,            |
//|      "price":1.1000}                                                |
//|     {"action":"sell_stop","symbol":"EURUSD","size":0.10,            |
//|      "price":1.1350,"stop_loss":30,"take_profit":60}                |
//|     {"action":"buy_stop_limit","symbol":"GOLD","size":0.01,         |
//|      "price":4200.00,"stoplimit":4190.00}                           |
//|     {"action":"pending","order_type":"sell_limit","symbol":"XAUUSD",|
//|      "size":0.10,"price":4160.00}                                   |
//|     {"action":"modify_pending","order_ticket":987654,               |
//|      "price":1.1050,"expiration":"2026.10.31 18:00"}                |
//|     {"action":"cancel_pending","order_ticket":987654}               |
//|     {"action":"cancel_pending","symbol":"*"}   (* = all symbols)    |
//|   Aliases: buy_stop, sell_limit, buy_stop_limit, sell_stop_limit,   |
//|   cancel_order, delete_order, delete_pending, modify_order,         |
//|   update_order, update_pending.                                     |
//|   Pricing extras:                                                   |
//|     "price_unit":"points"  -> price is a SIGNED distance from the   |
//|         current market (+ = above, - = below; buy uses Ask, sell    |
//|         uses Bid). For stop-limit orders "stoplimit" is the inner   |
//|         limit price and uses the same unit.                         |
//|     "expiration":"YYYY.MM.DD HH:MM" or ISO "YYYY-MM-DDTHH:MM:SS"   |
//|         -> order expires (ORDER_TIME_SPECIFIED).                    |
//|     SL/TP use the standard stop_loss/take_profit schema and are     |
//|         measured from the pending order price.                      |
//|   Pending orders are owned by MagicNumber; cancel/modify only       |
//|   touch orders whose magic matches (same policy as positions).      |
//|   NEW local guard retcodes: -40 invalid pending params,             |
//|   -41 no matching pending order, -42 market closed / no quote,      |
//|   -43 unparseable expiration. The result body also carries          |
//|   "order_ticket" when a pending order is placed/modified.           |
//|        (end of v6.5 notes; v6.4 sizing/exposure detail and the full |
//|        broker reference live in BROKER_CAPABILITIES.md).            |
//+---------------------------------------------------------------------+
//| SIGNAL SENDER GUIDE - READ BEFORE SENDING COMMANDS                  |
//+---------------------------------------------------------------------+
//
// WHAT THIS BOT IS
//   A TradingView -> MetaTrader 5 bridge command processor. It polls a
//   local HTTP bridge every PollIntervalMs and executes the latest command
//   against the MT5 account. It runs in UNIVERSAL SYMBOL MODE: attach it to
//   ONE chart and signals for ANY symbol are still handled.
//
//   Two byte-identical copies of this program exist:
//     MQL5\Experts\Bot_Trader\Bot_Trader.mq5
//     MQL5\Experts\Trading_Bot\Trading_Bot.mq5
//   They are the same bot. Run only ONE instance so a signal is not
//   processed twice; keep both files updated together.
//
// HOW A SIGNAL REACHES THIS BOT (what a signal sender must provide)
//   1. Place the latest command JSON where the bot can GET it: the bridge
//      endpoint ServerURL (default http://127.0.0.1:8080/signal).
//   2. The bot polls ServerURL. A signal whose envelope status is "NEW" is
//      parsed, executed, then acknowledged via POST to ProcessedURL
//      (default http://127.0.0.1:8080/signal/processed).
//   3. Duplicate protection: a payload identical to the last processed one,
//      or one whose status is not "NEW", is skipped (and logged, throttled).
//
// BRIDGE ENVELOPE (what GET ServerURL returns)
//   {
//     "status": "NEW",                       // NEW = process; PROCESSED = idle
//     "normalized": { ...command fields... },// preferred command source
//     "signal": "{...}",                     // legacy plain-text detector only
//     "timestamp": "2026-09-20T14:00:00Z",   // echoed back in the result
//     "request_id": "demo-fx-001",           // echoed back in the result
//     "source_file": "signals/demo-fx-001.json"
//   }
//   Command-field precedence: "normalized" object > payload top level >
//   legacy plain text (a "signal" value without "{").
//   If action/symbol/size are missing from steps 2-3, the parser falls
//   back to reading them from the JSON inside "signal" before defaults.
//
// COMMAND FIELDS (top level or inside "normalized")
//   action        string   required
//   symbol        string   optional, defaults to the chart symbol
//   size          number|object   optional, defaults to the LotSize input
//   lot / value   number   aliases for a plain lot size
//   risk_percent  number   % of balance to risk in "risk" size mode
//   stop_loss (sl) / take_profit (tp)   number|string|object
//   sl_distance / tp_distance  number   fallback distances in points
//   close_percent number   % of position closed by partial_close (omitted
//                        = full close; an explicit value <= 0 = 50%)
//   breakeven     object   optional SL management (see below)
//   trailing      object   parsed and logged only in this version
//   Unknown fields are ignored.
//
// ACTIONS (the events this bot reacts to; case-insensitive)
//   buy            buy | long | open_buy | openlong | buy_now
//   sell           sell | short | open_sell | openshort | sell_now
//   close          close | close_symbol | flat | cancel
//   close_all      close_all | flat_all | close_everything
//   partial_close  partial_close | close_percent | scale_out
//   modify         modify | modify_stop | set_sltp | update_sltp
//   breakeven      breakeven | be | move_be | break_even
//   noop           hold | hold_position | none | noop | wait | trend | wait_for
//   Any other action is reported as SKIPPED (retcode -13).
//
// EXAMPLE COMMANDS (JSON a signal sender can push)
//   Simple buy with points SL/TP:
//   { "action": "buy", "symbol": "EURUSD", "size": 0.01,
//     "stop_loss": { "type": "distance", "unit": "points", "value": 200 },
//     "take_profit": { "type": "distance", "unit": "points", "value": 400 } }
//   Sell any symbol:
//   { "action": "sell", "symbol": "XAUUSD", "size": 0.05,
//     "stop_loss": 500, "take_profit": 1000 }
//   Close one symbol / close everything / partial close:
//   { "action": "close", "symbol": "EURUSD" }
//   { "action": "close_all" }
//   { "action": "partial_close", "symbol": "EURUSD", "close_percent": 50 }
//   Modify SL/TP of the open position:
//   { "action": "modify", "symbol": "EURUSD",
//     "stop_loss": { "unit": "points", "value": 250 },
//     "take_profit": { "unit": "points", "value": 500 } }
//   Breakeven (SL to entry +- lock-in):
//   { "action": "breakeven", "symbol": "EURUSD" }
//   No-op / hold:
//   { "action": "hold", "symbol": "EURUSD" }
//
// SIZE MODES ("size" as a number, or as an object with "unit")
//   lots     fixed lots, { "value": 0.02, "unit": "lots" } (default)
//   percent  % of balance used as notional, { "unit": "percent", "value": 1.5 }
//   risk     risk_percent % of balance risked if SL is hit,
//            { "unit": "risk", "value": 1.0 } + "risk_percent": 1.0
//   money    account-currency cash amount converted to lots,
//            { "unit": "money", "value": 100 } ("currency" is an alias)
//   Size is normalized to the symbol lot step/min/max and capped by
//   MaxExposurePerSymbol and MaxOpenRiskPercent; margin is sanity-checked
//   (insufficient margin/bad size -> retcode -20). Sizing is a documented
//   approximation, not exact risk math.
//
// STOP LOSS / TAKE PROFIT FORMATS
//   number       { "stop_loss": 200 }
//   string       { "sl": "200" }
//   object       { "stop_loss": { "type": "distance", "unit": "points",
//                                 "value": 200 } }
//   "value" may also be spelled "distance".
//   unit = points          distance in terminal points (SYMBOL_POINT)
//   unit = price_distance  absolute price gap converted using SYMBOL_POINT
//   unit = atr             ATR multiple; optional "period" (default 14) and
//                          "multiplier" (default 1.0); "timeframe" is parsed
//                          but the ATR calculation currently always uses M15
//   Distances are applied from the market price: buy SL = Ask - pts*point,
//   buy TP = Ask + pts*point; sell SL = Bid + pts*point, sell TP = Bid - pts*point.
//   "modify" measures distances from the position open price.
//   Distances below MinStopDistancePoints are raised to that minimum.
//
// BREAKEVEN / TRAILING
//   breakeven  "lock_in" controls how far past entry the SL is placed
//              (default 20 points); "trigger" is parsed but not yet enforced,
//              so the SL is moved immediately when the breakeven action comes.
//   trailing   accepted by the parser and logged, but NOT executed in this
//              version (reserved for a future release).
//
// LEGACY PLAIN-TEXT SIGNALS (only when "signal" contains no "{")
//   "BUY EURUSD SL=50 TP=100 LOT=0.02"
//   - only buy/sell are recognised (SELL/SHORT keyword => sell)
//   - the symbol is ALWAYS the chart symbol
//   - SL=, TP=, LOT= tokens are optional; their values default to inputs.
//
// WHAT HAPPENS AFTER EXECUTION (feedback to the sender)
//   Every processed signal is acknowledged with a POST to ProcessedURL whose
//   JSON body describes the outcome:
//   { "request_id": "...", "timestamp": "...", "action": "buy",
//     "symbol": "EURUSD", "size": 0.01, "status": "SUCCESS",
//     "retcode": 0, "result": "BUY OPENED" }
//   status =
//     SUCCESS retcode == 0  (e.g. "BUY OPENED", "CLOSED EURUSD")
//     FAILED  retcode > 0   broker retcode (e.g. 10018 market closed,
//                            10014 invalid volume, 10016 invalid stops)
//     SKIPPED retcode < 0   local guard (see list below)
//   Local guard retcodes (signal skipped before reaching the broker):
//     -10 terminal offline     -11 trading disabled in terminal/account/EA
//     -12 MaxPositions reached -13 unknown action
//     -14/-15 market mode != FULL for buy/sell
//     -20 risk sizing: margin/volume problem
//     -30/-31/-32 no matching open position for partial close/modify/breakeven
//   The outcome is also appended, one pipe-separated line per signal, to the
//   ResultLogFile input (default MQL5\Files\TradingBridge_results.log).
//
// OPERATIONAL NOTES FOR SIGNAL SENDERS
//   - Run one bot instance; positions are owned by MagicNumber (default 12345).
//   - ServerURL/ProcessedURL must be whitelisted in the terminal:
//     Tools -> Options -> Expert Advisors -> Allow Web requests.
//   - "points" are SYMBOL_POINT units (0.0001 for most FX, 0.01 for JPY pairs
//     and most metals/indexes), NOT pips.
//   - On a closed market (weekend) a demo broker may answer retcode 10018;
//     the bot may also skip at guard -14/-15.
//   - On startup only signals older than StaleSignalMinutes are cleared;
//     a fresh pending signal is processed normally.
//   - Heartbeat journal lines ("Poll alive" / "Signal pending") appear every
//     HeartbeatLogSec (default 60 s) while polling works.
//   - Backtests do not prove live profitability; validate on a demo account.
#property copyright "Copyright 2025"
#property link      "https://github.com/sachgits/TradingView-MT5-Bridge"
#property version   "6.6"
#property strict

#include <Trade\Trade.mqh>

input double LotSize = 0.01;
input int TakeProfitPoints = 100;
input int StopLossPoints = 50;
input int MagicNumber = 12345;
input int MaxPositions = 5;
input int PollIntervalMs = 750;
input int StaleSignalMinutes = 10;
input int HeartbeatLogSec = 60; // 0 = disable the periodic liveness log
input double DefaultRiskPercent = 1.0;    // risk mode fallback: % of balance risked per trade
input double MaxOpenRiskPercent = 3.0;    // notional cap: % of balance per trade (approx)
input double MaxExposurePerSymbol = 0.0;  // lot cap per symbol (0 = no cap)
input int MinStopDistancePoints = 5;      // hard minimum SL/TP distance in points
input string ServerURL = "http://127.0.0.1:8080/signal";
input string ProcessedURL = "http://127.0.0.1:8080/signal/processed";
input string AllowedSymbols = ""; // empty = any symbol accepted, e.g. "EURUSD,GBPUSD,USDJPY"
input string ResultLogFile = "TradingBridge_results.log"; // "" = no local file log

CTrade trade;
uint lastCheckTime = 0;
uint lastHeartbeatTime = 0;
string lastProcessedSignal = "";
uint lastSkipLogTime = 0;
int lastTradeRetcode = 0;
string lastTradeResult = "PROCESSED";
string lastAccountSnapshot = "";   // v6.5: JSON body for "account" action
ulong lastTradeOrderTicket = 0;    // v6.5: ticket of last placed order
ulong lastTradePositionTicket = 0; // v6.6: ticket of opened/affected position
ulong lastTradeDealTicket = 0;     // v6.6: ticket of execution deal
double lastTradeFilledPrice = 0.0; // v6.6: execution fill price
double lastTradeFilledVolume = 0.0;// v6.6: execution fill volume

struct DistanceSpec
  {
   string            unit;
   double            value;
   int               period;
   string            timeframe;
   double            multiplier;
  };

struct TradeCommand
  {
   string            action;
   string            symbol;
   double            size;
   string            sizeUnit;
   double            riskPercent; double riskMoney;   // riskMoney: cash risk in account currency (size unit "risk_money")
   DistanceSpec      stopLoss;
   DistanceSpec      takeProfit;
   bool              hasBreakeven;
   DistanceSpec      breakevenTrigger;
   DistanceSpec      breakevenLockIn;
   bool              hasTrailing;
   DistanceSpec      trailingActivation;
   DistanceSpec      trailingDistance;
   bool              isLegacy;
   int               closePercent;
   //--- v6.5: pending-order engine fields
   string            orderType;      // canonical name: buy_limit, sell_stop, ...
   double            price;          // pending trigger price (or signed distance)
   double            stopLimit;      // inner limit price for stop-limit orders
   ulong             orderTicket;    // target pending order for cancel/modify
   string            priceUnit;      // "price" (absolute) or "points" (signed distance)
   string            expiration;     // "YYYY.MM.DD HH:MM" or ISO datetime
   bool              hasStopLoss;    // true when the payload really carried SL
   bool              hasTakeProfit;  // true when the payload really carried TP
   //--- v6.6: expanded capabilities
   ulong             positionTicket; // target position ticket for close/modify/breakeven/partial_close
   double            closeVolume;    // exact lots for partial close
   string            comment;        // order/deal comment
   int               deviation;      // max slippage in points
  };

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
// Poll on a wall-clock timer so signals for ANY symbol are caught,
// even on charts whose own symbol is quiet.
   EventSetTimer(MathMax(PollIntervalMs, 100));
   Print("✅ TradingBridge v6.6 Started (universal symbol mode)");
   Print("📡 Monitoring: ", ServerURL);
   ClearOldSignalsOnStartup();
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void ClearOldSignalsOnStartup()
  {
// Clear only genuinely old leftovers; a fresh pending signal must
// survive startup so it can be processed by the normal poll loop.
   string signalJson = "";
   string status = "";
   if(GetSignalFromServer(signalJson, status))
     {
      if(signalJson != "" && signalJson != "NONE")
        {
         if(IsSignalStale(signalJson, StaleSignalMinutes))
           {
            Print("⚠️ Old signal on server (older than ", StaleSignalMinutes, " min). Clearing it.");
            MarkSignalProcessed();
           }
         else
            Print("🟢 Fresh signal waiting - will be processed.");
        }
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnTimer()
  {
   EnsureTimerAlive();
   PollForSignals();
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnTick()
  {
   EnsureTimerAlive();
   PollForSignals();
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void EnsureTimerAlive()
  {
// Watchdog: if no poll has run for several poll intervals, the platform
// timer may have been suspended (e.g. weekend disconnect). Re-arm it
// from whatever event still fires (OnTick/OnTimer) so the EA resumes
// polling without a manual re-attach.
   uint now = GetTickCount();
   uint limit = (uint)MathMax(PollIntervalMs, 100) * 4;
   if(now - lastCheckTime > limit)
     {
      EventKillTimer();
      EventSetTimer(MathMax(PollIntervalMs, 100));
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void PollForSignals()
  {
   uint now = GetTickCount();
   if(now - lastCheckTime < (uint)PollIntervalMs)
      return;
   lastCheckTime = now;

   string signalJson = "";
   string status = "";
   bool awaiting = false;
   if(GetSignalFromServer(signalJson, status))
     {
      // Process a NEW signal once. lastProcessedSignal blocks re-executing
      // the same payload if the bridge repeats it.
      if(status == "NEW" && signalJson != "" && signalJson != "NONE" && signalJson != lastProcessedSignal)
        {
         TradeCommand cmd;
         if(ParseTradeCommand(signalJson, cmd))
           {
            Print("📊 Processing command: action=", cmd.action, " symbol=", cmd.symbol, " size=", cmd.size);
            lastProcessedSignal = signalJson;
            ExecuteTradeCommand(cmd);
            ReportSignalResult(signalJson, cmd);
           }
         else
           {
            // R2 fix: acknowledge parse / symbol rejection so we do not loop endlessly every poll
            Print("⚠️ Parse or symbol validation failed for signal. Acknowledging SKIPPED to prevent infinite loop.");
            lastProcessedSignal = signalJson;
            lastTradeRetcode = -50;
            lastTradeResult = "SKIPPED: parse or symbol validation rejected";
            ReportSignalResult(signalJson, cmd);
           }
        }
      else
        {
         // A real payload that we did not process (non-NEW status or a
         // duplicate of the last processed signal). Log it, throttled,
         // so a silently skipped signal becomes visible in the journal
         // without flooding it.
         if(signalJson != "" && signalJson != "NONE")
           {
            awaiting = true;
            uint skipNow = GetTickCount();
            if(skipNow - lastSkipLogTime > 5000)
              {
               lastSkipLogTime = skipNow;
               Print("ℹ️ Pending signal ignored (status=", status, ", duplicate=", (signalJson == lastProcessedSignal), ")");
              }
           }
        }
     }
// Heartbeat: proves the poll loop is alive even when idle. If the
// platform suspends timers (weekend disconnect), this line stops and
// the last one pinpoints exactly when polling died.
   if(HeartbeatLogSec > 0)
     {
      if(now - lastHeartbeatTime >= (uint)HeartbeatLogSec * 1000)
        {
         lastHeartbeatTime = now;
         if(awaiting)
            Print("⏳ Signal pending on bridge (status=", status, ") - will process when possible.");
         else
            Print("❤️ Poll alive - no signal.");
        }
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool GetSignalFromServer(string &signalJson, string &status)
  {
   char data[];
   char result[];
   string headers = "";
   int res = WebRequest("GET", ServerURL, "", NULL, 5000, data, 0, result, headers);
   if(res == 200)
     {
      signalJson = CharArrayToString(result);
      status = JsonGetString(signalJson, "status");
      if(status == "")
         status = "NEW";
      return true;
     }
   if(res == -1)
      Print("⚠️ WebRequest timeout - bridge unreachable: ", ServerURL);
   else
      if(res == 1001)
         Print("⛔ URL not allowed! Add ", ServerURL, " in Tools -> Options -> Expert Advisors -> Allow Web requests.");
      else
         Print("❌ Server responded: ", res);
   return false;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void MarkSignalProcessed()
  {
   char data[];
   char result[];
   string headers = "Content-Type: application/json\r\n";
   int res = WebRequest("POST", ProcessedURL, headers, NULL, 5000, data, 0, result, headers);
   if(res == -1)
      Print("⚠️ Could not mark signal as processed (timeout).");
   else
      if(res == 1001)
         Print("⛔ URL not allowed for processed-mark: ", ProcessedURL);
      else
         if(res != 200)
            Print("⚠️ Processed-mark response: ", res);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void ReportSignalResult(string jsonText, TradeCommand &cmd)
  {
// Send the outcome of this signal back to the bridge so the sender side
// can see exactly what happened with each request (debugging). The same
// POST to ProcessedURL doubles as the processed-ack; its JSON body
// carries the details. The outcome is also appended to a local log file
// so nothing is lost even if the bridge drops the body.
   string reqId = JsonGetString(jsonText, "request_id");
   string sigTime = JsonGetString(jsonText, "timestamp");
   string status = "SUCCESS";
   if(lastTradeRetcode > 0)
      status = "FAILED";
   if(lastTradeRetcode < 0)
      status = "SKIPPED";

   string body = "{\"request_id\":\"" + reqId +
                 "\",\"timestamp\":\"" + sigTime +
                 "\",\"action\":\"" + cmd.action +
                 "\",\"symbol\":\"" + cmd.symbol +
                 "\",\"size\":" + DoubleToString(cmd.size, 6) +
                 ",\"status\":\"" + status +
                 "\",\"retcode\":" + IntegerToString(lastTradeRetcode) +
                 ",\"result\":\"" + lastTradeResult + "\"";
// v6.5: optional extras - the ticket of a placed/modified pending order
// and the full account snapshot for "account" actions. Appended only
// when relevant so older consumers are not affected.
   if(lastTradeOrderTicket > 0)
      body += ",\"order_ticket\":" + IntegerToString(lastTradeOrderTicket);
   if(lastTradePositionTicket > 0)
      body += ",\"position_ticket\":" + IntegerToString(lastTradePositionTicket);
   if(lastTradeDealTicket > 0)
      body += ",\"deal_ticket\":" + IntegerToString(lastTradeDealTicket);
   if(lastTradeFilledPrice > 0.0)
      body += ",\"filled_price\":" + DoubleToString(lastTradeFilledPrice, 5);
   if(lastTradeFilledVolume > 0.0)
      body += ",\"filled_volume\":" + DoubleToString(lastTradeFilledVolume, 2);
   if(lastAccountSnapshot != "")
      body += ",\"snapshot\":" + lastAccountSnapshot;
   body += "}";

   char data[];
   char result[];
   int dataLen = StringToCharArray(body, data, 0, StringLen(body));
   string headers = "Content-Type: application/json\r\n";
   int res = WebRequest("POST", ProcessedURL, headers, NULL, 5000, data, dataLen, result, headers);
   if(res == 200)
      Print("📡 Result sent to bridge (status=", status, ", retcode=", lastTradeRetcode, ")");
   else
      if(res == -1)
         Print("⚠️ Could not report result to bridge (timeout).");
      else
         if(res == 1001)
            Print("⛔ URL not allowed for result-report: ", ProcessedURL);
         else
            Print("⚠️ Result-post response: ", res);

// Local debug trail: one line per outcome, appended.
   if(ResultLogFile != "")
     {
      int h = FileOpen(ResultLogFile, FILE_READ | FILE_WRITE);
      if(h != INVALID_HANDLE)
        {
         FileSeek(h, 0, SEEK_END);
         FileWriteString(h, TimeToString(TimeLocal(), TIME_DATE | TIME_SECONDS) + "|" + reqId + "|" +
                         cmd.symbol + "|" + cmd.action + "|" + DoubleToString(cmd.size, 6) + "|" +
                         IntegerToString(lastTradeRetcode) + "|" + status + "|" + lastTradeResult + "\n");
         FileClose(h);
        }
      else
         Print("⚠️ Could not open result log file: ", ResultLogFile);
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
string ParseSignalTimeStamp(string jsonText)
  {
// Bridge timestamps look like "2026-09-17T00:00:00Z". Normalize them
// to "YYYY.MM.DD HH:MM" (the same layout TimeToString() produces,
// minus seconds) so the signal can be aged WITHOUT StringToTime(),
// which is locale/build dependent on this terminal build.
// Returns "" when the timestamp is missing or unparseable.
   string ts = JsonGetString(jsonText, "timestamp");
   if(ts == "")
      return "";
   for(int i = 0; i < StringLen(ts); i++)
     {
      ushort c = StringGetCharacter(ts, i);
      if(c == '-')
         StringSetCharacter(ts, i, (ushort)'.');
      else
         if(c == 'T')
            StringSetCharacter(ts, i, (ushort)' ');
         else
            if(c == 'Z')
              {
               ts = StringSubstr(ts, 0, i);
               break;
              }
     }
// Truncation also neutralizes trailing junk (seconds, fraction,
// timezone offset) that could break the lexicographic comparison.
   if(StringLen(ts) > 16)
      ts = StringSubstr(ts, 0, 16);
   return ts;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
int StampToMinutes(string stamp)
  {
// "YYYY.MM.DD HH:MM" -> minutes past midnight (HH*60+MM).
   string timePart = StringSubstr(stamp, 11, 5);
   int hh = (int)StringToInteger(StringSubstr(timePart, 0, 2));
   int mm = (int)StringToInteger(StringSubstr(timePart, 3, 2));
   return hh * 60 + mm;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool IsSignalStale(string jsonText, int maxAgeMinutes)
  {
// Age the signal without StringToTime(): compare the "YYYY.MM.DD"
// day part lexicographically, then the HH:MM minute part. An earlier
// calendar day counts as stale unless it is exactly yesterday with a
// minute value close enough that only the midnight rollover separated
// them (e.g. 23:59 versus 00:05).
   string sigStamp = ParseSignalTimeStamp(jsonText);
   if(sigStamp == "")
      return false; // cannot date it - never clear blindly

   datetime nowGmt = TimeGMT();
   string nowStamp = TimeToString(nowGmt, TIME_DATE | TIME_SECONDS); // "YYYY.MM.DD HH:MM:SS" (UTC/GMT)
   string sigDate = StringSubstr(sigStamp, 0, 10);
   string nowDate = StringSubstr(nowStamp, 0, 10);

   int dayCmp = StringCompare(sigDate, nowDate);
   if(dayCmp > 0)
      return false;   // future-dated - do not touch
   if(dayCmp < 0)
     {
      string yesterday = TimeToString(nowGmt - 86400, TIME_DATE);
      if(sigDate == yesterday)
        {
         int ageMin = (StampToMinutes(nowStamp) + 1440) - StampToMinutes(sigStamp);
         return ageMin > maxAgeMinutes;
        }
      return true;     // older than yesterday => stale
     }
   int ageMin = StampToMinutes(nowStamp) - StampToMinutes(sigStamp);
   return ageMin > maxAgeMinutes;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool IsSymbolAllowed(string symbol)
  {
   if(symbol == "")
      return false;
   if(AllowedSymbols == "")
      return true;
   string list[];
   int count = StringSplit(AllowedSymbols, (ushort)',', list);
   for(int i = 0; i < count; i++)
     {
      string candidate = list[i];
      while(StringLen(candidate) > 0 && StringGetCharacter(candidate, 0) == ' ')
         candidate = StringSubstr(candidate, 1);
      while(StringLen(candidate) > 0 && StringGetCharacter(candidate, StringLen(candidate) - 1) == ' ')
         candidate = StringSubstr(candidate, 0, StringLen(candidate) - 1);
      if(candidate == symbol)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool IsSymbolAvailable(string symbol)
  {
// A symbol is usable when the terminal knows its digits or carries a
// quote (BID/ASK > 0), so a valid symbol is not rejected just because
// SYMBOL_DIGITS is not yet populated (weekend / cold start).
   if(SymbolInfoInteger(symbol, SYMBOL_DIGITS) > 0)
      return true;
   if(SymbolInfoDouble(symbol, SYMBOL_BID) > 0.0)
      return true;
   if(SymbolInfoDouble(symbol, SYMBOL_ASK) > 0.0)
      return true;
   return false;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
double NormalizeVolume(string symbol, double lot)
  {
// Round to the broker volume step and clamp to min/max so that
// arbitrary "size" values from the bridge cannot be rejected.
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minV = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxV = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0)
      step = 0.01;
   if(lot <= 0)
      lot = minV > 0 ? minV : lot;
   lot = MathFloor(MathMax(lot, minV) / step + 0.5) * step;
   if(maxV > 0 && lot > maxV)
      lot = maxV;
   return lot;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
string NormalizeAction(string action)
  {
// Accept the many ways a strategy/bridge may word the same order.
   string a = action;
   StringToUpper(a);
   if(a == "BUY" || a == "LONG" || a == "OPEN_BUY" || a == "OPENLONG" || a == "BUY_NOW")
      return "buy";
   if(a == "SELL" || a == "SHORT" || a == "OPEN_SELL" || a == "OPENSHORT" || a == "SELL_NOW")
      return "sell";
   if(a == "CLOSE" || a == "CLOSE_SYMBOL" || a == "FLAT" || a == "CANCEL")
      return "close_symbol";
   if(a == "CLOSE_ALL" || a == "FLAT_ALL" || a == "CLOSE_EVERYTHING")
      return "close_all";
   if(a == "PARTIAL_CLOSE" || a == "CLOSE_PERCENT" || a == "SCALE_OUT")
      return "partial_close";
   if(a == "MODIFY" || a == "MODIFY_STOP" || a == "SET_SLTP" || a == "UPDATE_SLTP")
      return "modify";
   if(a == "BREAKEVEN" || a == "BE" || a == "MOVE_BE" || a == "BREAK_EVEN")
      return "breakeven";
   if(a == "HOLD" || a == "HOLD_POSITION" || a == "NONE" || a == "NOOP" || a == "WAIT" || a == "TREND" || a == "WAIT_FOR")
      return "noop";
   if(a == "TRAILING" || a == "TRAILING_STOP" || a == "TRAIL" || a == "UPDATE_TRAILING")
      return "trailing";
   if(a == "CLOSE_POSITIONS")
      return "close_all";
   if(a == "MODIFY_POSITION")
      return "modify";
   if(a == "CLOSE_PENDING")
      return "cancel_pending";
// v6.5: account snapshot and pending-order actions.
   if(a == "ACCOUNT" || a == "STATUS" || a == "SNAPSHOT" || a == "ACCOUNT_INFO" || a == "INFO")
      return "account";
   if(a == "PENDING" || a == "PLACE_PENDING" || a == "PENDING_ORDER" || a == "OPEN_PENDING")
      return "pending";
   if(a == "BUY_LIMIT" || a == "BUYLIMIT")
      return "buy_limit";
   if(a == "SELL_LIMIT" || a == "SELLLIMIT")
      return "sell_limit";
   if(a == "BUY_STOP" || a == "BUYSTOP")
      return "buy_stop";
   if(a == "SELL_STOP" || a == "SELLSTOP")
      return "sell_stop";
   if(a == "BUY_STOP_LIMIT" || a == "BUYSTOPLIMIT")
      return "buy_stop_limit";
   if(a == "SELL_STOP_LIMIT" || a == "SELLSTOPLIMIT")
      return "sell_stop_limit";
   if(a == "CANCEL_PENDING" || a == "CANCEL_ORDER" || a == "DELETE_ORDER" || a == "DELETE_PENDING")
      return "cancel_pending";
   if(a == "MODIFY_PENDING" || a == "MODIFY_ORDER" || a == "UPDATE_ORDER" || a == "UPDATE_PENDING")
      return "modify_pending";
   return action; // keep original for unknown actions (reported to bridge)
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool ParseTradeCommand(string jsonText, TradeCommand &cmd)
  {
   cmd.action = "";
   cmd.symbol = "";
   cmd.size = 0.0;
   cmd.sizeUnit = "lots";
   cmd.riskPercent = DefaultRiskPercent; cmd.riskMoney = 0.0;
   cmd.stopLoss = BuildDistanceSpec("points", 0.0);
   cmd.takeProfit = BuildDistanceSpec("points", 0.0);
   cmd.hasBreakeven = false;
   cmd.breakevenTrigger = BuildDistanceSpec("points", 0.0);
   cmd.breakevenLockIn = BuildDistanceSpec("points", 0.0);
   cmd.hasTrailing = false;
   cmd.trailingActivation = BuildDistanceSpec("points", 0.0);
   cmd.trailingDistance = BuildDistanceSpec("points", 0.0);
   cmd.isLegacy = false;
   cmd.orderType = "";
   cmd.price = 0.0;
   cmd.stopLimit = 0.0;
   cmd.orderTicket = 0;
   cmd.priceUnit = "price";
   cmd.expiration = "";
   cmd.hasStopLoss = false;
   cmd.hasTakeProfit = false;
   cmd.positionTicket = 0;
   cmd.closeVolume = 0.0;
   cmd.comment = "";
   cmd.deviation = 10;

// The bridge wraps commands in "normalized"; raw top-level payloads
// stay supported for backwards compatibility.
   string cmdObject = JsonGetObject(jsonText, "normalized");
   if(cmdObject == "")
      cmdObject = jsonText;

// Plain-text legacy signals ("signal":"BUY EURUSD SL=50") are handled
// separately; JSON-string signal values (new schema) are NOT legacy.
   string signalText = JsonGetString(jsonText, "signal");
   if(signalText != "" && StringFind(signalText, "{") < 0)
     {
      cmd.isLegacy = true;
      return ParseLegacyCommand(signalText, cmd);
     }

   cmd.action = JsonGetString(cmdObject, "action");
// Fallback: the envelope may carry the command only inside the "signal"
// JSON string instead of "normalized"/top level - honour it there too.
   if(cmd.action == "" && StringFind(signalText, "{") >= 0)
      cmd.action = JsonGetString(signalText, "action");
   if(cmd.action == "")
     {
      Print("⚠️ Signal has no action field: ", StringSubstr(jsonText, 0, 150));
      return false;
     }
   cmd.action = NormalizeAction(cmd.action);

   cmd.symbol = JsonGetString(cmdObject, "symbol");
   if(cmd.symbol == "" && StringFind(signalText, "{") >= 0)
      cmd.symbol = JsonGetString(signalText, "symbol");
   if(cmd.symbol == "")
      cmd.symbol = Symbol();

// v6.5: "account" is a read-only snapshot and needs no symbol; "*"
// means "all symbols" for pending-order housekeeping (cancel/modify).
// Skip the allow-list and availability checks for both.
   bool needsSymbol = (cmd.action != "account" && cmd.symbol != "*");
   if(needsSymbol && !IsSymbolAllowed(cmd.symbol))
     {
      Print("⛔ Symbol not in allowed list: ", cmd.symbol);
      return false;
     }
   if(needsSymbol && !IsSymbolAvailable(cmd.symbol))
     {
      Print("⛔ Symbol not available in terminal: ", cmd.symbol);
      return false;
     }

// "size" may be a number (-> lots) or an object with a unit:
// lots | percent | risk | money. Plain "lot"/"value" keys accepted too.
   cmd.size = JsonGetDouble(cmdObject, "size", 0.0);
   if(cmd.size <= 0 && StringFind(signalText, "{") >= 0)
      cmd.size = JsonGetDouble(signalText, "size", 0.0);
   cmd.sizeUnit = "lots";
   string sizeObject = JsonGetObject(cmdObject, "size");
   if(sizeObject != "")
     {
      cmd.size = JsonGetDouble(sizeObject, "value", 0.0);
      cmd.sizeUnit = JsonGetString(sizeObject, "unit");
      if(cmd.sizeUnit == "")
         cmd.sizeUnit = "lots";
     }
   if(cmd.size <= 0)
     {
      cmd.size = JsonGetDouble(cmdObject, "lot", 0.0);
      cmd.sizeUnit = "lots";
     }
   if(cmd.size <= 0)
      cmd.size = JsonGetDouble(cmdObject, "value", 0.0);
   cmd.riskPercent = JsonGetDouble(cmdObject, "risk_percent", DefaultRiskPercent);
   cmd.riskMoney = JsonGetDouble(cmdObject, "risk_money",
                   JsonGetDouble(cmdObject, "risk_cash",
                   JsonGetDouble(cmdObject, "risk_amount",
                   JsonGetDouble(cmdObject, "risk_usd", 0.0))));

   cmd.stopLoss = ParseDistanceSpec(cmdObject, "stop_loss", "sl");
   cmd.takeProfit = ParseDistanceSpec(cmdObject, "take_profit", "tp");

   if(cmd.stopLoss.value <= 0 && cmd.stopLoss.unit != "atr")
      cmd.stopLoss = BuildDistanceSpec("points", JsonGetDouble(cmdObject, "sl_distance", StopLossPoints));
   if(cmd.takeProfit.value <= 0 && cmd.takeProfit.unit != "atr")
      cmd.takeProfit = BuildDistanceSpec("points", JsonGetDouble(cmdObject, "tp_distance", TakeProfitPoints));

   cmd.hasBreakeven = false;
   string breakevenObject = JsonGetObject(cmdObject, "breakeven");
   if(breakevenObject != "")
     {
      cmd.hasBreakeven = true;
      cmd.breakevenTrigger = ParseDistanceSpecObject(breakevenObject, "trigger");
      cmd.breakevenLockIn = ParseDistanceSpecObject(breakevenObject, "lock_in");
      if(cmd.breakevenTrigger.value <= 0)
         cmd.breakevenTrigger = BuildDistanceSpec("points", 100.0);
      if(cmd.breakevenLockIn.value <= 0)
         cmd.breakevenLockIn = BuildDistanceSpec("points", 20.0);
     }

   cmd.hasTrailing = false;
   string trailingObject = JsonGetObject(cmdObject, "trailing");
   if(trailingObject != "")
     {
      cmd.hasTrailing = true;
      cmd.trailingActivation = ParseDistanceSpecObject(trailingObject, "activation");
      cmd.trailingDistance = ParseDistanceSpecObject(trailingObject, "distance");
      if(cmd.trailingDistance.value <= 0)
         cmd.trailingDistance = BuildDistanceSpec("points", 60.0);
     }

   cmd.closePercent = (int)JsonGetDouble(cmdObject, "close_percent", 100.0);

// v6.5: pending-order fields. hasStopLoss/hasTakeProfit record whether
// the payload REALLY carried SL/TP so "modify_pending" never overwrites
// existing values with the default point fallbacks.
   cmd.orderType = JsonGetString(cmdObject, "order_type");
   if(cmd.orderType == "")
      cmd.orderType = cmd.action;   // direct action aliases are canonical

   // R1 fix: do not treat negative price as missing when price is a signed points offset
   bool hasPriceKey = (StringFind(cmdObject, "\"price\"") >= 0);
   if(hasPriceKey)
      cmd.price = JsonGetDouble(cmdObject, "price", 0.0);
   else
      cmd.price = JsonGetDouble(cmdObject, "entry", 0.0);

   cmd.stopLimit = JsonGetDouble(cmdObject, "stoplimit", 0.0);
   if(cmd.stopLimit <= 0)
      cmd.stopLimit = JsonGetDouble(cmdObject, "stop_limit", 0.0);
   if(cmd.stopLimit <= 0)
      cmd.stopLimit = JsonGetDouble(cmdObject, "limit_price", 0.0);
   cmd.orderTicket = (ulong)JsonGetDouble(cmdObject, "order_ticket", 0.0);
   if(cmd.orderTicket <= 0)
      cmd.orderTicket = (ulong)JsonGetDouble(cmdObject, "ticket", 0.0);
   cmd.priceUnit = JsonGetString(cmdObject, "price_unit");
   if(cmd.priceUnit == "")
      cmd.priceUnit = "price";
   cmd.expiration = JsonGetString(cmdObject, "expiration");
   if(cmd.expiration == "")
      cmd.expiration = JsonGetString(cmdObject, "expiry");
   cmd.hasStopLoss = (StringFind(cmdObject, "\"stop_loss\"") >= 0 ||
                      StringFind(cmdObject, "\"sl\"") >= 0 ||
                      StringFind(cmdObject, "\"sl_distance\"") >= 0);
   cmd.hasTakeProfit = (StringFind(cmdObject, "\"take_profit\"") >= 0 ||
                        StringFind(cmdObject, "\"tp\"") >= 0 ||
                        StringFind(cmdObject, "\"tp_distance\"") >= 0);

   // v6.6 additions: positionTicket, closeVolume, comment, deviation
   cmd.positionTicket = (ulong)JsonGetDouble(cmdObject, "position_ticket", 0.0);
   if(cmd.positionTicket <= 0)
      cmd.positionTicket = (ulong)JsonGetDouble(cmdObject, "position_id", 0.0);
   cmd.closeVolume = JsonGetDouble(cmdObject, "close_volume", 0.0);
   if(cmd.closeVolume <= 0.0)
      cmd.closeVolume = JsonGetDouble(cmdObject, "volume", 0.0);
   cmd.comment = JsonGetString(cmdObject, "comment");
   if(cmd.comment == "")
      cmd.comment = JsonGetString(cmdObject, "strategy_id");
   if(cmd.comment == "")
      cmd.comment = JsonGetString(cmdObject, "client_tag");
   if(cmd.comment == "")
      cmd.comment = JsonGetString(cmdObject, "tag");
   cmd.deviation = (int)JsonGetDouble(cmdObject, "deviation", 0.0);
   if(cmd.deviation <= 0)
      cmd.deviation = (int)JsonGetDouble(cmdObject, "slippage", 10.0);
   return true;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool ParseLegacyCommand(string signalText, TradeCommand &cmd)
  {
   cmd.action = "buy";
   string text = signalText;
   StringToUpper(text);
   if(StringFind(text, "SELL") >= 0 || StringFind(text, "SHORT") >= 0)
      cmd.action = "sell";

   cmd.symbol = Symbol();

   if(!IsSymbolAllowed(cmd.symbol))
     {
      Print("⛔ Symbol not in allowed list: ", cmd.symbol);
      return false;
     }
   if(!IsSymbolAvailable(cmd.symbol))
     {
      Print("⛔ Symbol not available in terminal: ", cmd.symbol);
      return false;
     }
   cmd.size = LotSize;
   cmd.sizeUnit = "lots";
   cmd.riskPercent = DefaultRiskPercent; cmd.riskMoney = 0.0;
   cmd.stopLoss = BuildDistanceSpec("points", StopLossPoints);
   cmd.takeProfit = BuildDistanceSpec("points", TakeProfitPoints);

   int pos = StringFind(signalText, "SL=");
   if(pos >= 0)
     {
      string remainder = StringSubstr(signalText, pos + 3);
      int endPos = StringFind(remainder, " ");
      if(endPos < 0)
         endPos = StringLen(remainder);
      string value = StringSubstr(remainder, 0, endPos);
      cmd.stopLoss.value = StringToDouble(value);
      cmd.stopLoss.unit = "points";
     }

   pos = StringFind(signalText, "TP=");
   if(pos >= 0)
     {
      string remainder = StringSubstr(signalText, pos + 3);
      int endPos = StringFind(remainder, " ");
      if(endPos < 0)
         endPos = StringLen(remainder);
      string value = StringSubstr(remainder, 0, endPos);
      cmd.takeProfit.value = StringToDouble(value);
      cmd.takeProfit.unit = "points";
     }

   pos = StringFind(signalText, "LOT=");
   if(pos >= 0)
     {
      string remainder = StringSubstr(signalText, pos + 4);
      int endPos = StringFind(remainder, " ");
      if(endPos < 0)
         endPos = StringLen(remainder);
      string value = StringSubstr(remainder, 0, endPos);
      cmd.size = StringToDouble(value);
     }

   return true;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
DistanceSpec ParseDistanceSpec(string jsonText, string fieldA, string fieldB)
  {
   DistanceSpec out;
   out.unit = "points";
   out.value = 0;
   out.period = 14;
   out.timeframe = "M15";
   out.multiplier = 1.0;

   string object = JsonGetObject(jsonText, fieldA);
   if(object == "")
      object = JsonGetObject(jsonText, fieldB);
   if(object != "")
      return ParseDistanceSpecObject(object, "");

   string value = JsonGetString(jsonText, fieldA);
   if(value == "")
      value = JsonGetString(jsonText, fieldB);
   if(value != "")
     {
      out.value = StringToDouble(value);
      return out;
     }

   return out;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
DistanceSpec ParseDistanceSpecObject(string objectText, string key)
  {
   DistanceSpec out;
   out.unit = "points";
   out.value = 0;
   out.period = 14;
   out.timeframe = "M15";
   out.multiplier = 1.0;

   if(key != "")
     {
      string nested = JsonGetObject(objectText, key);
      if(nested != "")
         objectText = nested;
     }

   out.unit = JsonGetString(objectText, "unit");
   if(out.unit == "")
      out.unit = "points";
   out.value = JsonGetDouble(objectText, "value", 0.0);
   if(out.value <= 0)
      out.value = JsonGetDouble(objectText, "distance", 0.0);
   out.multiplier = JsonGetDouble(objectText, "multiplier", 1.0);
   out.period = (int)JsonGetDouble(objectText, "period", 14.0);
   out.timeframe = JsonGetString(objectText, "timeframe");
   if(out.timeframe == "")
      out.timeframe = "M15";
   return out;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
DistanceSpec BuildDistanceSpec(string unit, double value)
  {
   DistanceSpec out;
   out.unit = unit;
   out.value = value;
   out.period = 14;
   out.timeframe = "M15";
   out.multiplier = 1.0;
   return out;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
double RiskSizePosition(string symbol, TradeCommand &cmd, int slPoints)
  {
// Risk-based position sizing (documented approximations, not exact
// risk math): "risk" mode targets risk_percent % of balance at risk
// when SL is hit; "percent" uses % of balance as notional; "money"
// converts a cash amount to lots; "lots" is used as-is.
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double price = ask > bid ? ask : bid;
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double contract = 100000.0; // FX default contract size (documented approx.)
   if(tickValue <= 0)
      tickValue = 10.0; // fallback: $10 per point (1.0 lot, USD pair)
   if(price <= 0)
      price = 1.0;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(point <= 0)
      point = 0.0001;
   if(tickSize <= 0)
      tickSize = point;

   string unit = cmd.sizeUnit;
   if(unit == "")
      unit = "lots";
   double riskPct = (cmd.riskPercent > 0) ? cmd.riskPercent : DefaultRiskPercent;
   double lots = cmd.size;
   bool isRiskMoney = (unit == "risk_money" || unit == "risk_cash" || unit == "risk_amount" || unit == "risk_usd"); if(unit == "risk" || isRiskMoney)
     {
      double lossPerLot = (slPoints > 0 && tickSize > 0) ? (slPoints * point / tickSize) * tickValue : 0.0;
      double riskAmt = isRiskMoney ? (cmd.size > 0 ? cmd.size : cmd.riskMoney) : (balance * riskPct / 100.0); lots = (lossPerLot > 0 && riskAmt > 0) ? riskAmt / lossPerLot : LotSize;
     }
   if(unit == "percent")
      lots = (balance * (cmd.size > 0 ? cmd.size : riskPct) / 100.0) / (price * contract);
   if(unit == "money" || unit == "currency")
      lots = (cmd.size > 0 ? cmd.size : 1.0) / (price * contract);
   if(unit == "lots" || lots <= 0)
      lots = (cmd.size > 0) ? cmd.size : LotSize;

// Hard caps: per-symbol exposure and one-trade notional risk ceiling.
   if(MaxExposurePerSymbol > 0)
      lots = MathMin(lots, MaxExposurePerSymbol);
   if(MaxOpenRiskPercent > 0)
     {
      double maxRiskLots = (balance * MaxOpenRiskPercent / 100.0) / (price * contract);
      if(maxRiskLots > 0)
         lots = MathMin(lots, maxRiskLots);
     }
   lots = NormalizeVolume(symbol, lots);

// Crude margin sanity check: needed margin ~ lots*contract*price/lev.
   long lev = AccountInfoInteger(ACCOUNT_LEVERAGE);
   if(lev <= 0)
      lev = 100;
   double margin = (lots * contract * price) / lev;
   if(free < margin * 0.05 || lots <= 0)
     {
      lastTradeRetcode = -20;
      lastTradeResult = "SKIPPED: insufficient margin/bad size";
      return 0;
     }
   return lots;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void ExecuteTradeCommand(TradeCommand &cmd)
  {
// Outcome tracking is global so ReportSignalResult() can send it back.
   lastTradeRetcode = 0;
   lastTradeResult = "PROCESSED";
   lastAccountSnapshot = "";
   lastTradeOrderTicket = 0;
   lastTradePositionTicket = 0;
   lastTradeDealTicket = 0;
   lastTradeFilledPrice = 0.0;
   lastTradeFilledVolume = 0.0;

// v6.5: read-only account snapshot - works even when trading is disabled
// or the market is closed (pure state query, never submits an order).
   if(cmd.action == "account")
     {
      lastAccountSnapshot = BuildAccountSnapshotJson();
      lastTradeResult = "ACCOUNT SNAPSHOT";
      Print("📊 Account snapshot: balance=", AccountInfoDouble(ACCOUNT_BALANCE),
            " equity=", AccountInfoDouble(ACCOUNT_EQUITY),
            " positions=", PositionsTotal(), " pending=", OrdersTotal());
      return;
     }

// R5 fix: noop acknowledged even when offline or trading disabled
   if(cmd.action == "noop")
     {
      lastTradeRetcode = 0;
      lastTradeResult = "NOOP";
      Print("ℹ️ No-op signal acknowledged (symbol=", cmd.symbol, ")");
      return;
     }

   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
     {
      lastTradeRetcode = -10;
      lastTradeResult = "SKIPPED: terminal offline";
      Print("⛔ Terminal offline - not connected to server. Signal skipped.");
      return;
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      lastTradeRetcode = -11;
      lastTradeResult = "SKIPPED: trading disabled";
      Print("⛔ Trading is disabled in terminal/account. Signal skipped.");
      return;
     }
   if((cmd.action == "buy" || cmd.action == "sell") && PositionsTotal() >= MaxPositions)
     {
      lastTradeRetcode = -12;
      lastTradeResult = "SKIPPED: max positions";
      Print("⛔ Maximum open positions reached. Ignoring command.");
      return;
     }

   if(cmd.action == "buy")
     {
      OpenBuyOrder(cmd.symbol, cmd);
      return;
     }
   if(cmd.action == "sell")
     {
      OpenSellOrder(cmd.symbol, cmd);
      return;
     }
   if(cmd.action == "close_position" || cmd.action == "close_symbol")
     {
      ClosePositionByMagicAndSymbol(cmd.symbol, MagicNumber, cmd.positionTicket);
      return;
     }
   if(cmd.action == "close_all")
     {
      CloseAllPositions();
      return;
     }
   if(cmd.action == "partial_close")
     {
      PartialClosePosition(cmd);
      return;
     }
   if(cmd.action == "modify")
     {
      ModifyPosition(cmd);
      return;
     }
   if(cmd.action == "breakeven")
     {
      MoveToBreakeven(cmd);
      return;
     }
   if(cmd.action == "trailing")
     {
      ApplyTrailingStop(cmd);
      return;
     }
// v6.5: pending-order engine (place / cancel / modify).
   if(cmd.action == "pending"        || cmd.action == "buy_limit"     ||
      cmd.action == "sell_limit"     || cmd.action == "buy_stop"      ||
      cmd.action == "sell_stop"      || cmd.action == "buy_stop_limit" ||
      cmd.action == "sell_stop_limit")
     {
      PlacePendingOrder(cmd);
      return;
     }
   if(cmd.action == "cancel_pending")
     {
      CancelPendingOrder(cmd);
      return;
     }
   if(cmd.action == "modify_pending")
     {
      ModifyPendingOrder(cmd);
      return;
     }
   lastTradeRetcode = -13;
   lastTradeResult = "SKIPPED: unknown action";
   Print("⚠️ Command action not implemented yet: ", cmd.action);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OpenBuyOrder(string symbol, TradeCommand &cmd)
  {
   double lot = cmd.size;
   if(SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
     {
      lastTradeRetcode = -14;
      lastTradeResult = "SKIPPED: market closed (" + symbol + ")";
      Print("⛔ Market closed/trading not allowed for ", symbol, " - buy skipped.");
      return;
     }
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int slPoints = ResolveDistanceToPoints(symbol, cmd.stopLoss);
   int tpPoints = ResolveDistanceToPoints(symbol, cmd.takeProfit);
   if(slPoints > 0 && slPoints < MinStopDistancePoints)
      slPoints = MinStopDistancePoints;
   if(tpPoints > 0 && tpPoints < MinStopDistancePoints)
      tpPoints = MinStopDistancePoints;
   lot = RiskSizePosition(symbol, cmd, slPoints);
   if(lot <= 0)
      return;
   double slPrice = ask - slPoints * point;
   double tpPrice = ask + tpPoints * point;

   if(cmd.deviation > 0)
      trade.SetDeviationInPoints(cmd.deviation);
   else
      trade.SetDeviationInPoints(10);
   string cmt = (cmd.comment != "") ? cmd.comment : "BUY";

   if(trade.Buy(lot, symbol, ask, slPrice, tpPrice, cmt))
     {
      lastTradeRetcode = 0;
      lastTradeResult = "BUY OPENED";
      lastTradeOrderTicket = trade.ResultOrder();
      lastTradeDealTicket = trade.ResultDeal();
      lastTradeFilledPrice = trade.ResultPrice();
      lastTradeFilledVolume = trade.ResultVolume();
      if(HistoryDealSelect(lastTradeDealTicket))
         lastTradePositionTicket = (ulong)HistoryDealGetInteger(lastTradeDealTicket, DEAL_POSITION_ID);
      if(lastTradePositionTicket <= 0)
         lastTradePositionTicket = lastTradeOrderTicket;
      Print("✅ BUY OPENED | symbol=", symbol, " lot=", lot, " ticket=", lastTradePositionTicket, " deal=", lastTradeDealTicket, " price=", lastTradeFilledPrice);
     }
   else
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "BUY FAILED";
      Print("❌ BUY FAILED: ", trade.ResultRetcode(), " ", trade.ResultComment());
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OpenSellOrder(string symbol, TradeCommand &cmd)
  {
   double lot = cmd.size;
   if(SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
     {
      lastTradeRetcode = -15;
      lastTradeResult = "SKIPPED: market closed (" + symbol + ")";
      Print("⛔ Market closed/trading not allowed for ", symbol, " - sell skipped.");
      return;
     }
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int slPoints = ResolveDistanceToPoints(symbol, cmd.stopLoss);
   int tpPoints = ResolveDistanceToPoints(symbol, cmd.takeProfit);
   if(slPoints > 0 && slPoints < MinStopDistancePoints)
      slPoints = MinStopDistancePoints;
   if(tpPoints > 0 && tpPoints < MinStopDistancePoints)
      tpPoints = MinStopDistancePoints;
   lot = RiskSizePosition(symbol, cmd, slPoints);
   if(lot <= 0)
      return;
   double slPrice = bid + slPoints * point;
   double tpPrice = bid - tpPoints * point;

   if(cmd.deviation > 0)
      trade.SetDeviationInPoints(cmd.deviation);
   else
      trade.SetDeviationInPoints(10);
   string cmt = (cmd.comment != "") ? cmd.comment : "SELL";

   if(trade.Sell(lot, symbol, bid, slPrice, tpPrice, cmt))
     {
      lastTradeRetcode = 0;
      lastTradeResult = "SELL OPENED";
      lastTradeOrderTicket = trade.ResultOrder();
      lastTradeDealTicket = trade.ResultDeal();
      lastTradeFilledPrice = trade.ResultPrice();
      lastTradeFilledVolume = trade.ResultVolume();
      if(HistoryDealSelect(lastTradeDealTicket))
         lastTradePositionTicket = (ulong)HistoryDealGetInteger(lastTradeDealTicket, DEAL_POSITION_ID);
      if(lastTradePositionTicket <= 0)
         lastTradePositionTicket = lastTradeOrderTicket;
      Print("✅ SELL OPENED | symbol=", symbol, " lot=", lot, " ticket=", lastTradePositionTicket, " deal=", lastTradeDealTicket, " price=", lastTradeFilledPrice);
     }
   else
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "SELL FAILED";
      Print("❌ SELL FAILED: ", trade.ResultRetcode(), " ", trade.ResultComment());
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void CloseAllPositions()
  {
   int closed = 0;
   int failed = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         if(trade.PositionClose(ticket))
            closed++;
         else
            failed++;
        }
     }
   if(closed > 0)
     {
      lastTradeRetcode = 0;
      lastTradeResult = "CLOSED " + IntegerToString(closed) + " POSITIONS";
     }
   else if(failed > 0)
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "CLOSE ALL FAILED";
     }
   else
     {
      lastTradeRetcode = 0;
      lastTradeResult = "NO POSITIONS TO CLOSE";
     }
   Print("✅ Closed positions: ", closed, " failed=", failed);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void PartialClosePosition(TradeCommand &cmd)
  {
   int percent = cmd.closePercent;
   if(percent <= 0)
      percent = 50;
   if(percent > 100)
      percent = 100;
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
        {
         if(cmd.positionTicket > 0)
           {
            if(ticket != cmd.positionTicket)
               continue;
           }
         else
           {
            if(PositionGetString(POSITION_SYMBOL) != cmd.symbol || PositionGetInteger(POSITION_MAGIC) != MagicNumber)
               continue;
           }
         string posSym = PositionGetString(POSITION_SYMBOL);
         double posVol = PositionGetDouble(POSITION_VOLUME);
         double vol = (cmd.closeVolume > 0.0) ? cmd.closeVolume : (posVol * percent / 100.0);
         vol = NormalizeVolume(posSym, vol);
         if(vol > posVol)
            vol = posVol;
         bool pcOk = trade.PositionClosePartial(ticket, vol);
         lastTradeRetcode = pcOk ? 0 : (int)trade.ResultRetcode();
         lastTradePositionTicket = ticket;
         lastTradeFilledVolume = vol;
         lastTradeResult = pcOk ? ("PARTIAL CLOSED ticket=" + IntegerToString(ticket) + " vol=" + DoubleToString(vol, 2)) : "PARTIAL CLOSE FAILED";
         Print("✅ Partial close ", vol, " lots on ticket=", ticket, " (", posSym, ")");
         return;
        }
     }
   lastTradeRetcode = -30;
   lastTradeResult = "FAILED: no position to close";
   Print("⚠️ No open position for partial close: ", cmd.symbol);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void ModifyPosition(TradeCommand &cmd)
  {
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
        {
         if(cmd.positionTicket > 0)
           {
            if(ticket != cmd.positionTicket)
               continue;
           }
         else
           {
            if(PositionGetString(POSITION_SYMBOL) != cmd.symbol || PositionGetInteger(POSITION_MAGIC) != MagicNumber)
               continue;
           }
         string posSym = PositionGetString(POSITION_SYMBOL);
         double open = PositionGetDouble(POSITION_PRICE_OPEN);
         double point = SymbolInfoDouble(posSym, SYMBOL_POINT);
         int slPts = cmd.hasStopLoss ? ResolveDistanceToPoints(posSym, cmd.stopLoss) : 0;
         int tpPts = cmd.hasTakeProfit ? ResolveDistanceToPoints(posSym, cmd.takeProfit) : 0;
         double sl = PositionGetDouble(POSITION_SL);
         double tp = PositionGetDouble(POSITION_TP);
         long ptype = PositionGetInteger(POSITION_TYPE);
         if(ptype == POSITION_TYPE_BUY)
           {
            if(slPts > 0)
               sl = open - slPts * point;
            if(tpPts > 0)
               tp = open + tpPts * point;
           }
         else
           {
            if(slPts > 0)
               sl = open + slPts * point;
            if(tpPts > 0)
               tp = open - tpPts * point;
           }
         sl = NormalizePrice(posSym, sl);
         tp = NormalizePrice(posSym, tp);
         bool modOk = trade.PositionModify(ticket, sl, tp);
         lastTradeRetcode = modOk ? 0 : (int)trade.ResultRetcode();
         lastTradePositionTicket = ticket;
         lastTradeResult = modOk ? ("MODIFIED ticket=" + IntegerToString(ticket)) : "MODIFY FAILED";
         Print("✅ SL/TP modified ticket=", ticket, " sl=", sl, " tp=", tp);
         return;
        }
     }
   lastTradeRetcode = -31;
   lastTradeResult = "FAILED: no position to modify";
   Print("⚠️ No open position to modify: ", cmd.symbol);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void MoveToBreakeven(TradeCommand &cmd)
  {
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
        {
         if(cmd.positionTicket > 0)
           {
            if(ticket != cmd.positionTicket)
               continue;
           }
         else
           {
            if(PositionGetString(POSITION_SYMBOL) != cmd.symbol || PositionGetInteger(POSITION_MAGIC) != MagicNumber)
               continue;
           }
         string posSym = PositionGetString(POSITION_SYMBOL);
         double open = PositionGetDouble(POSITION_PRICE_OPEN);
         double point = SymbolInfoDouble(posSym, SYMBOL_POINT);
         long ptype = PositionGetInteger(POSITION_TYPE);
         double currentPrice = (ptype == POSITION_TYPE_BUY) ? SymbolInfoDouble(posSym, SYMBOL_BID) : SymbolInfoDouble(posSym, SYMBOL_ASK);

         // Breakeven trigger enforcement
         if(cmd.hasBreakeven && cmd.breakevenTrigger.value > 0.0)
           {
            int triggerPts = ResolveDistanceToPoints(posSym, cmd.breakevenTrigger);
            double requiredPrice = (ptype == POSITION_TYPE_BUY) ? open + triggerPts * point : open - triggerPts * point;
            bool triggerReached = (ptype == POSITION_TYPE_BUY) ? (currentPrice >= requiredPrice) : (currentPrice <= requiredPrice);
            if(!triggerReached)
              {
               lastTradeRetcode = -32;
               lastTradeResult = "FAILED: breakeven trigger not reached (need " + IntegerToString(triggerPts) + " pts)";
               Print("⚠️ Breakeven trigger not reached for ticket=", ticket, " (current=", currentPrice, ", need=", requiredPrice, ")");
               return;
              }
           }

         int lockPts = (cmd.breakevenLockIn.value > 0) ? (int)MathRound(cmd.breakevenLockIn.value) : 10;
         double tp = PositionGetDouble(POSITION_TP);
         double sl = (ptype == POSITION_TYPE_BUY) ? open + lockPts * point : open - lockPts * point;
         sl = NormalizePrice(posSym, sl);
         bool beOk = trade.PositionModify(ticket, sl, tp);
         lastTradeRetcode = beOk ? 0 : (int)trade.ResultRetcode();
         lastTradePositionTicket = ticket;
         lastTradeResult = beOk ? ("BREAKEVEN SET ticket=" + IntegerToString(ticket)) : "BREAKEVEN FAILED";
         Print("✅ Breakeven set ticket=", ticket, " lock_in=", lockPts, " pts");
         return;
        }
     }
   lastTradeRetcode = -32;
   lastTradeResult = "FAILED: no position for breakeven";
   Print("⚠️ No open position for breakeven: ", cmd.symbol);
  }

//+------------------------------------------------------------------+
//| ApplyTrailingStop: moves SL to lock in profit behind market      |
//+------------------------------------------------------------------+
void ApplyTrailingStop(TradeCommand &cmd)
  {
   int modifiedCount = 0;
   int matchedCount = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
        {
         if(cmd.positionTicket > 0)
           {
            if(ticket != cmd.positionTicket)
               continue;
           }
         else
           {
            if(PositionGetString(POSITION_SYMBOL) != cmd.symbol || PositionGetInteger(POSITION_MAGIC) != MagicNumber)
               continue;
           }
         matchedCount++;
         string posSym = PositionGetString(POSITION_SYMBOL);
         double open = PositionGetDouble(POSITION_PRICE_OPEN);
         double curSl = PositionGetDouble(POSITION_SL);
         double curTp = PositionGetDouble(POSITION_TP);
         double point = SymbolInfoDouble(posSym, SYMBOL_POINT);
         long ptype = PositionGetInteger(POSITION_TYPE);
         double bid = SymbolInfoDouble(posSym, SYMBOL_BID);
         double ask = SymbolInfoDouble(posSym, SYMBOL_ASK);

         int trailDistPts = (cmd.hasTrailing && cmd.trailingDistance.value > 0.0) ? 
                            ResolveDistanceToPoints(posSym, cmd.trailingDistance) : 60;
         int actPts = (cmd.hasTrailing && cmd.trailingActivation.value > 0.0) ? 
                      ResolveDistanceToPoints(posSym, cmd.trailingActivation) : 0;

         if(ptype == POSITION_TYPE_BUY)
           {
            double profitPts = (bid - open) / point;
            if(actPts > 0 && profitPts < actPts)
              {
               Print("ℹ️ Trailing activation not reached for buy ticket=", ticket, " (profit=", profitPts, ", need=", actPts, ")");
               continue;
              }
            double newSl = bid - trailDistPts * point;
            newSl = NormalizePrice(posSym, newSl);
            if(newSl > curSl && newSl < bid)
              {
               if(trade.PositionModify(ticket, newSl, curTp))
                 {
                  modifiedCount++;
                  lastTradePositionTicket = ticket;
                  Print("✅ Trailing stop updated for buy ticket=", ticket, " newSl=", newSl);
                 }
              }
           }
         else // SELL
           {
            double profitPts = (open - ask) / point;
            if(actPts > 0 && profitPts < actPts)
              {
               Print("ℹ️ Trailing activation not reached for sell ticket=", ticket, " (profit=", profitPts, ", need=", actPts, ")");
               continue;
              }
            double newSl = ask + trailDistPts * point;
            newSl = NormalizePrice(posSym, newSl);
            if((curSl == 0.0 || newSl < curSl) && newSl > ask)
              {
               if(trade.PositionModify(ticket, newSl, curTp))
                 {
                  modifiedCount++;
                  lastTradePositionTicket = ticket;
                  Print("✅ Trailing stop updated for sell ticket=", ticket, " newSl=", newSl);
                 }
              }
           }
        }
     }
   if(matchedCount == 0)
     {
      lastTradeRetcode = -31;
      lastTradeResult = "FAILED: no position found for trailing stop";
      Print("⚠️ No position found for trailing stop: ", cmd.symbol);
     }
   else if(modifiedCount > 0)
     {
      lastTradeRetcode = 0;
      lastTradeResult = "TRAILING APPLIED (" + IntegerToString(modifiedCount) + " positions)";
     }
   else
     {
      lastTradeRetcode = 0;
      lastTradeResult = "TRAILING UNCHANGED (no SL adjustment needed)";
      Print("ℹ️ Trailing stop checked: no SL modification needed for ", cmd.symbol);
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void ClosePositionByMagicAndSymbol(string symbol, int magic, ulong targetTicket = 0)
  {
   int closed = 0;
   int failed = 0;
   bool allSymbols = (symbol == "*");
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
        {
         if(targetTicket > 0)
           {
            if(ticket != targetTicket)
               continue;
           }
         else
           {
            if(!allSymbols && PositionGetString(POSITION_SYMBOL) != symbol)
               continue;
            if(PositionGetInteger(POSITION_MAGIC) != magic)
               continue;
           }
         if(trade.PositionClose(ticket))
           {
            closed++;
            lastTradePositionTicket = ticket;
           }
         else
           {
            failed++;
           }
        }
     }
   if(closed > 0)
     {
      lastTradeRetcode = 0;
      lastTradeResult = (targetTicket > 0) ? ("CLOSED ticket=" + IntegerToString(targetTicket)) :
                        ("CLOSED " + IntegerToString(closed) + " POSITIONS (" + symbol + ")");
      Print("✅ Closed ", closed, " positions (symbol=", symbol, ")");
     }
   else if(failed > 0)
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "CLOSE FAILED";
      Print("❌ Close failed: ", trade.ResultRetcode(), " ", trade.ResultComment());
     }
   else
     {
      lastTradeRetcode = -30;
      lastTradeResult = "FAILED: no position to close";
      Print("⚠️ No position to close for symbol=", symbol, " ticket=", targetTicket);
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| EscapeJsonString: minimal JSON escaping for feedback payloads      |
//+------------------------------------------------------------------+
string EscapeJsonString(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\n", "\\n");
   StringReplace(s, "\r", "\\r");
   return s;
  }

//+------------------------------------------------------------------+
//| PositionTypeToString: POSITION_TYPE -> stable JSON token           |
//+------------------------------------------------------------------+
string PositionTypeToString(long type)
  {
   switch((ENUM_POSITION_TYPE)type)
     {
      case POSITION_TYPE_BUY:
         return "buy";
      case POSITION_TYPE_SELL:
         return "sell";
      default:
         return "unknown";
     }
  }

//+------------------------------------------------------------------+
//| OrderTypeToString: ORDER_TYPE -> stable JSON token                 |
//+------------------------------------------------------------------+
string OrderTypeToString(long type)
  {
   switch((ENUM_ORDER_TYPE)type)
     {
      case ORDER_TYPE_BUY:
         return "buy";
      case ORDER_TYPE_SELL:
         return "sell";
      case ORDER_TYPE_BUY_LIMIT:
         return "buy_limit";
      case ORDER_TYPE_SELL_LIMIT:
         return "sell_limit";
      case ORDER_TYPE_BUY_STOP:
         return "buy_stop";
      case ORDER_TYPE_SELL_STOP:
         return "sell_stop";
      case ORDER_TYPE_BUY_STOP_LIMIT:
         return "buy_stop_limit";
      case ORDER_TYPE_SELL_STOP_LIMIT:
         return "sell_stop_limit";
      default:
         return "unknown";
     }
  }

//+------------------------------------------------------------------+
//| OrderStateToString: ORDER_STATE -> stable JSON token               |
//+------------------------------------------------------------------+
string OrderStateToString(long state)
  {
   switch((ENUM_ORDER_STATE)state)
     {
      case ORDER_STATE_STARTED:
         return "started";
      case ORDER_STATE_PLACED:
         return "placed";
      case ORDER_STATE_PARTIAL:
         return "partial";
      case ORDER_STATE_FILLED:
         return "filled";
      case ORDER_STATE_CANCELED:
         return "canceled";
      case ORDER_STATE_EXPIRED:
         return "expired";
      case ORDER_STATE_REJECTED:
         return "rejected";
      case ORDER_STATE_REQUEST_ADD:
         return "request_add";
      case ORDER_STATE_REQUEST_MODIFY:
         return "request_modify";
      case ORDER_STATE_REQUEST_CANCEL:
         return "request_cancel";
      default:
         return "unknown";
     }
  }

//+------------------------------------------------------------------+
//| BuildPositionsJson: all open positions (any magic, magic included) |
//+------------------------------------------------------------------+
string BuildPositionsJson()
  {
   string out = "[";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      int digits = (int)SymbolInfoInteger(PositionGetString(POSITION_SYMBOL), SYMBOL_DIGITS);
      string sym = PositionGetString(POSITION_SYMBOL);
      if(out != "[")
         out += ",";
      out += "{\"ticket\":" + IntegerToString(ticket)
             + ",\"symbol\":\"" + sym + "\""
             + ",\"type\":\"" + PositionTypeToString(PositionGetInteger(POSITION_TYPE)) + "\""
             + ",\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2)
             + ",\"open_price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), digits)
             + ",\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), digits)
             + ",\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), digits)
             + ",\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2)
             + ",\"swap\":" + DoubleToString(PositionGetDouble(POSITION_SWAP), 2)
             + ",\"magic\":" + IntegerToString((long)PositionGetInteger(POSITION_MAGIC))
             + ",\"comment\":\"" + EscapeJsonString(PositionGetString(POSITION_COMMENT)) + "\"}";
     }
   out += "]";
   return out;
  }

//+------------------------------------------------------------------+
//| BuildPendingOrdersJson: all pending orders (magic included)        |
//+------------------------------------------------------------------+
string BuildPendingOrdersJson()
  {
   string out = "[";
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket))
         continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      datetime exp = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);
      string expStr = (exp > 0) ? TimeToString(exp, TIME_DATE | TIME_MINUTES) : "";
      if(out != "[")
         out += ",";
      out += "{\"ticket\":" + IntegerToString(ticket)
             + ",\"symbol\":\"" + sym + "\""
             + ",\"type\":\"" + OrderTypeToString(OrderGetInteger(ORDER_TYPE)) + "\""
             + ",\"state\":\"" + OrderStateToString(OrderGetInteger(ORDER_STATE)) + "\""
             + ",\"volume\":" + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 2)
             + ",\"price\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), digits)
             + ",\"stoplimit\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_STOPLIMIT), digits)
             + ",\"sl\":" + DoubleToString(OrderGetDouble(ORDER_SL), digits)
             + ",\"tp\":" + DoubleToString(OrderGetDouble(ORDER_TP), digits)
             + ",\"expiration\":\"" + expStr + "\""
             + ",\"magic\":" + IntegerToString((long)OrderGetInteger(ORDER_MAGIC))
             + ",\"comment\":\"" + EscapeJsonString(OrderGetString(ORDER_COMMENT)) + "\"}";
     }
   out += "]";
   return out;
  }

//+------------------------------------------------------------------+
//| BuildAccountSnapshotJson: one JSON object with account state,      |
//| open positions and pending orders (used by the "account" action).  |
//+------------------------------------------------------------------+
string BuildAccountSnapshotJson()
  {
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin  = AccountInfoDouble(ACCOUNT_MARGIN);
   double free    = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   long level = (margin > 0.0) ? (long)MathRound(equity / margin * 100.0) : 0;
   long mm = AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   string marginMode = (mm == (long)ACCOUNT_MARGIN_MODE_EXCHANGE ||
                        mm == (long)ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) ? "hedging" : "netting";
   long atm = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string accountTradeMode = (atm == (long)ACCOUNT_TRADE_MODE_DEMO)   ? "demo" :
                             (atm == (long)ACCOUNT_TRADE_MODE_REAL)   ? "real" :
                             (atm == (long)ACCOUNT_TRADE_MODE_CONTEST) ? "contest" : "unknown";

   string out = "{";
   out += "\"account\":" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));
   out += ",\"name\":\"" + EscapeJsonString(AccountInfoString(ACCOUNT_NAME)) + "\"";
   out += ",\"server\":\"" + EscapeJsonString(AccountInfoString(ACCOUNT_SERVER)) + "\"";
   out += ",\"company\":\"" + EscapeJsonString(AccountInfoString(ACCOUNT_COMPANY)) + "\"";
   out += ",\"currency\":\"" + AccountInfoString(ACCOUNT_CURRENCY) + "\"";
   out += ",\"margin_mode\":\"" + marginMode + "\"";
   out += ",\"trade_mode\":\"" + accountTradeMode + "\"";
   out += ",\"leverage\":" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LEVERAGE));
   out += ",\"balance\":" + DoubleToString(balance, 2);
   out += ",\"equity\":" + DoubleToString(equity, 2);
   out += ",\"margin\":" + DoubleToString(margin, 2);
   out += ",\"margin_free\":" + DoubleToString(free, 2);
   out += ",\"margin_level\":" + IntegerToString(level);
   out += ",\"credit\":" + DoubleToString(AccountInfoDouble(ACCOUNT_CREDIT), 2);
   out += ",\"profit\":" + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2);
   out += ",\"trade_allowed\":" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false");
   out += ",\"trade_expert\":" + (MQLInfoInteger(MQL_TRADE_ALLOWED) ? "true" : "false");
   out += ",\"terminal_connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false");
   out += ",\"positions_count\":" + IntegerToString(PositionsTotal());
   out += ",\"positions\":" + BuildPositionsJson();
   out += ",\"pending_orders_count\":" + IntegerToString(OrdersTotal());
   out += ",\"pending_orders\":" + BuildPendingOrdersJson();
   out += "}";
   return out;
  }

//+------------------------------------------------------------------+
//| NormalizePrice: round to the symbol tick size, then to digits      |
//+------------------------------------------------------------------+
double NormalizePrice(string symbol, double price)
  {
   double ts = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(ts > 0.0)
      price = MathRound(price / ts) * ts;
   if(digits >= 0)
      price = NormalizeDouble(price, digits);
   return price;
  }

//+------------------------------------------------------------------+
//| ParseDatetimeFlexible: "YYYY.MM.DD HH:MM[:SS]" or ISO datetime -  |
//| builds MqlDateTime manually (no locale-dependent StringToTime).   |
//| Returns 0 when unparseable so callers keep the broker default.    |
//+------------------------------------------------------------------+
datetime ParseDatetimeFlexible(string s)
  {
   if(StringLen(s) < 16)
      return 0;
   string t = s;
   StringReplace(t, "T", " ");
   StringReplace(t, "Z", "");
   int dash = StringFind(t, "-");
   int dot = StringFind(t, ".");
   if(dot < 0 && dash >= 0 && dash < 6)
      StringReplace(t, "-", ".");   // ISO date -> MT5 "YYYY.MM.DD"
   string parts[];
   if(StringSplit(t, (ushort)' ', parts) < 2)
      return 0;
   string dp[];
   if(StringSplit(parts[0], (ushort)'.', dp) != 3)
      return 0;
   string tp[];
   int tpc = StringSplit(parts[1], (ushort)':', tp);
   if(tpc < 2)
      return 0;
   int year  = (int)StringToInteger(dp[0]);
   int month = (int)StringToInteger(dp[1]);
   int day   = (int)StringToInteger(dp[2]);
   int hour  = (int)StringToInteger(tp[0]);
   int min   = (int)StringToInteger(tp[1]);
   int sec   = (tpc > 2) ? (int)StringToInteger(tp[2]) : 0;
   if(year < 2000 || year > 2099 || month < 1 || month > 12 ||
      day < 1 || day > 31 || hour < 0 || hour > 23 ||
      min < 0 || min > 59 || sec < 0 || sec > 59)
      return 0;
   MqlDateTime dt;
   dt.year = year;
   dt.mon  = month;
   dt.day  = day;
   dt.hour = hour;
   dt.min  = min;
   dt.sec  = sec;
   dt.day_of_week = 0;
   dt.day_of_year = 0;
   return StructToTime(dt);
  }

//+------------------------------------------------------------------+
//| PlacePendingOrder: places the 6 pending order types via CTrade.    |
//| Supports absolute or distance pricing, SL/TP from the order price, |
//| and optional expiration.                                          |
//+------------------------------------------------------------------+
void PlacePendingOrder(TradeCommand &cmd)
  {
   string typeName = cmd.orderType;
   if(typeName == "" || typeName == "pending")
      typeName = cmd.action;
   bool isBuyLimit  = (typeName == "buy_limit");
   bool isSellLimit = (typeName == "sell_limit");
   bool isBuyStop   = (typeName == "buy_stop");
   bool isSellStop  = (typeName == "sell_stop");
   bool isBuySL     = (typeName == "buy_stop_limit");
   bool isSellSL    = (typeName == "sell_stop_limit");
   if(!(isBuyLimit || isSellLimit || isBuyStop || isSellStop || isBuySL || isSellSL))
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: invalid pending type";
      Print("⚠️ Unsupported pending order type: ", typeName);
      return;
     }
   if(SymbolInfoInteger(cmd.symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
     {
      lastTradeRetcode = -42;
      lastTradeResult = "SKIPPED: market closed (" + cmd.symbol + ")";
      Print("⛔ Market closed/trading not allowed for ", cmd.symbol, " - pending skipped.");
      return;
     }
   bool isBuy = (isBuyLimit || isBuyStop || isBuySL);
   double ask = SymbolInfoDouble(cmd.symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(cmd.symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(cmd.symbol, SYMBOL_POINT);
   double base = isBuy ? ask : bid;
   if(base <= 0.0)
     {
      lastTradeRetcode = -42;
      lastTradeResult = "SKIPPED: no quote for " + cmd.symbol;
      Print("⛔ No quote for ", cmd.symbol, " - pending skipped.");
      return;
     }
   double price = cmd.price;
   double stopLimit = cmd.stopLimit;
// R1 fix: "price_unit":"points"/"distance" -> price is a signed offset from the
// current market (+above / -below); compute absolute price before validating price > 0.
   bool priceInPoints = (cmd.priceUnit == "points" || cmd.priceUnit == "distance");
   if(priceInPoints)
     {
      price = base + price * point;
      if(stopLimit > 0.0)
         stopLimit = base + stopLimit * point;
     }

   if(price <= 0.0)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: price required";
      Print("⚠️ Pending order needs a price field.");
      return;
     }

   // R3 fix: validate order price side against current market
   if(isBuyStop && price <= ask)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: buy_stop price must be above Ask";
      Print("⚠️ buy_stop price ", price, " is not above Ask ", ask);
      return;
     }
   if(isSellStop && price >= bid)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: sell_stop price must be below Bid";
      Print("⚠️ sell_stop price ", price, " is not below Bid ", bid);
      return;
     }
   if(isBuyLimit && price >= ask)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: buy_limit price must be below Ask";
      Print("⚠️ buy_limit price ", price, " is not below Ask ", ask);
      return;
     }
   if(isSellLimit && price <= bid)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: sell_limit price must be above Bid";
      Print("⚠️ sell_limit price ", price, " is not above Bid ", bid);
      return;
     }
   if(isBuySL && price <= ask)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: buy_stop_limit price must be above Ask";
      Print("⚠️ buy_stop_limit price ", price, " is not above Ask ", ask);
      return;
     }
   if(isSellSL && price >= bid)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: sell_stop_limit price must be below Bid";
      Print("⚠️ sell_stop_limit price ", price, " is not below Bid ", bid);
      return;
     }

   if((isBuySL || isSellSL) && stopLimit <= 0.0)
     {
      lastTradeRetcode = -40;
      lastTradeResult = "SKIPPED: stoplimit required";
      Print("⚠️ Stop-limit order needs a stoplimit field.");
      return;
     }

   price = NormalizePrice(cmd.symbol, price);
   if(stopLimit > 0.0)
      stopLimit = NormalizePrice(cmd.symbol, stopLimit);

   int slPoints = (cmd.hasStopLoss) ? ResolveDistanceToPoints(cmd.symbol, cmd.stopLoss) : 0;
   int tpPoints = (cmd.hasTakeProfit) ? ResolveDistanceToPoints(cmd.symbol, cmd.takeProfit) : 0;
   if(slPoints > 0 && slPoints < MinStopDistancePoints)
      slPoints = MinStopDistancePoints;
   if(tpPoints > 0 && tpPoints < MinStopDistancePoints)
      tpPoints = MinStopDistancePoints;
   double lot = RiskSizePosition(cmd.symbol, cmd, slPoints);
   if(lot <= 0.0)
      return;
// SL/TP are measured from the pending order price, not the market price.
   double sl = (slPoints > 0) ? (isBuy ? price - slPoints * point : price + slPoints * point) : 0.0;
   double tp = (tpPoints > 0) ? (isBuy ? price + tpPoints * point : price - tpPoints * point) : 0.0;
   sl = NormalizePrice(cmd.symbol, sl);
   tp = NormalizePrice(cmd.symbol, tp);

   datetime expiration = 0;
   ENUM_ORDER_TYPE_TIME typeTime = ORDER_TIME_GTC;
   if(cmd.expiration != "")
     {
      expiration = ParseDatetimeFlexible(cmd.expiration);
      if(expiration <= 0)
        {
         lastTradeRetcode = -43;
         lastTradeResult = "SKIPPED: invalid expiration";
         Print("⚠️ Unparseable expiration: ", cmd.expiration);
         return;
        }
      typeTime = ORDER_TIME_SPECIFIED;
     }

   if(cmd.deviation > 0)
      trade.SetDeviationInPoints(cmd.deviation);
   else
      trade.SetDeviationInPoints(10);
   string cmt = (cmd.comment != "") ? cmd.comment : (isBuySL ? "PENDING_BUY_STOP_LIMIT" : (isSellSL ? "PENDING_SELL_STOP_LIMIT" : typeName));

   bool ok = false;
   if(isBuyLimit)
      ok = trade.BuyLimit(lot, price, cmd.symbol, sl, tp, typeTime, expiration, cmt);
   else if(isSellLimit)
      ok = trade.SellLimit(lot, price, cmd.symbol, sl, tp, typeTime, expiration, cmt);
   else if(isBuyStop)
      ok = trade.BuyStop(lot, price, cmd.symbol, sl, tp, typeTime, expiration, cmt);
   else if(isSellStop)
      ok = trade.SellStop(lot, price, cmd.symbol, sl, tp, typeTime, expiration, cmt);
   else
      ok = trade.OrderOpen(cmd.symbol,
                           isBuySL ? ORDER_TYPE_BUY_STOP_LIMIT : ORDER_TYPE_SELL_STOP_LIMIT,
                           lot, stopLimit, price, sl, tp, typeTime, expiration, cmt);

   if(ok)
     {
      lastTradeRetcode = 0;
      lastTradeOrderTicket = trade.ResultOrder();
      lastTradeResult = "PENDING " + typeName;
      if(lastTradeOrderTicket > 0)
         lastTradeResult += " ticket=" + IntegerToString(lastTradeOrderTicket);
      Print("✅ PENDING ", typeName, " | symbol=", cmd.symbol, " lot=", lot,
            " price=", price, " slpts=", slPoints, " tppts=", tpPoints);
     }
   else
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "PENDING FAILED";
      Print("❌ PENDING ", typeName, " FAILED: ", trade.ResultRetcode(), " ", trade.ResultComment());
     }
  }

//+------------------------------------------------------------------+
//| CancelPendingOrder: delete by ticket, or all matching orders for   |
//| one symbol, or all symbols when cmd.symbol == "*". Magic-filtered. |
//+------------------------------------------------------------------+
void CancelPendingOrder(TradeCommand &cmd)
  {
   bool allSymbols = (cmd.symbol == "*");
   if(cmd.orderTicket > 0)
     {
      if(!OrderSelect(cmd.orderTicket))
        {
         lastTradeRetcode = -41;
         lastTradeResult = "FAILED: order not found";
         Print("⚠️ Pending order not found: ", cmd.orderTicket);
         return;
        }
      if((long)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
        {
         lastTradeRetcode = -41;
         lastTradeResult = "FAILED: order not owned by this bot";
         Print("⚠️ Pending order ", cmd.orderTicket, " has a different magic - refused.");
         return;
        }
      if(trade.OrderDelete(cmd.orderTicket))
        {
         lastTradeRetcode = 0;
         lastTradeOrderTicket = cmd.orderTicket; lastTradeResult = "ORDER CANCELED ticket=" + IntegerToString(cmd.orderTicket);
         Print("✅ Pending order canceled: ", cmd.orderTicket);
        }
      else
        {
         lastTradeRetcode = (int)trade.ResultRetcode();
         lastTradeResult = "CANCEL FAILED";
         Print("❌ Cancel failed: ", trade.ResultRetcode(), " ", trade.ResultComment());
        }
      return;
     }
   int canceled = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(!OrderSelect(ticket) || (long)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;
      if(allSymbols || OrderGetString(ORDER_SYMBOL) == cmd.symbol)
        {
         if(trade.OrderDelete(ticket))
            canceled++;
        }
     }
   if(canceled > 0)
     {
      lastTradeRetcode = 0;
      lastTradeResult = "CANCELED " + IntegerToString(canceled) + " ORDERS";
      Print("✅ Pending orders canceled: ", canceled);
     }
   else
     {
      lastTradeRetcode = -41;
      lastTradeResult = "FAILED: no matching pending order";
      Print("⚠️ No matching pending order for ", (cmd.symbol == "*" ? "any symbol" : cmd.symbol));
     }
  }

//+------------------------------------------------------------------+
//| ModifyPendingOrder: change price/SL/TP/expiration of one pending   |
//| order (by order_ticket, or the first magic+symbol match). Only     |
//| fields present in the payload are overwritten. Magic-filtered.    |
//+------------------------------------------------------------------+
void ModifyPendingOrder(TradeCommand &cmd)
  {
   ulong target = cmd.orderTicket;
   if(target == 0)
     {
      for(int i = OrdersTotal() - 1; i >= 0; i--)
        {
         ulong ticket = OrderGetTicket(i);
         if(OrderSelect(ticket) &&
            (long)OrderGetInteger(ORDER_MAGIC) == MagicNumber &&
            OrderGetString(ORDER_SYMBOL) == cmd.symbol)
           {
            target = ticket;
            break;
           }
        }
     }
   if(target == 0 || !OrderSelect(target))
     {
      lastTradeRetcode = -41;
      lastTradeResult = "FAILED: no matching pending order";
      Print("⚠️ No matching pending order for modify.");
      return;
     }
   if((long)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
     {
      lastTradeRetcode = -41;
      lastTradeResult = "FAILED: order not owned by this bot";
      Print("⚠️ Pending order ", target, " has a different magic - refused.");
      return;
     }
   long otype = OrderGetInteger(ORDER_TYPE);
   // R4 fix: resolve symbol directly from the order rather than relying on cmd.symbol
   string orderSymbol = OrderGetString(ORDER_SYMBOL);
   bool isBuy = (otype == ORDER_TYPE_BUY_LIMIT || otype == ORDER_TYPE_BUY_STOP ||
                 otype == ORDER_TYPE_BUY_STOP_LIMIT);
   double curPrice = OrderGetDouble(ORDER_PRICE_OPEN);
   double curSl = OrderGetDouble(ORDER_SL);
   double curTp = OrderGetDouble(ORDER_TP);
   double curStopLimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
   datetime curExp = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);
   double point = SymbolInfoDouble(orderSymbol, SYMBOL_POINT);

   double newPrice = (cmd.price > 0.0) ? NormalizePrice(orderSymbol, cmd.price) : curPrice;
   double newSl = curSl;
   double newTp = curTp;
   if(cmd.hasStopLoss)
     {
      int pts = ResolveDistanceToPoints(orderSymbol, cmd.stopLoss);
      if(pts > 0 && pts < MinStopDistancePoints)
         pts = MinStopDistancePoints;
      newSl = (pts > 0) ? (isBuy ? newPrice - pts * point : newPrice + pts * point) : 0.0;
     }
   if(cmd.hasTakeProfit)
     {
      int pts = ResolveDistanceToPoints(orderSymbol, cmd.takeProfit);
      if(pts > 0 && pts < MinStopDistancePoints)
         pts = MinStopDistancePoints;
      newTp = (pts > 0) ? (isBuy ? newPrice + pts * point : newPrice - pts * point) : 0.0;
     }
   newSl = NormalizePrice(orderSymbol, newSl);
   newTp = NormalizePrice(orderSymbol, newTp);
   datetime newExp = curExp;
   ENUM_ORDER_TYPE_TIME typeTime = (newExp > 0) ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC;
   if(cmd.expiration != "")
     {
      newExp = ParseDatetimeFlexible(cmd.expiration);
      if(newExp <= 0)
        {
         lastTradeRetcode = -43;
         lastTradeResult = "SKIPPED: invalid expiration";
         Print("⚠️ Unparseable expiration: ", cmd.expiration);
         return;
        }
      typeTime = ORDER_TIME_SPECIFIED;
     }

   if(trade.OrderModify(target, newPrice, newSl, newTp, typeTime, newExp,
                        (otype == ORDER_TYPE_BUY_STOP_LIMIT || otype == ORDER_TYPE_SELL_STOP_LIMIT) ?
                        curStopLimit : 0.0))
     {
      lastTradeRetcode = 0;
      lastTradeOrderTicket = target;
      lastTradeResult = "ORDER MODIFIED ticket=" + IntegerToString(target);
      Print("✅ Pending order modified: ", target, " price=", newPrice, " sl=", newSl, " tp=", newTp);
     }
   else
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "MODIFY FAILED";
      Print("❌ Modify pending failed: ", trade.ResultRetcode(), " ", trade.ResultComment());
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
double GetAtrValue(int handle)
  {
   double atrBuffer[]; for(int w = 0; w < 10 && BarsCalculated(handle) < 2; w++) Sleep(20);
   ArraySetAsSeries(atrBuffer, true);
   double value = 0.0;
   if(CopyBuffer(handle, 0, 1, 1, atrBuffer) > 0)
      value = atrBuffer[0];
   IndicatorRelease(handle);
   if(value > 0 && value != EMPTY_VALUE)
      return value;
   return 0.0;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
int ResolveDistanceToPoints(string symbol, DistanceSpec &spec)
  {
   if(spec.value <= 0 && spec.unit != "atr")
      return 50;

   if(spec.unit == "points")
      return (int)MathRound(spec.value);
   if(spec.unit == "price_distance")
      return (int)MathRound(spec.value / SymbolInfoDouble(symbol, SYMBOL_POINT));
   if(spec.unit == "atr")
     {
      int handle = iATR(symbol, PERIOD_M15, MathMax(spec.period, 2));
      if(handle != INVALID_HANDLE)
        {
         double atr = GetAtrValue(handle);
         if(atr > 0)
            return (int)MathRound((atr / SymbolInfoDouble(symbol, SYMBOL_POINT)) * spec.multiplier);
        }
     }
   return 50;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
string JsonGetString(string jsonText, string key)
  {
   string needle = "\"" + key + "\"";
   int pos = StringFind(jsonText, needle);
   if(pos < 0)
      return "";

   pos = StringFind(jsonText, ":", pos);
   if(pos < 0)
      return "";
   pos++;
   while(pos < StringLen(jsonText))
     {
      string ch = StringSubstr(jsonText, pos, 1);
      if(ch == " " || ch == "\n" || ch == "\r" || ch == "\t")
        {
         pos++;
         continue;
        }
      break;
     }

   string ch = StringSubstr(jsonText, pos, 1);
   if(ch == "\"")
     {
      pos++;
      int end = StringFind(jsonText, "\"", pos);
      if(end < 0)
         return "";
      return StringSubstr(jsonText, pos, end - pos);
     }

   int end = pos;
   while(end < StringLen(jsonText))
     {
      string current = StringSubstr(jsonText, end, 1);
      if(current == "," || current == "}" || current == "]")
         break;
      end++;
     }
// JSON null/true/false (symbol: null etc.) mean "not specified": return
// "" so callers fall back to defaults instead of looking up a symbol
// literally named "null".
   string token = StringSubstr(jsonText, pos, end - pos);
   string upper = token;
   StringToUpper(upper);
   if(token == "" || upper == "NULL" || upper == "TRUE" || upper == "FALSE")
      return "";
   return token;
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
string JsonGetObject(string jsonText, string key)
  {
   string needle = "\"" + key + "\"";
   int pos = StringFind(jsonText, needle);
   if(pos < 0)
      return "";

   int start = StringFind(jsonText, "{", pos);
   if(start < 0)
      return "";

   int end = start + 1;
   int depth = 1;
   while(end < StringLen(jsonText) && depth > 0)
     {
      string ch = StringSubstr(jsonText, end, 1);
      if(ch == "{")
         depth++;
      if(ch == "}")
         depth--;
      end++;
     }
   if(depth != 0)
      return "";
   return StringSubstr(jsonText, start, end - start);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
double JsonGetDouble(string jsonText, string key, double defaultValue)
  {
   string value = JsonGetString(jsonText, key);
   if(value == "")
      return defaultValue;
   return StringToDouble(value);
  }
//+------------------------------------------------------------------+
//+---------------------------------------------------------------------+