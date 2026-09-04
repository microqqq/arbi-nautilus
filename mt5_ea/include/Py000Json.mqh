#ifndef PY000_JSON_MQH
#define PY000_JSON_MQH
#define PY000_JSON_MAX_TEXT 256
#define PY000_JSON_MAX_IDENTIFIER 128
struct Py000Binding
{
   string account_id;
   string symbol;
   string ea_build_id;
   string stream_id;
   string boot_id;
};
struct Py000Request
{
   string protocol;
   int version;
   string request_id;
   string op;
   Py000Binding binding;
   string client_request_id;
   string side;
   string quantity_lots;
   string position_ticket;
   string position_identifier;
   string after_cursor;
   int limit;
};
struct Py000JsonCursor
{
   string source;
   int offset;
   int length;
   string error;
};
bool Py000JsonPreflight(const string source, string &error)
{
   bool in_string = false;
   bool escaped = false;
   int depth = 0;
   int digit_run = 0;
   for(int index = 0; index < StringLen(source); index++)
   {
      ushort code = (ushort)StringGetCharacter(source, index);
      if(in_string)
      {
         if(escaped) escaped = false;
         else if(code == '\\') escaped = true;
         else if(code == '"') in_string = false;
         continue;
      }
      if(code == '"')
      {
         in_string = true;
         digit_run = 0;
      }
      else if(code == '{' || code == '[')
      {
         depth++;
         digit_run = 0;
         if(depth > 32)
         {
            error = "JSON nesting exceeds v1 limit";
            return false;
         }
      }
      else if(code == '}' || code == ']')
      {
         depth--;
         digit_run = 0;
         if(depth < 0)
         {
            error = "unbalanced JSON container";
            return false;
         }
      }
      else if(code >= '0' && code <= '9')
      {
         digit_run++;
         if(digit_run > 20)
         {
            error = "JSON integer exceeds digit limit";
            return false;
         }
      }
      else
         digit_run = 0;
   }
   return true;
}
bool Py000JsonIsWhitespace(const ushort code)
{
   return code == 0x20 || code == 0x09 || code == 0x0a || code == 0x0d;
}
void Py000JsonSkipWhitespace(Py000JsonCursor &cursor)
{
   while(cursor.offset < cursor.length
      && Py000JsonIsWhitespace((ushort)StringGetCharacter(cursor.source, cursor.offset)))
      cursor.offset++;
}
bool Py000JsonConsume(Py000JsonCursor &cursor, const ushort expected)
{
   Py000JsonSkipWhitespace(cursor);
   if(cursor.offset >= cursor.length
      || (ushort)StringGetCharacter(cursor.source, cursor.offset) != expected)
   {
      cursor.error = "unexpected JSON token";
      return false;
   }
   cursor.offset++;
   return true;
}
int Py000JsonHexValue(const ushort code)
{
   if(code >= '0' && code <= '9')
      return (int)(code - '0');
   if(code >= 'a' && code <= 'f')
      return (int)(code - 'a') + 10;
   if(code >= 'A' && code <= 'F')
      return (int)(code - 'A') + 10;
   return -1;
}
bool Py000JsonParseString(Py000JsonCursor &cursor, string &value)
{
   value = "";
   Py000JsonSkipWhitespace(cursor);
   if(cursor.offset >= cursor.length
      || (ushort)StringGetCharacter(cursor.source, cursor.offset) != '"')
   {
      cursor.error = "JSON string required";
      return false;
   }
   cursor.offset++;
   while(cursor.offset < cursor.length)
   {
      ushort code = (ushort)StringGetCharacter(cursor.source, cursor.offset++);
      if(code == '"')
         return true;
      if(code < 0x20 || code == 0)
      {
         cursor.error = "control character in JSON string";
         return false;
      }
      if(code != '\\')
      {
         value += ShortToString(code);
         if(StringLen(value) > PY000_JSON_MAX_TEXT)
         {
            cursor.error = "JSON string too long";
            return false;
         }
         continue;
      }
      if(cursor.offset >= cursor.length)
      {
         cursor.error = "unterminated JSON escape";
         return false;
      }
      ushort escaped = (ushort)StringGetCharacter(cursor.source, cursor.offset++);
      if(escaped == '"' || escaped == '\\' || escaped == '/')
      {
         value += ShortToString(escaped);
         if(StringLen(value) > PY000_JSON_MAX_TEXT)
         {
            cursor.error = "JSON string too long";
            return false;
         }
         continue;
      }
      if(escaped != 'u' || cursor.offset + 4 > cursor.length)
      {
         cursor.error = "unsupported or control JSON escape";
         return false;
      }
      int scalar = 0;
      for(int digit = 0; digit < 4; digit++)
      {
         int nibble = Py000JsonHexValue(
            (ushort)StringGetCharacter(cursor.source, cursor.offset++)
         );
         if(nibble < 0)
         {
            cursor.error = "malformed JSON unicode escape";
            return false;
         }
         scalar = scalar * 16 + nibble;
      }
      if(scalar < 0x21 || scalar > 0x7e)
      {
         cursor.error = "request text must be visible ASCII";
         return false;
      }
      value += ShortToString((ushort)scalar);
      if(StringLen(value) > PY000_JSON_MAX_TEXT)
      {
         cursor.error = "JSON string too long";
         return false;
      }
   }
   cursor.error = "unterminated JSON string";
   return false;
}
bool Py000JsonParseUnsignedInt(Py000JsonCursor &cursor, int &value)
{
   Py000JsonSkipWhitespace(cursor);
   int start = cursor.offset;
   if(start >= cursor.length)
   {
      cursor.error = "JSON integer required";
      return false;
   }
   ushort first = (ushort)StringGetCharacter(cursor.source, cursor.offset);
   if(first < '0' || first > '9')
   {
      cursor.error = "unsigned JSON integer required";
      return false;
   }
   if(first == '0')
   {
      cursor.offset++;
      if(cursor.offset < cursor.length)
      {
         ushort next = (ushort)StringGetCharacter(cursor.source, cursor.offset);
         if(next >= '0' && next <= '9')
         {
            cursor.error = "leading zero in JSON integer";
            return false;
         }
      }
      value = 0;
      return true;
   }
   int digit_count = 0;
   int scan = cursor.offset;
   while(scan < cursor.length)
   {
      ushort code = (ushort)StringGetCharacter(cursor.source, scan);
      if(code < '0' || code > '9') break;
      digit_count++;
      scan++;
   }
   if(digit_count > 10)
   {
      cursor.error = "JSON integer exceeds int32";
      return false;
   }
   long parsed = 0;
   while(cursor.offset < cursor.length)
   {
      ushort code = (ushort)StringGetCharacter(cursor.source, cursor.offset);
      if(code < '0' || code > '9')
         break;
      parsed = parsed * 10 + (int)(code - '0');
      if(parsed > 2147483647)
      {
         cursor.error = "JSON integer exceeds int32";
         return false;
      }
      cursor.offset++;
   }
   if(cursor.offset == start)
   {
      cursor.error = "JSON integer required";
      return false;
   }
   if(cursor.offset < cursor.length)
   {
      ushort suffix = (ushort)StringGetCharacter(cursor.source, cursor.offset);
      if(suffix == '.' || suffix == 'e' || suffix == 'E')
      {
         cursor.error = "JSON integer cannot be float or exponent";
         return false;
      }
   }
   value = (int)parsed;
   return true;
}
bool Py000JsonVisibleIdentifier(const string value, const int maximum)
{
   int length = StringLen(value);
   if(length < 1 || length > maximum)
      return false;
   for(int index = 0; index < length; index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(code < 0x21 || code > 0x7e)
         return false;
   }
   return true;
}
bool Py000JsonSafeToken(const string value)
{
   if(!Py000JsonVisibleIdentifier(value, PY000_JSON_MAX_IDENTIFIER))
      return false;
   for(int index = 0; index < StringLen(value); index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      bool accepted = (code >= 'a' && code <= 'z')
         || (code >= 'A' && code <= 'Z')
         || (code >= '0' && code <= '9') || code == '-' || code == '_';
      if(!accepted)
         return false;
   }
   return true;
}
bool Py000JsonCanonicalUint64(const string value, const bool positive)
{
   int length = StringLen(value);
   if(length < 1 || length > 20)
      return false;
   if(length > 1 && StringGetCharacter(value, 0) == '0')
      return false;
   for(int index = 0; index < length; index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(code < '0' || code > '9')
         return false;
   }
   if(positive && value == "0")
      return false;
   if(length == 20 && StringCompare(value, "18446744073709551615") > 0)
      return false;
   return true;
}
bool Py000JsonPositiveDecimal(const string value)
{
   int length = StringLen(value);
   if(length < 1 || length > 64)
      return false;
   bool decimal_seen = false;
   bool nonzero_seen = false;
   int decimal_digits = 0;
   if(length > 1 && StringGetCharacter(value, 0) == '0'
      && StringGetCharacter(value, 1) != '.')
      return false;
   for(int index = 0; index < length; index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(code == '.')
      {
         if(decimal_seen || index == 0 || index == length - 1)
            return false;
         decimal_seen = true;
         continue;
      }
      if(code < '0' || code > '9')
         return false;
      if(code != '0')
         nonzero_seen = true;
      if(decimal_seen)
         decimal_digits++;
   }
   return nonzero_seen && (!decimal_seen || decimal_digits > 0);
}
bool Py000JsonSignedDecimal(const string value)
{
   int length = StringLen(value);
   if(length < 1 || length > 64)
      return false;
   int offset = StringGetCharacter(value, 0) == '-' ? 1 : 0;
   if(offset == length)
      return false;
   bool decimal_seen = false;
   int decimal_digits = 0;
   if(length - offset > 1 && StringGetCharacter(value, offset) == '0'
      && StringGetCharacter(value, offset + 1) != '.')
      return false;
   for(int index = offset; index < length; index++)
   {
      ushort code = (ushort)StringGetCharacter(value, index);
      if(code == '.')
      {
         if(decimal_seen || index == offset || index == length - 1)
            return false;
         decimal_seen = true;
         continue;
      }
      if(code < '0' || code > '9')
         return false;
      if(decimal_seen)
         decimal_digits++;
   }
   return !decimal_seen || decimal_digits > 0;
}
bool Py000JsonMarkSeen(string &seen[], const string key)
{
   int count = ArraySize(seen);
   for(int index = 0; index < count; index++)
      if(seen[index] == key)
         return false;
   ArrayResize(seen, count + 1);
   seen[count] = key;
   return true;
}
bool Py000JsonWasSeen(string &seen[], const string key)
{
   for(int index = 0; index < ArraySize(seen); index++)
      if(seen[index] == key)
         return true;
   return false;
}
bool Py000JsonParseBinding(Py000JsonCursor &cursor, Py000Binding &binding)
{
   if(!Py000JsonConsume(cursor, '{'))
      return false;
   string seen[];
   bool first = true;
   while(true)
   {
      Py000JsonSkipWhitespace(cursor);
      if(cursor.offset < cursor.length
         && StringGetCharacter(cursor.source, cursor.offset) == '}')
      {
         cursor.offset++;
         break;
      }
      if(!first && !Py000JsonConsume(cursor, ','))
         return false;
      first = false;
      string key;
      string value;
      if(!Py000JsonParseString(cursor, key) || !Py000JsonConsume(cursor, ':'))
         return false;
      Py000JsonSkipWhitespace(cursor);
      bool string_value = cursor.offset < cursor.length
         && StringGetCharacter(cursor.source, cursor.offset) == '"';
      if(!Py000JsonParseString(cursor, value))
      {
         if(!string_value)
            cursor.error = "SCHEMA: binding member must be string";
         return false;
      }
      if(!Py000JsonMarkSeen(seen, key))
      {
         cursor.error = "duplicate binding member";
         return false;
      }
      if(key == "account_id") binding.account_id = value;
      else if(key == "symbol") binding.symbol = value;
      else if(key == "ea_build_id") binding.ea_build_id = value;
      else if(key == "stream_id") binding.stream_id = value;
      else if(key == "boot_id") binding.boot_id = value;
      else
      {
         cursor.error = "SCHEMA: unknown binding member";
         return false;
      }
   }
   if(ArraySize(seen) != 5
      || !Py000JsonVisibleIdentifier(binding.account_id, PY000_JSON_MAX_IDENTIFIER)
      || !Py000JsonVisibleIdentifier(binding.symbol, PY000_JSON_MAX_IDENTIFIER)
      || !Py000JsonSafeToken(binding.ea_build_id)
      || !Py000JsonSafeToken(binding.stream_id)
      || !Py000JsonSafeToken(binding.boot_id))
   {
      cursor.error = "SCHEMA: binding is incomplete or invalid";
      return false;
   }
   return true;
}
bool Py000JsonParseRequest(const string source, Py000Request &request, string &error)
{
   ZeroMemory(request);
   request.request_id = "invalid";
   request.op = "unknown";
   Py000JsonCursor cursor;
   cursor.source = source;
   cursor.offset = 0;
   cursor.length = StringLen(source);
   cursor.error = "";
   if(!Py000JsonPreflight(source, error))
      return false;
   if(!Py000JsonConsume(cursor, '{'))
   {
      error = cursor.error;
      return false;
   }
   string seen[];
   bool first = true;
   while(true)
   {
      Py000JsonSkipWhitespace(cursor);
      if(cursor.offset < cursor.length
         && StringGetCharacter(cursor.source, cursor.offset) == '}')
      {
         cursor.offset++;
         break;
      }
      if(!first && !Py000JsonConsume(cursor, ','))
      {
         error = cursor.error;
         return false;
      }
      first = false;
      string key;
      if(!Py000JsonParseString(cursor, key) || !Py000JsonConsume(cursor, ':'))
      {
         error = cursor.error;
         return false;
      }
      if(!Py000JsonMarkSeen(seen, key))
      {
         error = "duplicate request member";
         return false;
      }
      if(key == "version" || key == "limit")
      {
         int number = 0;
         if(!Py000JsonParseUnsignedInt(cursor, number))
         {
            error = "SCHEMA: exact JSON integer required for " + key;
            return false;
         }
         if(key == "version") request.version = number;
         else request.limit = number;
         continue;
      }
      if(key == "binding")
      {
         Py000JsonSkipWhitespace(cursor);
         if(cursor.offset >= cursor.length
            || StringGetCharacter(cursor.source, cursor.offset) != '{')
         {
            error = "SCHEMA: binding must be an object";
            return false;
         }
         if(!Py000JsonParseBinding(cursor, request.binding))
         {
            error = cursor.error;
            return false;
         }
         continue;
      }
      string value;
      Py000JsonSkipWhitespace(cursor);
      bool string_value = cursor.offset < cursor.length
         && StringGetCharacter(cursor.source, cursor.offset) == '"';
      if(!Py000JsonParseString(cursor, value))
      {
         error = string_value ? cursor.error : "SCHEMA: request field must be string";
         return false;
      }
      if(key == "protocol") request.protocol = value;
      else if(key == "request_id") request.request_id = value;
      else if(key == "op") request.op = value;
      else if(key == "client_request_id") request.client_request_id = value;
      else if(key == "side") request.side = value;
      else if(key == "quantity_lots") request.quantity_lots = value;
      else if(key == "position_ticket") request.position_ticket = value;
      else if(key == "position_identifier") request.position_identifier = value;
      else if(key == "after_cursor") request.after_cursor = value;
      else
      {
         error = "SCHEMA: unknown request member";
         return false;
      }
   }
   Py000JsonSkipWhitespace(cursor);
   if(cursor.offset != cursor.length)
   {
      error = "trailing JSON content";
      return false;
   }
   if(request.protocol != "py000.mt5" || request.version != 1
      || !Py000JsonVisibleIdentifier(request.request_id, 64)
      || !Py000JsonVisibleIdentifier(request.op, 64))
   {
      error = "SCHEMA: request envelope mismatch";
      return false;
   }
   int expected = 0;
   if(request.op == "hello") expected = 4;
   else if(request.op == "get_snapshot") expected = 5;
   else if(request.op == "submit_market_delta") expected = 8;
   else if(request.op == "close_position") expected = 10;
   else if(request.op == "get_execution_events") expected = 7;
   else
   {
      error = "UNKNOWN_OP";
      return false;
   }
   if(ArraySize(seen) != expected)
   {
      error = "SCHEMA: request fields differ from closed schema";
      return false;
   }
   if(!Py000JsonWasSeen(seen, "protocol") || !Py000JsonWasSeen(seen, "version")
      || !Py000JsonWasSeen(seen, "request_id") || !Py000JsonWasSeen(seen, "op")
      || (request.op == "hello" && Py000JsonWasSeen(seen, "binding"))
      || (request.op != "hello" && !Py000JsonWasSeen(seen, "binding"))
      || (request.op == "submit_market_delta"
         && (!Py000JsonWasSeen(seen, "client_request_id")
            || !Py000JsonWasSeen(seen, "side")
            || !Py000JsonWasSeen(seen, "quantity_lots")))
      || (request.op == "close_position"
         && (!Py000JsonWasSeen(seen, "client_request_id")
            || !Py000JsonWasSeen(seen, "side")
            || !Py000JsonWasSeen(seen, "quantity_lots")
            || !Py000JsonWasSeen(seen, "position_ticket")
            || !Py000JsonWasSeen(seen, "position_identifier")))
      || (request.op == "get_execution_events"
         && (!Py000JsonWasSeen(seen, "after_cursor")
            || !Py000JsonWasSeen(seen, "limit"))))
   {
      error = "SCHEMA: required request member mismatch";
      return false;
   }
   if(request.op == "submit_market_delta"
      && (!Py000JsonSafeToken(request.client_request_id)
         || (request.side != "buy" && request.side != "sell")
         || !Py000JsonPositiveDecimal(request.quantity_lots)))
   {
      error = "SCHEMA: submit_market_delta fields invalid";
      return false;
   }
   if(request.op == "close_position"
      && (!Py000JsonSafeToken(request.client_request_id)
         || (request.side != "buy" && request.side != "sell")
         || !Py000JsonPositiveDecimal(request.quantity_lots)
         || !Py000JsonCanonicalUint64(request.position_ticket, true)
         || !Py000JsonCanonicalUint64(request.position_identifier, true)))
   {
      error = "SCHEMA: close_position fields invalid";
      return false;
   }
   if(request.op == "get_execution_events"
      && (!Py000JsonCanonicalUint64(request.after_cursor, false)
         || request.limit < 1 || request.limit > 500))
   {
      error = "SCHEMA: get_execution_events fields invalid";
      return false;
   }
   return true;
}
#endif
