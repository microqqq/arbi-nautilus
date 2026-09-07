#property strict
#property description "Synthetic capacity/preflight tests; no account or OrderSend access"
#include "../include/Py000Json.mqh"
#include "../include/Py000Journal.mqh"

int g_capacity_count = 0;
int g_capacity_selected = 0;
int g_capacity_total_calls = 0;
int g_capacity_bad = 0;
int g_capacity_other_symbol = -1;
bool g_capacity_grow_after_check = false;
int g_capacity_send_calls = 0;
int g_capacity_unknown_getters = 0;
int g_capacity_checks = 0;
int g_capacity_failures = 0;
string CapacitySymbol() { return "SYNTHETIC"; }
datetime CapacityTime() { return D'2026.09.07 00:00:00'; }
int CapacityTotal() { g_capacity_total_calls++; return g_capacity_count; }
ulong CapacityTicket(const int index)
{
   g_capacity_selected = index;
   return index >= 0 && index < g_capacity_count ? (ulong)(1000 + index) : 0;
}
bool CapacitySelect(const ulong ticket)
{
   if(ticket < 1000 || ticket >= (ulong)(1000 + g_capacity_count)) return false;
   g_capacity_selected = (int)(ticket - 1000);
   return true;
}
bool CapacityPositionInteger(const ENUM_POSITION_PROPERTY_INTEGER property, long &value)
{
   if(g_capacity_bad == 1 && g_capacity_selected == 1) return false;
   if(property == POSITION_TICKET) value = 1000 + g_capacity_selected;
   else if(property == POSITION_IDENTIFIER)
      value = 2000 + (g_capacity_bad == 2 ? 0 : g_capacity_selected);
   else if(property == POSITION_MAGIC) value = g_capacity_selected == 1 ? 99 : 7;
   else if(property == POSITION_TYPE)
      value = g_capacity_selected % 2 == 0 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   else if(property == POSITION_TIME_MSC) value = 1700000000000;
   else { g_capacity_unknown_getters++; return false; }
   return true;
}
long CapacityPositionInteger(const ENUM_POSITION_PROPERTY_INTEGER property)
{
   long value = 0;
   CapacityPositionInteger(property, value);
   return value;
}
bool CapacityPositionString(const ENUM_POSITION_PROPERTY_STRING property, string &value)
{
   if(property == POSITION_SYMBOL)
      value = g_capacity_selected == g_capacity_other_symbol ? "OTHER" : CapacitySymbol();
   else if(property == POSITION_COMMENT) value = "synthetic";
   else { g_capacity_unknown_getters++; return false; }
   return true;
}
string CapacityPositionString(const ENUM_POSITION_PROPERTY_STRING property)
{
   string value;
   CapacityPositionString(property, value);
   return value;
}
bool CapacityPositionDouble(const ENUM_POSITION_PROPERTY_DOUBLE property, double &value)
{
   if(property == POSITION_VOLUME)
      value = g_capacity_bad == 3 && g_capacity_total_calls > 2 ? 0.03 : 0.02;
   else if(property == POSITION_PRICE_OPEN || property == POSITION_PRICE_CURRENT)
      value = 2400.0;
   else if(property == POSITION_SL || property == POSITION_TP
      || property == POSITION_PROFIT || property == POSITION_SWAP) value = 0.0;
   else { g_capacity_unknown_getters++; return false; }
   return true;
}
double CapacityPositionDouble(const ENUM_POSITION_PROPERTY_DOUBLE property)
{
   double value = 0.0;
   CapacityPositionDouble(property, value);
   return value;
}
long CapacityAccountInteger(const ENUM_ACCOUNT_INFO_INTEGER property)
{
   if(property == ACCOUNT_TRADE_MODE) return ACCOUNT_TRADE_MODE_DEMO;
   if(property == ACCOUNT_MARGIN_MODE) return ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
   if(property == ACCOUNT_TRADE_ALLOWED || property == ACCOUNT_TRADE_EXPERT) return 1;
   if(property == ACCOUNT_LOGIN || property == ACCOUNT_LEVERAGE) return 100;
   g_capacity_unknown_getters++; return 0;
}
string CapacityAccountString(const ENUM_ACCOUNT_INFO_STRING property)
{
   if(property == ACCOUNT_SERVER) return "SYNTHETIC";
   if(property == ACCOUNT_CURRENCY) return "USD";
   g_capacity_unknown_getters++; return "";
}
double CapacityAccountDouble(const ENUM_ACCOUNT_INFO_DOUBLE property)
{
   if(property == ACCOUNT_BALANCE || property == ACCOUNT_EQUITY
      || property == ACCOUNT_MARGIN_FREE) return 100000.0;
   if(property == ACCOUNT_MARGIN || property == ACCOUNT_MARGIN_LEVEL) return 0.0;
   g_capacity_unknown_getters++; return 0.0;
}
long CapacityTerminalInteger(const ENUM_TERMINAL_INFO_INTEGER property)
{
   if(property == TERMINAL_CONNECTED || property == TERMINAL_TRADE_ALLOWED) return 1;
   if(property == TERMINAL_BUILD) return 6182;
   g_capacity_unknown_getters++; return 0;
}
long CapacityMqlInteger(const ENUM_MQL_INFO_INTEGER property)
{
   if(property == MQL_TRADE_ALLOWED) return 1;
   g_capacity_unknown_getters++; return 0;
}
long CapacitySymbolInteger(const string symbol, const ENUM_SYMBOL_INFO_INTEGER property)
{
   if(property == SYMBOL_DIGITS) return 2;
   if(property == SYMBOL_TRADE_MODE) return SYMBOL_TRADE_MODE_FULL;
   if(property == SYMBOL_FILLING_MODE) return SYMBOL_FILLING_FOK;
   return 0;
}
double CapacitySymbolDouble(const string symbol, const ENUM_SYMBOL_INFO_DOUBLE property)
{
   if(property == SYMBOL_VOLUME_MIN || property == SYMBOL_VOLUME_STEP
      || property == SYMBOL_POINT || property == SYMBOL_TRADE_TICK_SIZE) return 0.01;
   if(property == SYMBOL_VOLUME_MAX || property == SYMBOL_TRADE_CONTRACT_SIZE) return 100.0;
   return 0.0;
}
string CapacitySymbolString(const string symbol, const ENUM_SYMBOL_INFO_STRING property)
{
   return "USD";
}
bool CapacityTick(const string symbol, MqlTick &tick)
{
   ZeroMemory(tick);
   tick.time = CapacityTime(); tick.time_msc = (long)tick.time * 1000;
   tick.bid = 2400.0; tick.ask = 2401.0;
   return true;
}
bool CapacitySession(const string symbol, const ENUM_DAY_OF_WEEK day,
   const uint index, datetime &from, datetime &to)
{
   from = 0; to = 0; return false;
}
bool CapacityOrderCheck(const MqlTradeRequest &request, MqlTradeCheckResult &check)
{
   ZeroMemory(check);
   if(g_capacity_grow_after_check) g_capacity_count = 32;
   return true;
}
bool CapacityOrderSend(const MqlTradeRequest &request, MqlTradeResult &result)
{
   g_capacity_send_calls++;
   ZeroMemory(result);
   result.retcode = TRADE_RETCODE_REJECT;
   return false;
}
#define _Symbol CapacitySymbol()
#define _Digits 2
#define PositionsTotal CapacityTotal
#define PositionGetTicket CapacityTicket
#define PositionSelectByTicket CapacitySelect
#define PositionGetInteger CapacityPositionInteger
#define PositionGetString CapacityPositionString
#define PositionGetDouble CapacityPositionDouble
#define AccountInfoInteger CapacityAccountInteger
#define AccountInfoString CapacityAccountString
#define AccountInfoDouble CapacityAccountDouble
#define TerminalInfoInteger CapacityTerminalInteger
#define MQLInfoInteger CapacityMqlInteger
#define SymbolInfoInteger CapacitySymbolInteger
#define SymbolInfoDouble CapacitySymbolDouble
#define SymbolInfoString CapacitySymbolString
#define SymbolInfoTick CapacityTick
#define SymbolInfoSessionTrade CapacitySession
#define TimeGMT CapacityTime
#define TimeCurrent CapacityTime
#define TimeTradeServer CapacityTime
#define OrderCheck CapacityOrderCheck
#define OrderSend CapacityOrderSend
#include "../include/Py000Execution.mqh"
#include "../include/Py000Protocol.mqh"
#undef MQLInfoInteger
#undef TerminalInfoInteger

void CapacityCheck(const string label, const bool condition)
{
   g_capacity_checks++;
   if(!condition) { g_capacity_failures++; Print("CAPACITY_FAIL ", label); }
}
void CapacityReset(const int count)
{
   g_capacity_count = count; g_capacity_total_calls = 0; g_capacity_selected = 0;
   g_capacity_bad = 0; g_capacity_other_symbol = -1; g_capacity_send_calls = 0;
   g_capacity_grow_after_check = false;
}
string CapacityPreflight(const bool closing = false)
{
   Py000Request request = {};
   request.op = closing ? "close_position" : "submit_market_delta";
   request.binding.symbol = CapacitySymbol();
   request.side = closing ? "sell" : "buy";
   request.quantity_lots = "0.01";
   if(closing) { request.position_ticket = "1000"; request.position_identifier = "2000"; }
   double lots; MqlTick tick; Py000CloseTarget target;
   return Py000ExecutionPreflight(request, lots, tick, target);
}
int OnStart()
{
   if(MQLInfoInteger(MQL_TRADE_ALLOWED) || MQLInfoInteger(MQL_DLLS_ALLOWED)
      || TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      Print("CAPACITY_FAIL actual permissions must remain disabled");
      return 1;
   }
   g_py000_execution_max_order_lots = 0.02;
   g_py000_execution_magic = 7;
   string decimal;
   CapacityCheck("decimal64 allowed", Py000DecimalFromDouble(9.0e63, 8, decimal)
      && StringLen(decimal) == 64);
   CapacityCheck("decimal65 rejected", !Py000DecimalFromDouble(9.0e64, 8, decimal));
   CapacityReset(31);
   CapacityCheck("31 permits prospective ticket 32", CapacityPreflight() == "");
   CapacityReset(32);
   CapacityCheck("32 rejects prospective ticket 33", CapacityPreflight() != "");
   CapacityReset(33);
   CapacityCheck("above cap mixed book exact close preserved", CapacityPreflight(true) == "");
   CapacityReset(32); g_capacity_other_symbol = 31;
   CapacityCheck("other symbol does not consume a slot", CapacityPreflight() == "");
   CapacityReset(32);
   CapacityCheck("same symbol foreign magic consumes a slot", CapacityPreflight() != "");
   for(int invalid = 1; invalid <= 3; invalid++)
   {
      CapacityReset(31); g_capacity_bad = invalid;
      CapacityCheck(StringFormat("invalid or unstable sample %d rejected", invalid),
         CapacityPreflight() != "");
   }
   CapacityReset(31);
   string name = PY000_JOURNAL_PREFIX + "W8_P05_CAP_" + IntegerToString((long)GetMicrosecondCount());
   CapacityCheck("isolated synthetic journal initialized", Py000JournalInit(name));
   if(g_py000_journal_ready)
   {
      CapacityCheck("isolated stream started", Py000JournalAppendStreamStarted(
         "capacity-boot", "1700000000000", "capacity-test", false));
      g_py000_execution_enabled = true; // Only this synthetic getter/OrderSend implementation.
      g_capacity_grow_after_check = true;
      Py000Request request = {};
      request.op = "submit_market_delta"; request.binding.symbol = CapacitySymbol();
      request.client_request_id = "capacity-race"; request.side = "buy";
      request.quantity_lots = "0.01";
      Py000JournalEvent outcome; string code, message;
      bool complete = Py000ExecutionSubmit(request, "capacity-boot", outcome, code, message);
      CapacityCheck("after-check growth recorded as zero-fill rejection",
         complete && outcome.event_type == "order_rejected" && g_capacity_send_calls == 0);
   }
   CapacityCheck("all getter calls mapped", g_capacity_unknown_getters == 0);
   PrintFormat("PY000_CAPACITY_TEST checks=%d failures=%d stub_send_calls=%d namespace=%s",
      g_capacity_checks, g_capacity_failures, g_capacity_send_calls, name);
   return g_capacity_failures == 0 ? 0 : 1;
}
