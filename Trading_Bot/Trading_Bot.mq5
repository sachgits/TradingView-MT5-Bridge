//+---------------------------------------------------------------------+
//| TradingView to MT5 Bridge - Trading Bot v6.0                          |
//| Universal command processor                                          |
//+---------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property link      "https://github.com/sachgits/TradingView-MT5-Bridge"
#property version   "6.0"
#property strict

#include <Trade\Trade.mqh>

input double LotSize = 0.01;
input int TakeProfitPoints = 100;
input int StopLossPoints = 50;
input int MagicNumber = 12345;
input int MaxPositions = 5;
input string ServerURL = "http://127.0.0.1:8080/signal";
input string ProcessedURL = "http://127.0.0.1:8080/signal/processed";

CTrade trade;
uint lastCheckTime = 0;

struct DistanceSpec
{
   string unit;
   double value;
   int period;
   string timeframe;
   double multiplier;
};

struct TradeCommand
{
   string action;
   string symbol;
   double size;
   DistanceSpec stopLoss;
   DistanceSpec takeProfit;
   bool hasBreakeven;
   DistanceSpec breakevenTrigger;
   DistanceSpec breakevenLockIn;
   bool hasTrailing;
   DistanceSpec trailingActivation;
   DistanceSpec trailingDistance;
   bool isLegacy;
   int closePercent;
};

int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   Print("✅ TradingBridge v6.0 Started");
   Print("📡 Monitoring: ", ServerURL);
   ClearOldSignalsOnStartup();
   return(INIT_SUCCEEDED);
}

void ClearOldSignalsOnStartup()
{
   string signalJson = "";
   string status = "";
   if(GetSignalFromServer(signalJson, status))
   {
      if(signalJson != "" && signalJson != "NONE")
      {
         Print("⚠️ Found stale signal on server. Clearing it.");
         MarkSignalProcessed();
      }
   }
}

void OnTick()
{
   uint checkIntervalMs = 750;
   uint now = GetTickCount();
   if(now - lastCheckTime < checkIntervalMs)
      return;
   lastCheckTime = now;

   string signalJson = "";
   string status = "";
   if(GetSignalFromServer(signalJson, status))
   {
      if(status == "NEW" && signalJson != "" && signalJson != "NONE")
      {
         TradeCommand cmd;
         if(ParseTradeCommand(signalJson, cmd))
         {
            Print("📊 Processing command: action=", cmd.action, " symbol=", cmd.symbol, " size=", cmd.size);
            ExecuteTradeCommand(cmd);
            MarkSignalProcessed();
         }
      }
   }
}

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
      Print("⚠️ WebRequest error! Add localhost:8080 to MT5 allowed URLs.");
   else
      Print("❌ Server error: ", res);
   return false;
}

void MarkSignalProcessed()
{
   char data[];
   char result[];
   string headers = "Content-Type: application/json\r\n";
   int res = WebRequest("POST", ProcessedURL, headers, NULL, 5000, data, 0, result, headers);
   if(res == -1)
      Print("⚠️ Could not mark signal as processed.");
}

bool ParseTradeCommand(string jsonText, TradeCommand &cmd)
{
   cmd = NULL;
   cmd.isLegacy = false;

   if(StringFind(jsonText, "\"signal\"") >= 0)
   {
      string signalText = JsonGetString(jsonText, "signal");
      if(signalText != "")
      {
         cmd.isLegacy = true;
         return ParseLegacyCommand(signalText, cmd);
      }
   }

   cmd.action = JsonGetString(jsonText, "action");
   if(cmd.action == "")
      return false;

   cmd.symbol = JsonGetString(jsonText, "symbol");
   if(cmd.symbol == "")
      cmd.symbol = Symbol();

   cmd.size = JsonGetDouble(jsonText, "size", LotSize);
   if(cmd.size <= 0)
      cmd.size = JsonGetDouble(jsonText, "lot", LotSize);
   if(cmd.size <= 0)
      cmd.size = JsonGetDouble(jsonText, "value", LotSize);

   cmd.stopLoss = ParseDistanceSpec(jsonText, "stop_loss", "sl");
   cmd.takeProfit = ParseDistanceSpec(jsonText, "take_profit", "tp");

   if(cmd.stopLoss.value <= 0)
      cmd.stopLoss = BuildDistanceSpec("points", JsonGetDouble(jsonText, "sl_distance", StopLossPoints));
   if(cmd.takeProfit.value <= 0)
      cmd.takeProfit = BuildDistanceSpec("points", JsonGetDouble(jsonText, "tp_distance", TakeProfitPoints));

   cmd.hasBreakeven = false;
   string breakevenObject = JsonGetObject(jsonText, "breakeven");
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
   string trailingObject = JsonGetObject(jsonText, "trailing");
   if(trailingObject != "")
   {
      cmd.hasTrailing = true;
      cmd.trailingActivation = ParseDistanceSpecObject(trailingObject, "activation");
      cmd.trailingDistance = ParseDistanceSpecObject(trailingObject, "distance");
      if(cmd.trailingDistance.value <= 0)
         cmd.trailingDistance = BuildDistanceSpec("points", 60.0);
   }

   cmd.closePercent = (int)JsonGetDouble(jsonText, "close_percent", 100.0);
   return true;
}

bool ParseLegacyCommand(string signalText, TradeCommand &cmd)
{
   cmd.action = "buy";
   string text = StringUpper(signalText);
   if(StringFind(text, "SELL") >= 0 || StringFind(text, "SHORT") >= 0)
      cmd.action = "sell";

   cmd.symbol = Symbol();
   cmd.size = LotSize;
   cmd.stopLoss = BuildDistanceSpec("points", StopLossPoints);
   cmd.takeProfit = BuildDistanceSpec("points", TakeProfitPoints);

   int pos = StringFind(signalText, "SL=");
   if(pos >= 0)
   {
      string remainder = StringSubstr(signalText, pos + 3);
      int endPos = StringFind(remainder, " ");
      if(endPos < 0) endPos = StringLen(remainder);
      string value = StringSubstr(remainder, 0, endPos);
      cmd.stopLoss.value = StringToDouble(value);
      cmd.stopLoss.unit = "points";
   }

   pos = StringFind(signalText, "TP=");
   if(pos >= 0)
   {
      string remainder = StringSubstr(signalText, pos + 3);
      int endPos = StringFind(remainder, " ");
      if(endPos < 0) endPos = StringLen(remainder);
      string value = StringSubstr(remainder, 0, endPos);
      cmd.takeProfit.value = StringToDouble(value);
      cmd.takeProfit.unit = "points";
   }

   pos = StringFind(signalText, "LOT=");
   if(pos >= 0)
   {
      string remainder = StringSubstr(signalText, pos + 4);
      int endPos = StringFind(remainder, " ");
      if(endPos < 0) endPos = StringLen(remainder);
      string value = StringSubstr(remainder, 0, endPos);
      cmd.size = StringToDouble(value);
   }

   return true;
}

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

void ExecuteTradeCommand(TradeCommand &cmd)
{
   if(PositionsTotal() >= MaxPositions)
   {
      Print("⛔ Maximum open positions reached. Ignoring command.");
      return;
   }

   if(StringFind(cmd.action, "buy") >= 0)
   {
      OpenBuyOrder(cmd.symbol, cmd.size, cmd.stopLoss, cmd.takeProfit);
      return;
   }
   if(StringFind(cmd.action, "sell") >= 0)
   {
      OpenSellOrder(cmd.symbol, cmd.size, cmd.stopLoss, cmd.takeProfit);
      return;
   }
   if(cmd.action == "close_position")
   {
      ClosePositionByMagicAndSymbol(cmd.symbol, MagicNumber);
      return;
   }
   if(cmd.action == "close_positions")
   {
      ClosePositionsBySymbol(cmd.symbol);
      return;
   }
   Print("⚠️ Command action not implemented yet: ", cmd.action);
}

void OpenBuyOrder(string symbol, double lot, DistanceSpec sl, DistanceSpec tp)
{
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int slPoints = ResolveDistanceToPoints(symbol, sl);
   int tpPoints = ResolveDistanceToPoints(symbol, tp);
   double slPrice = ask - slPoints * point;
   double tpPrice = ask + tpPoints * point;

   if(trade.Buy(lot, symbol, ask, slPrice, tpPrice, "BUY"))
      Print("✅ BUY OPENED | symbol=", symbol, " lot=", lot, " sl=", slPoints, " tp=", tpPoints);
   else
      Print("❌ BUY FAILED: ", trade.ResultRetcode());
}

void OpenSellOrder(string symbol, double lot, DistanceSpec sl, DistanceSpec tp)
{
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int slPoints = ResolveDistanceToPoints(symbol, sl);
   int tpPoints = ResolveDistanceToPoints(symbol, tp);
   double slPrice = bid + slPoints * point;
   double tpPrice = bid - tpPoints * point;

   if(trade.Sell(lot, symbol, bid, slPrice, tpPrice, "SELL"))
      Print("✅ SELL OPENED | symbol=", symbol, " lot=", lot, " sl=", slPoints, " tp=", tpPoints);
   else
      Print("❌ SELL FAILED: ", trade.ResultRetcode());
}

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

void ClosePositionsBySymbol(string symbol)
{
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) && PositionGetString(POSITION_SYMBOL) == symbol)
      {
         trade.PositionClose(ticket);
      }
   }
}

int ResolveDistanceToPoints(string symbol, DistanceSpec spec)
{
   if(spec.value <= 0)
      return 50;

   if(spec.unit == "points")
      return (int)MathRound(spec.value);
   if(spec.unit == "price_distance")
      return (int)MathRound(spec.value / SymbolInfoDouble(symbol, SYMBOL_POINT));
   if(spec.unit == "atr")
   {
      int handle = iATR(symbol, PERIOD_M15, spec.period);
      if(handle != INVALID_HANDLE)
      {
         double atr = iATR(symbol, PERIOD_M15, spec.period);
         if(atr > 0)
            return (int)MathRound((atr / SymbolInfoDouble(symbol, SYMBOL_POINT)) * spec.multiplier);
      }
   }
   return 50;
}

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
   return StringSubstr(jsonText, pos, end - pos);
}

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
      if(ch == "{") depth++;
      if(ch == "}") depth--;
      end++;
   }
   if(depth != 0)
      return "";
   return StringSubstr(jsonText, start, end - start);
}

double JsonGetDouble(string jsonText, string key, double defaultValue)
{
   string value = JsonGetString(jsonText, key);
   if(value == "")
      return defaultValue;
   return StringToDouble(value);
}











