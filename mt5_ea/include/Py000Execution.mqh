#ifndef PY000_EXECUTION_MQH
#define PY000_EXECUTION_MQH
enum Py000ExecutionMode
{
   PY000_EXECUTION_DISABLED = 0,
   PY000_EXECUTION_DEMO = 1
};
bool g_py000_execution_enabled = false;
double g_py000_execution_max_order_lots = 0.0;
ulong g_py000_execution_deviation_points = 0;
ulong g_py000_execution_magic = 0;
int g_py000_execution_freshness_seconds = 10;
string Py000ExecutionUint(ulong value)
{
   if(value == 0) return "0";
   string result = "";
   while(value > 0)
   {
      result = ShortToString((ushort)('0' + (int)(value % 10))) + result;
      value /= 10;
   }
   return result;
}
string Py000ExecutionUtcMs()
{
   return IntegerToString((long)TimeGMT() * 1000);
}
bool Py000ExecutionDecimal(
   const double value,
   const int digits,
   string &result
)
{
   if(!MathIsValidNumber(value) || digits < 0 || digits > 16)
      return false;
   result = DoubleToString(NormalizeDouble(value, digits), digits);
   while(StringFind(result, ".") >= 0
      && StringGetCharacter(result, StringLen(result) - 1) == '0')
      result = StringSubstr(result, 0, StringLen(result) - 1);
   if(StringGetCharacter(result, StringLen(result) - 1) == '.')
      result = StringSubstr(result, 0, StringLen(result) - 1);
   if(result == "-0") result = "0";
   return Py000JsonSignedDecimal(result);
}
bool Py000ExecutionParseLots(const string source, double &value)
{
   if(!Py000JsonPositiveDecimal(source) || StringLen(source) > 20)
      return false;
   value = 0.0;
   double fraction = 0.1;
   bool after_decimal = false;
   int fractional_digits = 0;
   for(int index = 0; index < StringLen(source); index++)
   {
      ushort code = (ushort)StringGetCharacter(source, index);
      if(code == '.')
      {
         after_decimal = true;
         continue;
      }
      int digit = (int)code - (int)'0';
      if(after_decimal)
      {
         if(++fractional_digits > 8) return false;
         value += digit * fraction;
         fraction *= 0.1;
      }
      else
         value = value * 10.0 + digit;
      if(!MathIsValidNumber(value)) return false;
   }
   value = NormalizeDouble(value, fractional_digits);
   return value > 0.0;
}
bool Py000ExecutionConfigure(
   const Py000ExecutionMode mode,
   const double max_order_lots,
   const ulong deviation_points,
   const ulong magic,
   const int freshness_seconds
)
{
   if((mode != PY000_EXECUTION_DISABLED && mode != PY000_EXECUTION_DEMO)
      || !MathIsValidNumber(max_order_lots) || max_order_lots <= 0.0
      || deviation_points > 100000 || magic == 0
      || freshness_seconds < 1 || freshness_seconds > 300)
      return false;
   g_py000_execution_max_order_lots = max_order_lots;
   g_py000_execution_deviation_points = deviation_points;
   g_py000_execution_magic = magic;
   g_py000_execution_freshness_seconds = freshness_seconds;
   g_py000_execution_enabled = mode == PY000_EXECUTION_DEMO
      && AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO
      && AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
   return true;
}
bool Py000ExecutionVolumeValid(const double lots)
{
   double minimum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maximum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(!MathIsValidNumber(lots) || !MathIsValidNumber(minimum)
      || !MathIsValidNumber(maximum) || !MathIsValidNumber(step)
      || minimum <= 0.0 || maximum < minimum || step <= 0.0
      || lots < minimum || lots > maximum
      || lots > g_py000_execution_max_order_lots)
      return false;
   double units = lots / step;
   double nearest = MathRound(units);
   double tolerance = 1e-9 * MathMax(1.0, MathAbs(units));
   return MathAbs(units - nearest) <= tolerance;
}
string Py000ExecutionPreflight(
   const Py000Request &request,
   double &lots,
   MqlTick &tick
)
{
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO
      || AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      return "ACCOUNT_MODE_CHANGED";
   if(request.binding.symbol != _Symbol)
      return "SYMBOL_MISMATCH";
   if(!(bool)TerminalInfoInteger(TERMINAL_CONNECTED))
      return "TERMINAL_DISCONNECTED";
   if(!(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
      || !(bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)
      || !(bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT)
      || !(bool)MQLInfoInteger(MQL_TRADE_ALLOWED))
      return "TRADING_NOT_ALLOWED";
   long trade_mode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   bool side_allowed = request.side == "buy"
      ? (trade_mode == SYMBOL_TRADE_MODE_FULL
         || trade_mode == SYMBOL_TRADE_MODE_LONGONLY)
      : (trade_mode == SYMBOL_TRADE_MODE_FULL
         || trade_mode == SYMBOL_TRADE_MODE_SHORTONLY);
   if(!side_allowed)
      return "SYMBOL_TRADE_MODE";
   long filling = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) == 0)
      return "FOK_UNAVAILABLE";
   if(!Py000ExecutionParseLots(request.quantity_lots, lots)
      || !Py000ExecutionVolumeValid(lots))
      return "VOLUME_INVALID";
   if(!SymbolInfoTick(_Symbol, tick) || tick.time_msc <= 0
      || tick.bid <= 0.0 || tick.ask <= 0.0 || tick.bid > tick.ask)
      return "TICK_UNAVAILABLE";
   datetime server_time = TimeTradeServer();
   if(server_time <= 0 || tick.time <= 0 || server_time < tick.time
      || server_time - tick.time > g_py000_execution_freshness_seconds)
      return "STALE_TICK";
   return "";
}
bool Py000ExecutionFinishSimple(
   const Py000Request &request,
   const string boot_id,
   const string event_type,
   const string reason,
   const string broker_retcode,
   Py000JournalEvent &outcome,
   string &error_code,
   string &error_message
)
{
   if(Py000JournalAppendRejectedOrUnknown(
         boot_id,
         Py000ExecutionUtcMs(),
         event_type,
         request.client_request_id,
         reason,
         broker_retcode,
         outcome
      ))
      return true;
   g_py000_execution_recovery_ready = false;
   error_code = "RECOVERY_BLOCKED";
   error_message = "terminal outcome could not be persisted";
   return false;
}
bool Py000ExecutionSubmit(
   const Py000Request &request,
   const string boot_id,
   Py000JournalEvent &outcome,
   string &error_code,
   string &error_message
)
{
   error_code = "";
   error_message = "";
   if(!g_py000_journal_ready)
   {
      error_code = "RECOVERY_BLOCKED";
      error_message = "journal recovery is not ready";
      return false;
   }
   int reserved = Py000JournalReservedIndex(request.client_request_id);
   if(reserved >= 0)
   {
      Py000JournalEvent intent = g_py000_journal_events[reserved];
      if(intent.side != request.side || intent.quantity_lots != request.quantity_lots)
      {
         error_code = "IDEMPOTENCY_CONFLICT";
         error_message = "client_request_id payload differs from durable reservation";
         return false;
      }
      int terminal = Py000JournalOutcomeIndex(request.client_request_id);
      if(terminal >= 0)
      {
         outcome = g_py000_journal_events[terminal];
         return true;
      }
      error_code = "RECOVERY_BLOCKED";
      error_message = "submission reservation has no durable terminal outcome";
      return false;
   }
   if(!g_py000_execution_enabled)
   {
      error_code = "EXECUTION_DISABLED";
      error_message = "execution requires explicit DEMO mode on a hedging demo account";
      return false;
   }
   if(!g_py000_journal_ready || !g_py000_execution_recovery_ready)
   {
      error_code = "RECOVERY_BLOCKED";
      error_message = "execution recovery is not ready";
      return false;
   }
   Py000JournalEvent reservation;
   if(!Py000JournalAppendReserved(
         boot_id,
         Py000ExecutionUtcMs(),
         request.client_request_id,
         request.side,
         request.quantity_lots,
         reservation
      ))
   {
      g_py000_execution_recovery_ready = false;
      error_code = "RECOVERY_BLOCKED";
      error_message = "submission reservation could not be persisted";
      return false;
   }
   double lots = 0.0;
   MqlTick tick;
   string preflight = Py000ExecutionPreflight(request, lots, tick);
   if(preflight != "")
      return Py000ExecutionFinishSimple(
         request, boot_id, "order_rejected", preflight, "0",
         outcome, error_code, error_message
      );
   MqlTradeRequest native_request = {};
   native_request.action = TRADE_ACTION_DEAL;
   native_request.magic = g_py000_execution_magic;
   native_request.symbol = _Symbol;
   native_request.volume = lots;
   native_request.price = request.side == "buy" ? tick.ask : tick.bid;
   native_request.deviation = g_py000_execution_deviation_points;
   native_request.type = request.side == "buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   native_request.type_filling = ORDER_FILLING_FOK;
   native_request.comment = "PY000-" + StringSubstr(request.client_request_id, 0, 24);
   MqlTradeCheckResult check = {};
   ResetLastError();
   if(!OrderCheck(native_request, check) || check.retcode != 0)
      return Py000ExecutionFinishSimple(
         request, boot_id, "order_rejected", "ORDER_CHECK_REJECTED",
         Py000ExecutionUint((ulong)check.retcode),
         outcome, error_code, error_message
      );
   MqlTradeResult result = {};
   ResetLastError();
   bool sent = OrderSend(native_request, result);
   string retcode = Py000ExecutionUint((ulong)result.retcode);
   if(!sent || result.retcode != TRADE_RETCODE_DONE
      || result.order == 0 || result.deal == 0
      || !HistoryDealSelect(result.deal))
      return Py000ExecutionFinishSimple(
         request, boot_id, "order_unknown", "ORDER_SEND_UNCERTAIN", retcode,
         outcome, error_code, error_message
      );
   long venue_order_raw = 0;
   long venue_position_raw = 0;
   long deal_magic_raw = 0;
   long deal_type = 0;
   string deal_symbol = "";
   double filled = 0.0;
   double price = 0.0;
   double commission = 0.0;
   bool facts_read = HistoryDealGetInteger(
         result.deal, DEAL_ORDER, venue_order_raw
      )
      && HistoryDealGetInteger(
         result.deal, DEAL_POSITION_ID, venue_position_raw
      )
      && HistoryDealGetInteger(result.deal, DEAL_MAGIC, deal_magic_raw)
      && HistoryDealGetInteger(result.deal, DEAL_TYPE, deal_type)
      && HistoryDealGetString(result.deal, DEAL_SYMBOL, deal_symbol)
      && HistoryDealGetDouble(result.deal, DEAL_VOLUME, filled)
      && HistoryDealGetDouble(result.deal, DEAL_PRICE, price)
      && HistoryDealGetDouble(result.deal, DEAL_COMMISSION, commission);
   ulong venue_order = venue_order_raw > 0 ? (ulong)venue_order_raw : 0;
   ulong venue_position = venue_position_raw > 0 ? (ulong)venue_position_raw : 0;
   ulong deal_magic = deal_magic_raw >= 0 ? (ulong)deal_magic_raw : 0;
   bool matching_side = request.side == "buy"
      ? deal_type == DEAL_TYPE_BUY : deal_type == DEAL_TYPE_SELL;
   double quantity_tolerance = 1e-9 * MathMax(1.0, MathAbs(lots));
   string filled_text, price_text, commission_text;
   if(!facts_read || venue_order == 0 || venue_order != result.order
      || venue_position == 0
      || deal_magic != g_py000_execution_magic || deal_symbol != _Symbol
      || !matching_side || filled <= 0.0 || price <= 0.0
      || MathAbs(filled - lots) > quantity_tolerance
      || !Py000ExecutionDecimal(filled, 8, filled_text)
      || !Py000ExecutionDecimal(price, _Digits, price_text)
      || !Py000ExecutionDecimal(commission, 8, commission_text))
      return Py000ExecutionFinishSimple(
         request, boot_id, "order_unknown", "FILL_FACT_UNAVAILABLE", retcode,
         outcome, error_code, error_message
      );
   if(Py000JournalAppendFilled(
         boot_id,
         Py000ExecutionUtcMs(),
         request.client_request_id,
         filled_text,
         price_text,
         Py000ExecutionUint(venue_order),
         Py000ExecutionUint(result.deal),
         Py000ExecutionUint(venue_position),
         commission_text,
         retcode,
         outcome
      ))
      return true;
   g_py000_execution_recovery_ready = false;
   error_code = "RECOVERY_BLOCKED";
   error_message = "fill outcome could not be persisted";
   return false;
}
#endif
