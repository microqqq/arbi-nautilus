#ifndef PY000_PROTOCOL_MQH
#define PY000_PROTOCOL_MQH
#define PY000_PROTOCOL "py000.mt5"
#define PY000_VERSION 1
#define PY000_PROTOCOL_MAX_WIRE 65536
struct Py000Identity
{
   string account_id;
   string broker_server;
   string broker_login;
   string server_timezone;
   string symbol;
   int terminal_build;
   string ea_build_id;
   string declared_source_sha256;
   string stream_id;
   string boot_id;
   string magic;
   bool execution_enabled;
};
Py000Identity g_py000_identity;
int g_py000_session_freshness_seconds = 10;
string Py000JsonEscape(const string value)
{
   string result = "";
   for(int index = 0; index < StringLen(value); index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(code == '"') result += "\\\"";
      else if(code == '\\') result += "\\\\";
      else if(code == 0x08) result += "\\b";
      else if(code == 0x0c) result += "\\f";
      else if(code == 0x0a) result += "\\n";
      else if(code == 0x0d) result += "\\r";
      else if(code == 0x09) result += "\\t";
      else if(code < 0x20) result += StringFormat("\\u%04x", (int)code);
      else result += ShortToString(code);
   }
   return result;
}
string Py000Quoted(const string value)
{
   return "\"" + Py000JsonEscape(value) + "\"";
}
string Py000Bool(const bool value)
{
   return value ? "true" : "false";
}
int Py000Utf8Size(const string value)
{
   uchar encoded[];
   int count = StringToCharArray(value, encoded, 0, WHOLE_ARRAY, CP_UTF8);
   return count > 0 ? count - 1 : -1;
}
string Py000UlongString(ulong value)
{
   if(value == 0)
      return "0";
   string result = "";
   while(value > 0)
   {
      int digit = (int)(value % 10);
      result = ShortToString((ushort)('0' + digit)) + result;
      value /= 10;
   }
   return result;
}
bool Py000DecimalFromDouble(const double value, const int digits, string &result)
{
   if(!MathIsValidNumber(value) || digits < 0 || digits > 16)
      return false;
   double normalized = NormalizeDouble(value, digits);
   if(normalized == 0.0)
      normalized = 0.0;
   result = DoubleToString(normalized, digits);
   bool has_decimal = false;
   for(int index = 0; index < StringLen(result); index++)
      if(StringGetCharacter(result, index) == '.')
         has_decimal = true;
   if(has_decimal)
   {
      while(StringLen(result) > 1
         && StringGetCharacter(result, StringLen(result) - 1) == '0')
         result = StringSubstr(result, 0, StringLen(result) - 1);
      if(StringLen(result) > 1
         && StringGetCharacter(result, StringLen(result) - 1) == '.')
         result = StringSubstr(result, 0, StringLen(result) - 1);
   }
   return true;
}
string Py000ObservedUtcMs()
{
   return IntegerToString((long)TimeGMT() * 1000);
}
bool Py000Sha256Hex(uchar &canonical[], string &hex)
{
   uchar key[], digest[];
   if(CryptEncode(CRYPT_HASH_SHA256, canonical, key, digest) != 32)
      return false;
   string alphabet = "0123456789abcdef";
   hex = "";
   for(int index = 0; index < ArraySize(digest); index++)
   {
      hex += StringSubstr(alphabet, (digest[index] >> 4) & 0x0f, 1);
      hex += StringSubstr(alphabet, digest[index] & 0x0f, 1);
   }
   return StringLen(hex) == 64;
}
bool Py000DeriveAccountId(
   const string broker_server,
   const string broker_login,
   string &account_id
)
{
   uchar domain[], server[], login[];
   int domain_count = StringToCharArray(
      "py000.mt5/account-id/v1", domain, 0, WHOLE_ARRAY, CP_UTF8
   );
   int server_count = StringToCharArray(
      broker_server, server, 0, WHOLE_ARRAY, CP_UTF8
   );
   int login_count = StringToCharArray(
      broker_login, login, 0, WHOLE_ARRAY, CP_UTF8
   );
   if(domain_count <= 1 || server_count <= 1 || login_count <= 1)
      return false;
   int server_bytes = server_count - 1;
   uchar canonical[];
   int total = domain_count + 4 + server_bytes + 1 + login_count - 1;
   ArrayResize(canonical, total);
   int offset = 0;
   for(int index = 0; index < domain_count; index++)
      canonical[offset++] = domain[index];
   canonical[offset++] = (uchar)((server_bytes >> 24) & 0xff);
   canonical[offset++] = (uchar)((server_bytes >> 16) & 0xff);
   canonical[offset++] = (uchar)((server_bytes >> 8) & 0xff);
   canonical[offset++] = (uchar)(server_bytes & 0xff);
   for(int index = 0; index < server_bytes; index++)
      canonical[offset++] = server[index];
   canonical[offset++] = 0;
   for(int index = 0; index < login_count - 1; index++)
      canonical[offset++] = login[index];
   string hex;
   if(!Py000Sha256Hex(canonical, hex)) return false;
   account_id = "mt5-sha256-" + hex;
   return true;
}
bool Py000LowerSha256(const string value)
{
   if(StringLen(value) != 64)
      return false;
   bool nonzero = false;
   for(int index = 0; index < 64; index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(!((code >= '0' && code <= '9') || (code >= 'a' && code <= 'f')))
         return false;
      if(code != '0')
         nonzero = true;
   }
   return nonzero;
}
bool Py000OptionalBrokerText(const string value, const int maximum)
{
   if(value == "")
      return true;
   if(StringLen(value) > maximum)
      return false;
   if(Py000JsonIsWhitespace((ushort)StringGetCharacter(value, 0))
      || Py000JsonIsWhitespace((ushort)StringGetCharacter(value, StringLen(value) - 1)))
      return false;
   for(int index = 0; index < StringLen(value); index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(code == 0 || code < 0x20 || (code >= 0x7f && code <= 0x9f)
         || (code >= 0xd800 && code <= 0xdfff))
         return false;
   }
   return true;
}
bool Py000BuildIdentity(
   const string ea_build_id,
   const string declared_source_sha256,
   const string server_timezone,
   const ulong magic,
   const string stream_id,
   const string boot_id,
   const bool execution_enabled
)
{
   string server = AccountInfoString(ACCOUNT_SERVER);
   ulong login = (ulong)AccountInfoInteger(ACCOUNT_LOGIN);
   string login_text = Py000UlongString(login);
   if(server == "" || !Py000OptionalBrokerText(server, 128)
      || !Py000JsonVisibleIdentifier(_Symbol, 128)
      || !Py000JsonSafeToken(ea_build_id)
      || !Py000LowerSha256(declared_source_sha256)
      || server_timezone == "" || !Py000OptionalBrokerText(server_timezone, 64)
      || !Py000JsonSafeToken(stream_id) || !Py000JsonSafeToken(boot_id)
      || login == 0)
      return false;
   string account_id;
   if(!Py000DeriveAccountId(server, login_text, account_id)
      || !Py000JsonVisibleIdentifier(account_id, 128))
      return false;
   g_py000_identity.account_id = account_id;
   g_py000_identity.broker_server = server;
   g_py000_identity.broker_login = login_text;
   g_py000_identity.server_timezone = server_timezone;
   g_py000_identity.symbol = _Symbol;
   g_py000_identity.terminal_build = (int)TerminalInfoInteger(TERMINAL_BUILD);
   g_py000_identity.ea_build_id = ea_build_id;
   g_py000_identity.declared_source_sha256 = declared_source_sha256;
   g_py000_identity.stream_id = stream_id;
   g_py000_identity.boot_id = boot_id;
   g_py000_identity.magic = Py000UlongString(magic);
   g_py000_identity.execution_enabled = execution_enabled;
   return g_py000_identity.terminal_build > 0;
}
string Py000IdentityJson()
{
   return "{\"account_id\":" + Py000Quoted(g_py000_identity.account_id)
      + ",\"broker_login\":" + Py000Quoted(g_py000_identity.broker_login)
      + ",\"broker_server\":" + Py000Quoted(g_py000_identity.broker_server)
      + ",\"server_timezone\":" + Py000Quoted(g_py000_identity.server_timezone)
      + ",\"server_timezone_source\":\"configured_input\""
      + ",\"boot_id\":" + Py000Quoted(g_py000_identity.boot_id)
      + ",\"ea_build_id\":" + Py000Quoted(g_py000_identity.ea_build_id)
      + ",\"declared_source_sha256\":" + Py000Quoted(g_py000_identity.declared_source_sha256)
      + ",\"execution_enabled\":" + Py000Bool(g_py000_identity.execution_enabled)
      + ",\"magic\":" + Py000Quoted(g_py000_identity.magic)
      + ",\"stream_id\":" + Py000Quoted(g_py000_identity.stream_id)
      + ",\"symbol\":" + Py000Quoted(g_py000_identity.symbol)
      + ",\"terminal_build\":" + IntegerToString(g_py000_identity.terminal_build)
      + "}";
}
bool Py000BindingMatches(const Py000Binding &binding)
{
   return binding.account_id == g_py000_identity.account_id
      && binding.symbol == g_py000_identity.symbol
      && binding.ea_build_id == g_py000_identity.ea_build_id
      && binding.stream_id == g_py000_identity.stream_id
      && binding.boot_id == g_py000_identity.boot_id;
}
string Py000ResponsePrefix(
   const string request_id,
   const string op,
   const bool ok
)
{
   return "{\"protocol\":\"py000.mt5\",\"version\":1,\"request_id\":"
      + Py000Quoted(request_id) + ",\"op\":" + Py000Quoted(op)
      + ",\"ok\":" + Py000Bool(ok);
}
string Py000ErrorResponse(
   const string request_id,
   const string op,
   const string code,
   const string message
)
{
   return Py000ResponsePrefix(request_id, op, false)
      + ",\"error\":{\"code\":" + Py000Quoted(code)
      + ",\"message\":" + Py000Quoted(message) + "}}";
}
string Py000HelloResponse(const Py000Request &request)
{
   string recovery = g_py000_journal_ready && g_py000_execution_recovery_ready
      ? "ready" : "blocked";
   string data = "{\"capabilities\":[\"close_position\",\"get_execution_events\","
      + "\"get_snapshot\",\"hello\",\"submit_market_delta\"],\"identity\":"
      + Py000IdentityJson() + ",\"recovery_state\":" + Py000Quoted(recovery) + "}";
   return Py000ResponsePrefix(request.request_id, request.op, true)
      + ",\"data\":" + data + "}";
}
int Py000SecondsOfDay(const datetime value)
{
   MqlDateTime parts;
   TimeToStruct(value, parts);
   return parts.hour * 3600 + parts.min * 60 + parts.sec;
}
bool Py000BuildSessionJson(
   const datetime sample_time,
   const bool terminal_connected,
   const bool tick_available,
   const MqlTick &tick,
   string &json
)
{
   bool schedule_available = false;
   bool scheduled_open = false;
   if(sample_time > 0)
   {
      MqlDateTime sample;
      TimeToStruct(sample_time, sample);
      int sample_seconds = Py000SecondsOfDay(sample_time);
      for(uint index = 0; index < 64; index++)
      {
         datetime session_from, session_to;
         if(!SymbolInfoSessionTrade(
               _Symbol,
               (ENUM_DAY_OF_WEEK)sample.day_of_week,
               index,
               session_from,
               session_to
            ))
            break;
         schedule_available = true;
         int from_seconds = Py000SecondsOfDay(session_from);
         int to_seconds = Py000SecondsOfDay(session_to);
         bool inside = from_seconds == to_seconds
            || (from_seconds < to_seconds
               ? sample_seconds >= from_seconds && sample_seconds < to_seconds
               : sample_seconds >= from_seconds || sample_seconds < to_seconds);
         if(inside) scheduled_open = true;
      }
   }
   string freshness = "unknown";
   string age_json = "null";
   if(sample_time > 0 && tick_available && tick.time > 0 && sample_time >= tick.time)
   {
      long age_seconds = (long)(sample_time - tick.time);
      freshness = age_seconds <= g_py000_session_freshness_seconds ? "fresh" : "stale";
      age_json = Py000Quoted(IntegerToString(age_seconds * 1000));
   }
   bool session_open = scheduled_open && terminal_connected && freshness == "fresh";
   string sample_json = sample_time > 0
      ? Py000Quoted(IntegerToString((long)sample_time * 1000)) : "null";
   json = "{\"freshness\":" + Py000Quoted(freshness)
      + ",\"freshness_age_ms\":" + age_json
      + ",\"freshness_source\":\"mt5_symbol_info_tick_vs_time_trade_server\""
      + ",\"sample_server_time_ms\":" + sample_json
      + ",\"sample_server_time_source\":\"mt5_time_trade_server_calculated_second_precision\""
      + ",\"scheduled_open\":" + Py000Bool(scheduled_open)
      + ",\"session_open\":" + Py000Bool(session_open)
      + ",\"session_schedule_available\":" + Py000Bool(schedule_available)
      + ",\"session_source\":\"mt5_symbol_info_session_trade_at_time_trade_server\"}";
   return true;
}
bool Py000BuildSymbolSpecJson(string &json)
{
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   string point, tick_size, tick_value, tick_value_profit, tick_value_loss;
   string contract_size, volume_min, volume_max, volume_step, volume_limit;
   string margin_initial, margin_maintenance, swap_long, swap_short;
   string swap_sunday, swap_monday, swap_tuesday, swap_wednesday;
   string swap_thursday, swap_friday, swap_saturday;
   string currency_base = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_BASE);
   string currency_margin = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_MARGIN);
   string currency_profit = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_PROFIT);
   if(!Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_POINT), digits, point)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE), digits, tick_size)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE), 8, tick_value)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_PROFIT), 8, tick_value_profit)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS), 8, tick_value_loss)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE), 8, contract_size)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), 8, volume_min)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX), 8, volume_max)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP), 8, volume_step)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_LIMIT), 8, volume_limit)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_MARGIN_INITIAL), 8, margin_initial)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_MARGIN_MAINTENANCE), 8, margin_maintenance)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_LONG), 8, swap_long)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_SHORT), 8, swap_short)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_SUNDAY), 8, swap_sunday)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_MONDAY), 8, swap_monday)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_TUESDAY), 8, swap_tuesday)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_WEDNESDAY), 8, swap_wednesday)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_THURSDAY), 8, swap_thursday)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_FRIDAY), 8, swap_friday)
      || !Py000DecimalFromDouble(SymbolInfoDouble(_Symbol, SYMBOL_SWAP_SATURDAY), 8, swap_saturday)
      || !Py000JsonVisibleIdentifier(currency_base, 16)
      || !Py000JsonVisibleIdentifier(currency_margin, 16)
      || !Py000JsonVisibleIdentifier(currency_profit, 16))
      return false;
   json = "{\"contract_size\":" + Py000Quoted(contract_size)
      + ",\"currency_base\":" + Py000Quoted(currency_base)
      + ",\"currency_margin\":" + Py000Quoted(currency_margin)
      + ",\"currency_profit\":" + Py000Quoted(currency_profit)
      + ",\"digits\":" + IntegerToString(digits)
      + ",\"expiration_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_EXPIRATION_MODE))
      + ",\"filling_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE))
      + ",\"freeze_level\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL))
      + ",\"margin_initial\":" + Py000Quoted(margin_initial)
      + ",\"margin_maintenance\":" + Py000Quoted(margin_maintenance)
      + ",\"order_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_ORDER_MODE))
      + ",\"point\":" + Py000Quoted(point)
      + ",\"stops_level\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL))
      + ",\"symbol\":" + Py000Quoted(_Symbol)
      + ",\"swap_long\":" + Py000Quoted(swap_long)
      + ",\"swap_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_SWAP_MODE))
      + ",\"swap_rates\":[" + Py000Quoted(swap_sunday)
      + "," + Py000Quoted(swap_monday)
      + "," + Py000Quoted(swap_tuesday)
      + "," + Py000Quoted(swap_wednesday)
      + "," + Py000Quoted(swap_thursday)
      + "," + Py000Quoted(swap_friday)
      + "," + Py000Quoted(swap_saturday) + "]"
      + ",\"swap_short\":" + Py000Quoted(swap_short)
      + ",\"tick_size\":" + Py000Quoted(tick_size)
      + ",\"tick_value\":" + Py000Quoted(tick_value)
      + ",\"tick_value_loss\":" + Py000Quoted(tick_value_loss)
      + ",\"tick_value_profit\":" + Py000Quoted(tick_value_profit)
      + ",\"trade_calc_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_CALC_MODE))
      + ",\"trade_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE))
      + ",\"volume_limit\":" + Py000Quoted(volume_limit)
      + ",\"volume_max\":" + Py000Quoted(volume_max)
      + ",\"volume_min\":" + Py000Quoted(volume_min)
      + ",\"volume_step\":" + Py000Quoted(volume_step) + "}";
   return true;
}
bool Py000BuildAccountJson(string &json)
{
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   string balance, equity, margin, margin_free, margin_level;
   if(!Py000JsonVisibleIdentifier(currency, 16)
      || !Py000DecimalFromDouble(AccountInfoDouble(ACCOUNT_BALANCE), 8, balance)
      || !Py000DecimalFromDouble(AccountInfoDouble(ACCOUNT_EQUITY), 8, equity)
      || !Py000DecimalFromDouble(AccountInfoDouble(ACCOUNT_MARGIN), 8, margin)
      || !Py000DecimalFromDouble(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 8, margin_free)
      || !Py000DecimalFromDouble(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 8, margin_level))
      return false;
   json = "{\"balance\":" + Py000Quoted(balance)
      + ",\"broker_login\":" + Py000Quoted(g_py000_identity.broker_login)
      + ",\"broker_server\":" + Py000Quoted(g_py000_identity.broker_server)
      + ",\"currency\":" + Py000Quoted(currency)
      + ",\"equity\":" + Py000Quoted(equity)
      + ",\"leverage\":" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LEVERAGE))
      + ",\"margin\":" + Py000Quoted(margin)
      + ",\"margin_free\":" + Py000Quoted(margin_free)
      + ",\"margin_level\":" + Py000Quoted(margin_level)
      + ",\"trade_allowed\":" + Py000Bool((bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
      + ",\"trade_expert\":" + Py000Bool((bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
      + "}";
   return true;
}
// These five native getters can be replaced by the isolated snapshot test script.
#ifndef PY000_SNAPSHOT_TOTAL
#define PY000_SNAPSHOT_TOTAL PositionsTotal
#define PY000_SNAPSHOT_TICKET PositionGetTicket
#define PY000_SNAPSHOT_STRING PositionGetString
#define PY000_SNAPSHOT_INTEGER PositionGetInteger
#define PY000_SNAPSHOT_DOUBLE PositionGetDouble
#endif
struct Py000PositionSample
{
   ulong ticket;
   long identifier;
   string symbol;
   long magic;
   long side;
   double volume;
   string json;
};
bool Py000CollectPositions(Py000PositionSample &samples[], string &error_code)
{
   error_code = "SNAPSHOT_UNAVAILABLE";
   ResetLastError();
   int total = PY000_SNAPSHOT_TOTAL();
   if(total < 0 || GetLastError() != 0 || ArrayResize(samples, total) != total)
      return false;
   int count = 0;
   for(int index = 0; index < total; index++)
   {
      ulong ticket = PY000_SNAPSHOT_TICKET(index);
      if(ticket == 0)
         return false;
      string symbol;
      if(!PY000_SNAPSHOT_STRING(POSITION_SYMBOL, symbol) || StringLen(symbol) == 0)
         return false;
      if(symbol != _Symbol)
         continue;
      Py000PositionSample row;
      row.ticket = ticket;
      row.symbol = symbol;
      long ticket_raw, time_msc;
      string comment;
      double open_raw, current_raw, sl_raw, tp_raw, profit_raw, swap_raw;
      if(!PY000_SNAPSHOT_INTEGER(POSITION_TICKET, ticket_raw)
         || !PY000_SNAPSHOT_INTEGER(POSITION_IDENTIFIER, row.identifier)
         || !PY000_SNAPSHOT_INTEGER(POSITION_MAGIC, row.magic)
         || !PY000_SNAPSHOT_INTEGER(POSITION_TYPE, row.side)
         || !PY000_SNAPSHOT_INTEGER(POSITION_TIME_MSC, time_msc)
         || !PY000_SNAPSHOT_STRING(POSITION_COMMENT, comment)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_VOLUME, row.volume)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_PRICE_OPEN, open_raw)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_PRICE_CURRENT, current_raw)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_SL, sl_raw)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_TP, tp_raw)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_PROFIT, profit_raw)
         || !PY000_SNAPSHOT_DOUBLE(POSITION_SWAP, swap_raw)
         || ticket_raw <= 0 || (ulong)ticket_raw != ticket || row.identifier <= 0
         || row.magic < 0 || time_msc <= 0
         || (row.side != POSITION_TYPE_BUY && row.side != POSITION_TYPE_SELL)
         || !MathIsValidNumber(row.volume) || row.volume <= 0.0)
         return false;
      for(int previous = 0; previous < count; previous++)
         if(samples[previous].ticket == ticket
            || samples[previous].identifier == row.identifier)
            return false;
      string volume, open_price, current_price, stop_loss, take_profit, profit, swap;
      if(!Py000OptionalBrokerText(comment, 128)
         || !Py000DecimalFromDouble(row.volume, 8, volume)
         || !Py000JsonPositiveDecimal(volume)
         || !Py000DecimalFromDouble(open_raw, _Digits, open_price)
         || !Py000DecimalFromDouble(current_raw, _Digits, current_price)
         || !Py000DecimalFromDouble(sl_raw, _Digits, stop_loss)
         || !Py000DecimalFromDouble(tp_raw, _Digits, take_profit)
         || !Py000DecimalFromDouble(profit_raw, 8, profit)
         || !Py000DecimalFromDouble(swap_raw, 8, swap))
      {
         error_code = "SCHEMA_MISMATCH";
         return false;
      }
      string side = row.side == POSITION_TYPE_BUY ? "buy" : "sell";
      row.json = "{\"comment\":" + Py000Quoted(comment)
         + ",\"identifier\":" + Py000Quoted(Py000UlongString((ulong)row.identifier))
         + ",\"magic\":" + Py000Quoted(Py000UlongString((ulong)row.magic))
         + ",\"price_current\":" + Py000Quoted(current_price)
         + ",\"price_open\":" + Py000Quoted(open_price)
         + ",\"profit\":" + Py000Quoted(profit)
         + ",\"side\":" + Py000Quoted(side)
         + ",\"stop_loss\":" + Py000Quoted(stop_loss)
         + ",\"swap\":" + Py000Quoted(swap)
         + ",\"take_profit\":" + Py000Quoted(take_profit)
         + ",\"ticket\":" + Py000Quoted(Py000UlongString(ticket))
         + ",\"time_msc\":" + Py000Quoted(Py000UlongString((ulong)time_msc))
         + ",\"volume_lots\":" + Py000Quoted(volume) + "}";
      samples[count++] = row;
   }
   ResetLastError();
   int final_total = PY000_SNAPSHOT_TOTAL();
   return final_total == total && GetLastError() == 0
      && ArrayResize(samples, count) == count;
}
bool Py000PositionSamplesMatch(
   const Py000PositionSample &first[],
   const Py000PositionSample &second[]
)
{
   if(ArraySize(first) != ArraySize(second))
      return false;
   for(int index = 0; index < ArraySize(first); index++)
   {
      bool found = false;
      for(int other = 0; other < ArraySize(second); other++)
         if(first[index].ticket == second[other].ticket
            && first[index].identifier == second[other].identifier
            && first[index].symbol == second[other].symbol
            && first[index].magic == second[other].magic
            && first[index].side == second[other].side
            && first[index].volume == second[other].volume)
         {
            found = true;
            break;
         }
      if(!found) return false;
   }
   return true;
}
bool Py000BuildPositionsJson(string &json, string &error_code)
{
   json = "";
   Py000PositionSample first[], second[];
   if(!Py000CollectPositions(first, error_code)
      || !Py000CollectPositions(second, error_code)
      || !Py000PositionSamplesMatch(first, second))
      return false;
   json = "[";
   for(int index = 0; index < ArraySize(second); index++)
   {
      if(index > 0) json += ",";
      json += second[index].json;
   }
   json += "]";
   error_code = "";
   return true;
}
bool Py000BuildSnapshotData(string &json, string &error_code)
{
   json = "";
   error_code = "SCHEMA_MISMATCH";
   datetime server_quote_time = TimeCurrent();
   datetime session_sample_time = TimeTradeServer();
   datetime observed_utc = TimeGMT();
   string symbol_spec, account, positions, session, max_order_lots;
   MqlTick tick;
   bool tick_available = SymbolInfoTick(_Symbol, tick);
   bool terminal_connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool market_open_hint = tick_available
      && SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_DISABLED
      && tick.bid > 0.0 && tick.ask > 0.0;
   if(server_quote_time <= 0 || observed_utc <= 0
      || !Py000BuildSymbolSpecJson(symbol_spec)
      || !Py000BuildAccountJson(account)
      || !Py000DecimalFromDouble(
         g_py000_execution_max_order_lots, 8, max_order_lots
      )
      || !Py000BuildSessionJson(
         session_sample_time, terminal_connected, tick_available, tick, session
      ))
      return false;
   if(!Py000BuildPositionsJson(positions, error_code))
      return false;
   string flags = "{\"account_trade_allowed\":"
      + Py000Bool((bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
      + ",\"account_trade_expert\":" + Py000Bool((bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
      + ",\"market_open_hint\":" + Py000Bool(market_open_hint)
      + ",\"mql_trade_allowed\":" + Py000Bool((bool)MQLInfoInteger(MQL_TRADE_ALLOWED))
      + ",\"symbol_trade_mode\":" + IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE))
      + ",\"terminal_connected\":" + Py000Bool(terminal_connected)
      + ",\"terminal_trade_allowed\":" + Py000Bool((bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      + "}";
   string recovery = g_py000_journal_ready && g_py000_execution_recovery_ready
      ? "ready" : "blocked";
   string time_json = "{\"observed_utc_ms\":"
      + Py000Quoted(IntegerToString((long)observed_utc * 1000))
      + ",\"observed_utc_precision_ms\":\"1000\""
      + ",\"observed_utc_source\":\"mt5_time_gmt_second_precision\""
      + ",\"server_quote_time_ms\":"
      + Py000Quoted(IntegerToString((long)server_quote_time * 1000))
      + ",\"server_quote_time_source\":\"mt5_time_current_last_known_quote\""
      + ",\"server_timezone\":" + Py000Quoted(g_py000_identity.server_timezone) + "}";
   json = "{\"account\":" + account
      + ",\"authority_flags\":" + flags
      + ",\"execution_enabled\":" + Py000Bool(g_py000_identity.execution_enabled)
      + ",\"execution_limits\":{\"max_order_lots\":"
      + Py000Quoted(max_order_lots) + "}"
      + ",\"identity\":" + Py000IdentityJson()
      + ",\"positions\":" + positions
      + ",\"recovery_state\":" + Py000Quoted(recovery)
      + ",\"session\":" + session
      + ",\"symbol_spec\":" + symbol_spec
      + ",\"time\":" + time_json + "}";
   return true;
}
string Py000SnapshotResponse(const Py000Request &request)
{
   string data, error_code;
   if(!Py000BuildSnapshotData(data, error_code))
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         error_code,
         error_code == "SNAPSHOT_UNAVAILABLE"
            ? "complete stable native positions snapshot is temporarily unavailable"
            : "native snapshot value cannot satisfy v1 grammar"
      );
   return Py000ResponsePrefix(request.request_id, request.op, true)
      + ",\"data\":" + data + "}";
}
bool Py000JournalEventJson(const Py000JournalEvent &event, string &json)
{
   if(event.event_type != "stream_started"
      && !Py000JournalCloseTargetValid(
         event.position_ticket, event.position_identifier
      ))
      return false;
   string payload = "";
   string close_target = "";
   if(Py000JournalHasCloseTarget(event))
      close_target = ",\"position_identifier\":"
         + Py000Quoted(event.position_identifier)
         + ",\"position_ticket\":" + Py000Quoted(event.position_ticket);
   if(event.event_type == "stream_started")
      payload = "{\"ea_build_id\":" + Py000Quoted(event.ea_build_id)
         + ",\"execution_enabled\":" + Py000Bool(event.execution_enabled) + "}";
   else if(event.event_type == "submission_reserved")
      payload = "{\"client_request_id\":" + Py000Quoted(event.client_request_id)
         + ",\"side\":" + Py000Quoted(event.side)
         + ",\"quantity_lots\":" + Py000Quoted(event.quantity_lots)
         + close_target + "}";
   else if(event.event_type == "order_rejected" || event.event_type == "order_unknown")
      payload = "{\"client_request_id\":" + Py000Quoted(event.client_request_id)
         + ",\"side\":" + Py000Quoted(event.side)
         + ",\"quantity_lots\":" + Py000Quoted(event.quantity_lots)
         + ",\"reason\":" + Py000Quoted(event.reason)
         + ",\"broker_retcode\":" + Py000Quoted(event.broker_retcode)
         + close_target + "}";
   else if(event.event_type == "order_filled")
      payload = "{\"client_request_id\":" + Py000Quoted(event.client_request_id)
         + ",\"side\":" + Py000Quoted(event.side)
         + ",\"quantity_lots\":" + Py000Quoted(event.quantity_lots)
         + ",\"filled_quantity_lots\":" + Py000Quoted(event.filled_quantity_lots)
         + ",\"fill_price\":" + Py000Quoted(event.fill_price)
         + ",\"venue_order_id\":" + Py000Quoted(event.venue_order_id)
         + ",\"venue_deal_id\":" + Py000Quoted(event.venue_deal_id)
         + ",\"venue_position_id\":" + Py000Quoted(event.venue_position_id)
         + ",\"commission\":" + Py000Quoted(event.commission)
         + ",\"broker_retcode\":" + Py000Quoted(event.broker_retcode)
         + close_target + "}";
   else
      return false;
   json = "{\"boot_id\":" + Py000Quoted(event.boot_id)
      + ",\"event_seq\":" + Py000Quoted(event.event_seq)
      + ",\"event_time_ms\":" + Py000Quoted(event.event_time_ms)
      + ",\"event_type\":" + Py000Quoted(event.event_type)
      + ",\"payload\":" + payload
      + ",\"stream_id\":" + Py000Quoted(event.stream_id) + "}";
   return true;
}
string Py000EventsResponse(const Py000Request &request)
{
   if(!g_py000_journal_ready)
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         "RECOVERY_BLOCKED",
         "journal recovery is blocked"
      );
   string last = Py000JournalLastCursor();
   if(Py000JournalCompareUint(request.after_cursor, last) > 0)
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         "CURSOR_AHEAD",
         "after_cursor exceeds last_cursor"
      );
   if(Py000JournalCompareUint(request.after_cursor, g_py000_journal_first_cursor) < 0)
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         "CURSOR_EXPIRED",
         "after_cursor precedes retained stream"
      );
   string events_body = "";
   string next = request.after_cursor;
   int returned = 0;
   for(int index = 0; index < ArraySize(g_py000_journal_events); index++)
   {
      Py000JournalEvent event = g_py000_journal_events[index];
      if(Py000JournalCompareUint(event.event_seq, request.after_cursor) <= 0)
         continue;
      if(returned >= request.limit)
         break;
      string event_json;
      if(!Py000JournalEventJson(event, event_json))
         return Py000ErrorResponse(
            request.request_id,
            request.op,
            "SCHEMA_MISMATCH",
            "journal event cannot satisfy v1 grammar"
         );
      string candidate_body = events_body;
      if(returned > 0) candidate_body += ",";
      candidate_body += event_json;
      string candidate_next = event.event_seq;
      string candidate_data = "{\"events\":[" + candidate_body + "]"
         + ",\"first_retained_cursor\":" + Py000Quoted(g_py000_journal_first_cursor)
         + ",\"has_more\":"
         + Py000Bool(Py000JournalCompareUint(candidate_next, last) < 0)
         + ",\"identity\":" + Py000IdentityJson()
         + ",\"last_cursor\":" + Py000Quoted(last)
         + ",\"next_cursor\":" + Py000Quoted(candidate_next)
         + ",\"stream_id\":" + Py000Quoted(g_py000_journal_stream_id) + "}";
      string candidate_response = Py000ResponsePrefix(
         request.request_id, request.op, true
      ) + ",\"data\":" + candidate_data + "}";
      int candidate_size = Py000Utf8Size(candidate_response);
      if(candidate_size < 0 || candidate_size > PY000_PROTOCOL_MAX_WIRE)
         break;
      events_body = candidate_body;
      next = candidate_next;
      returned++;
   }
   if(returned == 0 && Py000JournalCompareUint(request.after_cursor, last) < 0)
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         "SCHEMA_MISMATCH",
         "single event exceeds wire budget"
      );
   string data = "{\"events\":[" + events_body + "]"
      + ",\"first_retained_cursor\":" + Py000Quoted(g_py000_journal_first_cursor)
      + ",\"has_more\":" + Py000Bool(Py000JournalCompareUint(next, last) < 0)
      + ",\"identity\":" + Py000IdentityJson()
      + ",\"last_cursor\":" + Py000Quoted(last)
      + ",\"next_cursor\":" + Py000Quoted(next)
      + ",\"stream_id\":" + Py000Quoted(g_py000_journal_stream_id) + "}";
   return Py000ResponsePrefix(request.request_id, request.op, true)
      + ",\"data\":" + data + "}";
}
string Py000SubmitResponse(const Py000Request &request)
{
   Py000JournalEvent outcome;
   string error_code;
   string error_message;
   if(!Py000ExecutionSubmit(
         request,
         g_py000_identity.boot_id,
         outcome,
         error_code,
         error_message
      ))
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         error_code,
         error_message
      );
   string outcome_json;
   if(!Py000JournalIsTerminal(outcome.event_type)
      || !Py000JournalEventJson(outcome, outcome_json))
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         "SCHEMA_MISMATCH",
         "terminal execution outcome cannot satisfy v1 grammar"
      );
   string data = "{\"identity\":" + Py000IdentityJson()
      + ",\"outcome\":" + outcome_json + "}";
   return Py000ResponsePrefix(request.request_id, request.op, true)
      + ",\"data\":" + data + "}";
}
string Py000HandleRequest(const string source)
{
   Py000Request request;
   string parse_error;
   if(!Py000JsonParseRequest(source, request, parse_error))
   {
      string code = "MALFORMED";
      string message = parse_error;
      if(parse_error == "UNKNOWN_OP")
      {
         code = "UNKNOWN_OP";
         message = "unsupported operation";
      }
      else if(StringSubstr(parse_error, 0, 8) == "SCHEMA: ")
      {
         code = "SCHEMA_MISMATCH";
         message = StringSubstr(parse_error, 8);
      }
      string request_id = Py000JsonVisibleIdentifier(request.request_id, 64)
         ? request.request_id : "invalid";
      string op = Py000JsonVisibleIdentifier(request.op, 64) ? request.op : "unknown";
      return Py000ErrorResponse(request_id, op, code, message);
   }
   string response;
   if(request.op == "hello")
      response = Py000HelloResponse(request);
   else
   {
      if(!Py000BindingMatches(request.binding))
         return Py000ErrorResponse(
            request.request_id,
            request.op,
            "BINDING_MISMATCH",
            "request binding does not match current EA"
         );
      if(request.op == "get_snapshot")
         response = Py000SnapshotResponse(request);
      else if(request.op == "submit_market_delta" || request.op == "close_position")
         response = Py000SubmitResponse(request);
      else
         response = Py000EventsResponse(request);
   }
   uchar encoded[];
   int byte_count = StringToCharArray(response, encoded, 0, WHOLE_ARRAY, CP_UTF8);
   if(byte_count <= 0 || byte_count - 1 > PY000_PROTOCOL_MAX_WIRE)
      return Py000ErrorResponse(
         request.request_id,
         request.op,
         "SCHEMA_MISMATCH",
         "response exceeds 64 KiB wire limit"
      );
   return response;
}
string Py000TickJson(const MqlTick &tick, const bool market_open_hint)
{
   string bid, ask;
   if(tick.bid <= 0.0 || tick.ask <= 0.0 || tick.bid > tick.ask
      || !Py000DecimalFromDouble(tick.bid, _Digits, bid)
      || !Py000DecimalFromDouble(tick.ask, _Digits, ask))
      return "";
   return "{\"ask\":" + Py000Quoted(ask)
      + ",\"bid\":" + Py000Quoted(bid)
      + ",\"event_time_ms\":" + Py000Quoted(Py000UlongString((ulong)tick.time_msc))
      + ",\"event_time_source\":\"mt5_symbol_info_tick\""
      + ",\"identity\":" + Py000IdentityJson()
      + ",\"market_open_hint\":" + Py000Bool(market_open_hint)
      + ",\"message_type\":\"tick\",\"protocol\":\"py000.mt5\",\"version\":1}";
}
string Py000HeartbeatJson(
   const string event_time_ms,
   const string last_tick_time_ms,
   const bool market_open_hint
)
{
   string last_tick = last_tick_time_ms == "" ? "null" : Py000Quoted(last_tick_time_ms);
   return "{\"event_time_ms\":" + Py000Quoted(event_time_ms)
      + ",\"event_time_source\":\"mt5_time_gmt_second_precision\""
      + ",\"identity\":" + Py000IdentityJson()
      + ",\"last_tick_time_ms\":" + last_tick
      + ",\"market_open_hint\":" + Py000Bool(market_open_hint)
      + ",\"message_type\":\"heartbeat\",\"protocol\":\"py000.mt5\",\"version\":1}";
}
#endif
