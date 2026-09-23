//+---------------------------------------------------------------------+
//| TradingView to MT5 Bridge - Trading Bot v6.5                        |
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
//|        size modes lots/percent/risk/money with risk engine (risk %, |
//|        exposure caps, approx. margin check) and min-stop-distance.  |
//| v6.5:  fix NormalizeVolume, which clamped EVERY order to the broker |
//|        minimum lot, discarding all risk/percent/money and explicit  |
//|        lot sizing; modify now preserves an unspecified SL/TP level  |
//|        instead of overwriting it with the EA default.               |
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
//   lot           number   alias for a plain lot size. A bare top-level
//                          "value" is NOT a size alias: the JSON reader cannot
//                          scope a key search, so "value" would also match a
//                          nested stop_loss.value and trade it as a lot count.
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
#property version   "6.5"
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
   double            riskPercent;
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
   Print("✅ TradingBridge v6.5 Started (universal symbol mode)");
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
                 ",\"result\":\"" + lastTradeResult + "\"}";

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

   string nowStamp = TimeToString(TimeLocal(), TIME_DATE | TIME_SECONDS); // "YYYY.MM.DD HH:MM:SS"
   string sigDate = StringSubstr(sigStamp, 0, 10);
   string nowDate = StringSubstr(nowStamp, 0, 10);

   int dayCmp = StringCompare(sigDate, nowDate);
   if(dayCmp > 0)
      return false;   // future-dated - do not touch
   if(dayCmp < 0)
     {
      string yesterday = TimeToString(TimeLocal() - 86400, TIME_DATE);
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
      lot = (minV > 0) ? minV : step;
   lot = MathFloor(lot / step + 0.5) * step;
   if(minV > 0 && lot < minV)
      lot = minV;
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
   cmd.riskPercent = DefaultRiskPercent;
   cmd.stopLoss = BuildDistanceSpec("points", 0.0);
   cmd.takeProfit = BuildDistanceSpec("points", 0.0);
   cmd.hasBreakeven = false;
   cmd.breakevenTrigger = BuildDistanceSpec("points", 0.0);
   cmd.breakevenLockIn = BuildDistanceSpec("points", 0.0);
   cmd.hasTrailing = false;
   cmd.trailingActivation = BuildDistanceSpec("points", 0.0);
   cmd.trailingDistance = BuildDistanceSpec("points", 0.0);
   cmd.isLegacy = false;

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

// "size" may be a number (-> lots) or an object with a unit:
// lots | percent | risk | money. A plain "lot" key is accepted too.
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
// A bare top-level "value" is deliberately NOT accepted as a size. JsonGetDouble
// searches the whole command text for the first "value" key and cannot tell a
// top-level one from a nested one, so {"stop_loss":{"value":200}} with no size
// would have been read as 200 lots. Leaving size at 0 falls through to the
// LotSize input, which is the documented behaviour for an unstated volume.
   cmd.riskPercent = JsonGetDouble(cmdObject, "risk_percent", DefaultRiskPercent);

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
   cmd.riskPercent = DefaultRiskPercent;
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
   if(unit == "risk")
      {
       double lossPerLot = (slPoints > 0 && tickSize > 0) ? (slPoints * point / tickSize) * tickValue : 0.0;
       lots = (lossPerLot > 0) ? (balance * riskPct / 100.0) / lossPerLot : LotSize;
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
   if(PositionsTotal() >= MaxPositions)
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
      ClosePositionByMagicAndSymbol(cmd.symbol, MagicNumber);
      lastTradeResult = "CLOSED " + cmd.symbol;
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
   if(cmd.action == "noop")
     {
      lastTradeRetcode = 0;
      lastTradeResult = "NOOP";
      Print("ℹ️ No-op signal acknowledged (symbol=", cmd.symbol, ")");
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

   if(trade.Buy(lot, symbol, ask, slPrice, tpPrice, "BUY"))
     {
      lastTradeRetcode = 0;
      lastTradeResult = "BUY OPENED";
      Print("✅ BUY OPENED | symbol=", symbol, " lot=", lot, " sl=", slPoints, " tp=", tpPoints);
     }
   else
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "BUY FAILED";
      Print("❌ BUY FAILED: ", trade.ResultRetcode());
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

   if(trade.Sell(lot, symbol, bid, slPrice, tpPrice, "SELL"))
     {
      lastTradeRetcode = 0;
      lastTradeResult = "SELL OPENED";
      Print("✅ SELL OPENED | symbol=", symbol, " lot=", lot, " sl=", slPoints, " tp=", tpPoints);
     }
   else
     {
      lastTradeRetcode = (int)trade.ResultRetcode();
      lastTradeResult = "SELL FAILED";
      Print("❌ SELL FAILED: ", trade.ResultRetcode());
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void CloseAllPositions()
  {
   int closed = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         trade.PositionClose(ticket);
         closed++;
        }
     }
   lastTradeRetcode = 0;
   lastTradeResult = "CLOSED ALL (" + IntegerToString(closed) + ")";
   Print("✅ Close-all executed, positions closed: ", closed);
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
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == cmd.symbol &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         double vol = PositionGetDouble(POSITION_VOLUME) * percent / 100.0;
         vol = NormalizeVolume(cmd.symbol, vol);
         trade.PositionClosePartial(ticket, vol);
         lastTradeRetcode = 0;
         lastTradeResult = "PARTIAL CLOSED " + IntegerToString(percent) + "%";
         Print("✅ Partial close ", percent, "% ", cmd.symbol);
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
// ResolveDistanceToPoints() substitutes a default whenever a spec has no
// value, so it cannot tell "not specified" from "specified as zero".
// Decide which levels the command actually carries BEFORE resolving, so
// that modifying only the SL leaves the broker's existing TP alone.
   bool hasSl = (cmd.stopLoss.value > 0 || cmd.stopLoss.unit == "atr");
   bool hasTp = (cmd.takeProfit.value > 0 || cmd.takeProfit.unit == "atr");
   if(!hasSl && !hasTp)
     {
      lastTradeRetcode = -31;
      lastTradeResult = "FAILED: modify carried neither SL nor TP";
      Print("⚠️ Modify command specified no stop_loss or take_profit: ", cmd.symbol);
      return;
     }

   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == cmd.symbol &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         double open = PositionGetDouble(POSITION_PRICE_OPEN);
         double point = SymbolInfoDouble(cmd.symbol, SYMBOL_POINT);
         int slPts = hasSl ? ResolveDistanceToPoints(cmd.symbol, cmd.stopLoss) : 0;
         int tpPts = hasTp ? ResolveDistanceToPoints(cmd.symbol, cmd.takeProfit) : 0;
         double sl = 0, tp = 0;
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
         // PositionModify() treats 0 as "remove this level", so carry the
         // untouched level over from the position to preserve it.
         if(sl <= 0)
            sl = PositionGetDouble(POSITION_SL);
         if(tp <= 0)
            tp = PositionGetDouble(POSITION_TP);
         trade.PositionModify(ticket, sl, tp);
         lastTradeRetcode = 0;
         lastTradeResult = "MODIFIED " + cmd.symbol;
         Print("✅ SL/TP modified: ", cmd.symbol, " sl=", slPts, " tp=", tpPts,
               " (sl_set=", hasSl, " tp_set=", hasTp, ")");
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
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == cmd.symbol &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         double open = PositionGetDouble(POSITION_PRICE_OPEN);
         double point = SymbolInfoDouble(cmd.symbol, SYMBOL_POINT);
         int lockPts = (cmd.breakevenLockIn.value > 0) ? (int)MathRound(cmd.breakevenLockIn.value) : 10;
         long ptype = PositionGetInteger(POSITION_TYPE);
         double sl = (ptype == POSITION_TYPE_BUY) ? open + lockPts * point : open - lockPts * point;
         trade.PositionModify(ticket, sl, 0);
         lastTradeRetcode = 0;
         lastTradeResult = "BREAKEVEN SET " + cmd.symbol;
         Print("✅ Breakeven set: ", cmd.symbol, " lock_in=", lockPts, " pts");
         return;
        }
     }
   lastTradeRetcode = -32;
   lastTradeResult = "FAILED: no position for breakeven";
   Print("⚠️ No open position for breakeven: ", cmd.symbol);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void ClosePositionByMagicAndSymbol(string symbol, int magic)
  {
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == symbol &&
         PositionGetInteger(POSITION_MAGIC) == magic)
        {
         trade.PositionClose(ticket);
        }
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
double GetAtrValue(int handle)
  {
   double atrBuffer[];
   ArraySetAsSeries(atrBuffer, true);
   double value = 0.0;
   if(CopyBuffer(handle, 0, 0, 1, atrBuffer) > 0)
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