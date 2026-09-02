#ifndef PY000_JOURNAL_MQH
#define PY000_JOURNAL_MQH
#define PY000_JOURNAL_PREFIX "PY000_MT5_V1_"
struct Py000JournalEvent
{
   string event_seq;
   string event_time_ms;
   string stream_id;
   string boot_id;
   string event_type;
   string ea_build_id;
   bool execution_enabled;
   string client_request_id;
   string side;
   string quantity_lots;
   string reason;
   string broker_retcode;
   string filled_quantity_lots;
   string fill_price;
   string venue_order_id;
   string venue_deal_id;
   string venue_position_id;
   string commission;
};
string g_py000_journal_namespace = "";
string g_py000_journal_stream_id = "";
string g_py000_journal_first_cursor = "0";
Py000JournalEvent g_py000_journal_events[];
bool g_py000_journal_ready = false;
bool g_py000_execution_recovery_ready = true;
string Py000JournalHex64(ulong value)
{
   string alphabet = "0123456789abcdef";
   string result = "";
   for(int shift = 60; shift >= 0; shift -= 4)
      result += StringSubstr(alphabet, (int)((value >> shift) & 0x0f), 1);
   return result;
}
string Py000JournalChecksum(const string value)
{
   uchar bytes[];
   int count = StringToCharArray(value, bytes, 0, WHOLE_ARRAY, CP_UTF8);
   ulong hash = 0xcbf29ce484222325;
   for(int index = 0; index < count - 1; index++)
   {
      hash ^= (ulong)bytes[index];
      hash *= 0x00000100000001b3;
   }
   return Py000JournalHex64(hash);
}
int Py000JournalCompareUint(const string left, const string right)
{
   int left_length = StringLen(left);
   int right_length = StringLen(right);
   if(left_length < right_length) return -1;
   if(left_length > right_length) return 1;
   return StringCompare(left, right);
}
string Py000JournalIncrement(const string value)
{
   string result = value;
   int carry = 1;
   for(int index = StringLen(result) - 1; index >= 0 && carry == 1; index--)
   {
      int digit = (int)StringGetCharacter(result, index) - (int)'0' + carry;
      carry = digit / 10;
      digit %= 10;
      StringSetCharacter(result, index, (ushort)('0' + digit));
   }
   if(carry == 1)
      result = "1" + result;
   if(!Py000JsonCanonicalUint64(result, true))
      return "";
   return result;
}
string Py000JournalIdentityFile()
{
   return g_py000_journal_namespace + ".identity";
}
string Py000JournalEventsFile()
{
   return g_py000_journal_namespace + ".events";
}
bool Py000JournalWriteLine(
   const string file_name,
   const string contents,
   const bool append
)
{
   int handle = FileOpen(
      file_name,
      FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON,
      0,
      CP_UTF8
   );
   if(handle == INVALID_HANDLE)
      return false;
   if(append)
      FileSeek(handle, 0, SEEK_END);
   bool written = FileWriteString(handle, contents + "\n") > 0;
   FileFlush(handle);
   FileClose(handle);
   return written;
}
bool Py000JournalVerifiedParts(const string line, const int expected, string &parts[])
{
   ushort separator = (ushort)StringGetCharacter("|", 0);
   if(StringSplit(line, separator, parts) != expected)
      return false;
   string body = parts[0];
   for(int index = 1; index < expected - 1; index++)
      body += "|" + parts[index];
   return parts[expected - 1] == Py000JournalChecksum(body);
}
string Py000JournalNewId(const string prefix)
{
   string now = IntegerToString((long)TimeGMT() * 1000);
   string ticks = IntegerToString((long)GetTickCount64());
   return prefix + "-" + now + "-" + ticks;
}
bool Py000JournalIsTerminal(const string event_type)
{
   return event_type == "order_rejected"
      || event_type == "order_filled"
      || event_type == "order_unknown";
}
bool Py000JournalIsOrderEvent(const string event_type)
{
   return event_type == "submission_reserved" || Py000JournalIsTerminal(event_type);
}
int Py000JournalReservedIndex(const string client_request_id)
{
   for(int index = 0; index < ArraySize(g_py000_journal_events); index++)
      if(g_py000_journal_events[index].event_type == "submission_reserved"
         && g_py000_journal_events[index].client_request_id == client_request_id)
         return index;
   return -1;
}
int Py000JournalOutcomeIndex(const string client_request_id)
{
   for(int index = 0; index < ArraySize(g_py000_journal_events); index++)
      if(Py000JournalIsTerminal(g_py000_journal_events[index].event_type)
         && g_py000_journal_events[index].client_request_id == client_request_id)
         return index;
   return -1;
}
bool Py000JournalValidateExecutionHistory()
{
   g_py000_execution_recovery_ready = true;
   for(int index = 0; index < ArraySize(g_py000_journal_events); index++)
   {
      Py000JournalEvent current = g_py000_journal_events[index];
      if(current.event_type == "stream_started")
      {
         for(int prior = 0; prior < index; prior++)
            if(g_py000_journal_events[prior].event_type == "stream_started"
               && g_py000_journal_events[prior].boot_id == current.boot_id)
               return false;
         continue;
      }
      if(current.event_type == "submission_reserved")
      {
         for(int prior = 0; prior < index; prior++)
            if(Py000JournalIsOrderEvent(g_py000_journal_events[prior].event_type)
               && g_py000_journal_events[prior].client_request_id
                  == current.client_request_id)
               return false;
         continue;
      }
      int reserved = -1;
      for(int prior = 0; prior < index; prior++)
      {
         Py000JournalEvent candidate = g_py000_journal_events[prior];
         if(candidate.client_request_id != current.client_request_id) continue;
         if(candidate.event_type == "submission_reserved") reserved = prior;
         else if(Py000JournalIsTerminal(candidate.event_type)) return false;
      }
      if(reserved < 0
         || g_py000_journal_events[reserved].side != current.side
         || g_py000_journal_events[reserved].quantity_lots != current.quantity_lots)
         return false;
   }
   for(int index = 0; index < ArraySize(g_py000_journal_events); index++)
   {
      Py000JournalEvent current = g_py000_journal_events[index];
      if(current.event_type != "submission_reserved") continue;
      int outcome = Py000JournalOutcomeIndex(current.client_request_id);
      if(outcome < 0 || g_py000_journal_events[outcome].event_type == "order_unknown")
         g_py000_execution_recovery_ready = false;
   }
   return true;
}
bool Py000JournalCreateNew()
{
   g_py000_journal_stream_id = Py000JournalNewId("stream");
   if(!Py000JsonSafeToken(g_py000_journal_stream_id))
      return false;
   string identity_body = "I|1|" + g_py000_journal_namespace
      + "|" + g_py000_journal_stream_id;
   string events_body = "H|1|" + g_py000_journal_namespace
      + "|" + g_py000_journal_stream_id + "|0";
   if(!Py000JournalWriteLine(
         Py000JournalIdentityFile(),
         identity_body + "|" + Py000JournalChecksum(identity_body),
         false
      ))
      return false;
   if(!Py000JournalWriteLine(
         Py000JournalEventsFile(),
         events_body + "|" + Py000JournalChecksum(events_body),
         false
      ))
      return false;
   ArrayResize(g_py000_journal_events, 0);
   g_py000_journal_ready = true;
   g_py000_execution_recovery_ready = true;
   return true;
}
bool Py000JournalLoadIdentity()
{
   int handle = FileOpen(
      Py000JournalIdentityFile(),
      FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON,
      0,
      CP_UTF8
   );
   if(handle == INVALID_HANDLE)
      return false;
   string first_line = FileReadString(handle);
   bool extra = !FileIsEnding(handle);
   FileClose(handle);
   string parts[];
   if(extra || !Py000JournalVerifiedParts(first_line, 5, parts))
      return false;
   if(parts[0] != "I" || parts[1] != "1"
      || parts[2] != g_py000_journal_namespace
      || !Py000JsonSafeToken(parts[3]))
      return false;
   g_py000_journal_stream_id = parts[3];
   return true;
}
bool Py000JournalParseEvent(
   const string line,
   const string expected_sequence,
   Py000JournalEvent &event
)
{
   ZeroMemory(event);
   string probe[];
   ushort separator = (ushort)StringGetCharacter("|", 0);
   int count = StringSplit(line, separator, probe);
   if(count < 7)
      return false;
   string parts[];
   if(!Py000JournalVerifiedParts(line, count, parts)
      || parts[0] != "E" || parts[1] != expected_sequence
      || !Py000JsonCanonicalUint64(parts[1], true)
      || !Py000JsonCanonicalUint64(parts[2], true)
      || parts[3] != g_py000_journal_stream_id
      || !Py000JsonSafeToken(parts[4]))
      return false;
   event.event_seq = parts[1];
   event.event_time_ms = parts[2];
   event.stream_id = parts[3];
   event.boot_id = parts[4];
   event.event_type = parts[5];
   if(event.event_type == "stream_started")
   {
      if(count != 9 || !Py000JsonSafeToken(parts[6])
         || (parts[7] != "readonly-v1" && parts[7] != "demo-v1"))
         return false;
      event.ea_build_id = parts[6];
      event.execution_enabled = parts[7] == "demo-v1";
      return true;
   }
   if(event.event_type == "submission_reserved")
   {
      if(count != 10 || !Py000JsonSafeToken(parts[6])
         || (parts[7] != "buy" && parts[7] != "sell")
         || !Py000JsonPositiveDecimal(parts[8]))
         return false;
      event.client_request_id = parts[6];
      event.side = parts[7];
      event.quantity_lots = parts[8];
      return true;
   }
   if(event.event_type == "order_rejected" || event.event_type == "order_unknown")
   {
      if(count != 12 || !Py000JsonSafeToken(parts[6])
         || (parts[7] != "buy" && parts[7] != "sell")
         || !Py000JsonPositiveDecimal(parts[8])
         || !Py000JsonSafeToken(parts[9])
         || !Py000JsonCanonicalUint64(parts[10], false))
         return false;
      event.client_request_id = parts[6];
      event.side = parts[7];
      event.quantity_lots = parts[8];
      event.reason = parts[9];
      event.broker_retcode = parts[10];
      return true;
   }
   if(event.event_type == "order_filled")
   {
      if(count != 17 || !Py000JsonSafeToken(parts[6])
         || (parts[7] != "buy" && parts[7] != "sell")
         || !Py000JsonPositiveDecimal(parts[8])
         || !Py000JsonPositiveDecimal(parts[9])
         || !Py000JsonPositiveDecimal(parts[10])
         || !Py000JsonCanonicalUint64(parts[11], true)
         || !Py000JsonCanonicalUint64(parts[12], true)
         || !Py000JsonCanonicalUint64(parts[13], true)
         || !Py000JsonSignedDecimal(parts[14])
         || !Py000JsonCanonicalUint64(parts[15], false))
         return false;
      event.client_request_id = parts[6];
      event.side = parts[7];
      event.quantity_lots = parts[8];
      event.filled_quantity_lots = parts[9];
      event.fill_price = parts[10];
      event.venue_order_id = parts[11];
      event.venue_deal_id = parts[12];
      event.venue_position_id = parts[13];
      event.commission = parts[14];
      event.broker_retcode = parts[15];
      return true;
   }
   return false;
}
bool Py000JournalLoadEvents()
{
   ArrayResize(g_py000_journal_events, 0);
   int handle = FileOpen(
      Py000JournalEventsFile(),
      FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON,
      0,
      CP_UTF8
   );
   if(handle == INVALID_HANDLE)
      return false;
   if(FileIsEnding(handle))
   {
      FileClose(handle);
      return false;
   }
   string header = FileReadString(handle);
   string header_parts[];
   if(!Py000JournalVerifiedParts(header, 6, header_parts)
      || header_parts[0] != "H" || header_parts[1] != "1"
      || header_parts[2] != g_py000_journal_namespace
      || header_parts[3] != g_py000_journal_stream_id
      || !Py000JsonCanonicalUint64(header_parts[4], false))
   {
      FileClose(handle);
      return false;
   }
   g_py000_journal_first_cursor = header_parts[4];
   string expected = Py000JournalIncrement(g_py000_journal_first_cursor);
   while(!FileIsEnding(handle))
   {
      string line = FileReadString(handle);
      Py000JournalEvent event;
      if(line == "" || !Py000JournalParseEvent(line, expected, event))
      {
         FileClose(handle);
         return false;
      }
      int count = ArraySize(g_py000_journal_events);
      ArrayResize(g_py000_journal_events, count + 1);
      g_py000_journal_events[count] = event;
      expected = Py000JournalIncrement(expected);
      if(expected == "" && !FileIsEnding(handle))
      {
         FileClose(handle);
         return false;
      }
   }
   FileClose(handle);
   return Py000JournalValidateExecutionHistory();
}
bool Py000JournalInit(const string journal_namespace)
{
   g_py000_journal_namespace = journal_namespace;
   g_py000_journal_stream_id = "";
   g_py000_journal_first_cursor = "0";
   g_py000_journal_ready = false;
   g_py000_execution_recovery_ready = true;
   ArrayResize(g_py000_journal_events, 0);
   if(StringSubstr(journal_namespace, 0, StringLen(PY000_JOURNAL_PREFIX))
      != PY000_JOURNAL_PREFIX || !Py000JsonSafeToken(journal_namespace))
      return false;
   bool identity_exists = FileIsExist(Py000JournalIdentityFile(), FILE_COMMON);
   bool events_exists = FileIsExist(Py000JournalEventsFile(), FILE_COMMON);
   if(!identity_exists && !events_exists)
      return Py000JournalCreateNew();
   if(!identity_exists || !Py000JournalLoadIdentity())
      return false;
   if(!events_exists || !Py000JournalLoadEvents())
   {
      g_py000_journal_ready = false;
      return true;
   }
   g_py000_journal_ready = true;
   return true;
}
string Py000JournalLastCursor()
{
   int count = ArraySize(g_py000_journal_events);
   if(count == 0)
      return g_py000_journal_first_cursor;
   return g_py000_journal_events[count - 1].event_seq;
}
bool Py000JournalAppendStreamStarted(
   const string boot_id,
   const string event_time_ms,
   const string ea_build_id,
   const bool execution_enabled
)
{
   if(!g_py000_journal_ready || !Py000JsonSafeToken(boot_id)
      || !Py000JsonCanonicalUint64(event_time_ms, true)
      || !Py000JsonSafeToken(ea_build_id))
      return false;
   for(int index = 0; index < ArraySize(g_py000_journal_events); index++)
      if(g_py000_journal_events[index].event_type == "stream_started"
         && g_py000_journal_events[index].boot_id == boot_id)
         return false;
   string sequence = Py000JournalIncrement(Py000JournalLastCursor());
   if(sequence == "")
      return false;
   string mode = execution_enabled ? "demo-v1" : "readonly-v1";
   string body = "E|" + sequence + "|" + event_time_ms + "|"
      + g_py000_journal_stream_id + "|" + boot_id
      + "|stream_started|" + ea_build_id + "|" + mode;
   if(!Py000JournalWriteLine(
         Py000JournalEventsFile(),
         body + "|" + Py000JournalChecksum(body),
         true
      ))
      return false;
   int count = ArraySize(g_py000_journal_events);
   ArrayResize(g_py000_journal_events, count + 1);
   g_py000_journal_events[count].event_seq = sequence;
   g_py000_journal_events[count].event_time_ms = event_time_ms;
   g_py000_journal_events[count].stream_id = g_py000_journal_stream_id;
   g_py000_journal_events[count].boot_id = boot_id;
   g_py000_journal_events[count].event_type = "stream_started";
   g_py000_journal_events[count].ea_build_id = ea_build_id;
   g_py000_journal_events[count].execution_enabled = execution_enabled;
   return true;
}
bool Py000JournalStoreEvent(const Py000JournalEvent &event)
{
   int count = ArraySize(g_py000_journal_events);
   if(ArrayResize(g_py000_journal_events, count + 1) != count + 1)
   {
      g_py000_journal_ready = false;
      g_py000_execution_recovery_ready = false;
      return false;
   }
   g_py000_journal_events[count] = event;
   return true;
}
bool Py000JournalAppendReserved(
   const string boot_id,
   const string event_time_ms,
   const string client_request_id,
   const string side,
   const string quantity_lots,
   Py000JournalEvent &event
)
{
   if(!g_py000_journal_ready || !g_py000_execution_recovery_ready
      || !Py000JsonSafeToken(boot_id)
      || !Py000JsonCanonicalUint64(event_time_ms, true)
      || !Py000JsonSafeToken(client_request_id)
      || (side != "buy" && side != "sell")
      || !Py000JsonPositiveDecimal(quantity_lots)
      || Py000JournalReservedIndex(client_request_id) >= 0
      || Py000JournalOutcomeIndex(client_request_id) >= 0)
      return false;
   string sequence = Py000JournalIncrement(Py000JournalLastCursor());
   if(sequence == "")
      return false;
   string body = "E|" + sequence + "|" + event_time_ms + "|"
      + g_py000_journal_stream_id + "|" + boot_id
      + "|submission_reserved|" + client_request_id + "|" + side
      + "|" + quantity_lots;
   if(!Py000JournalWriteLine(
         Py000JournalEventsFile(),
         body + "|" + Py000JournalChecksum(body),
         true
      ))
      return false;
   g_py000_execution_recovery_ready = false;
   ZeroMemory(event);
   event.event_seq = sequence;
   event.event_time_ms = event_time_ms;
   event.stream_id = g_py000_journal_stream_id;
   event.boot_id = boot_id;
   event.event_type = "submission_reserved";
   event.client_request_id = client_request_id;
   event.side = side;
   event.quantity_lots = quantity_lots;
   return Py000JournalStoreEvent(event);
}
bool Py000JournalPrepareTerminal(
   const string boot_id,
   const string event_time_ms,
   const string client_request_id,
   Py000JournalEvent &event
)
{
   if(!g_py000_journal_ready || !Py000JsonSafeToken(boot_id)
      || !Py000JsonCanonicalUint64(event_time_ms, true)
      || !Py000JsonSafeToken(client_request_id)
      || Py000JournalOutcomeIndex(client_request_id) >= 0)
      return false;
   int reserved = Py000JournalReservedIndex(client_request_id);
   if(reserved < 0)
      return false;
   ZeroMemory(event);
   event.event_seq = Py000JournalIncrement(Py000JournalLastCursor());
   if(event.event_seq == "")
      return false;
   event.event_time_ms = event_time_ms;
   event.stream_id = g_py000_journal_stream_id;
   event.boot_id = boot_id;
   event.client_request_id = client_request_id;
   event.side = g_py000_journal_events[reserved].side;
   event.quantity_lots = g_py000_journal_events[reserved].quantity_lots;
   return true;
}
bool Py000JournalAppendRejectedOrUnknown(
   const string boot_id,
   const string event_time_ms,
   const string event_type,
   const string client_request_id,
   const string reason,
   const string broker_retcode,
   Py000JournalEvent &event
)
{
   if((event_type != "order_rejected" && event_type != "order_unknown")
      || !Py000JsonSafeToken(reason)
      || !Py000JsonCanonicalUint64(broker_retcode, false)
      || !Py000JournalPrepareTerminal(
         boot_id, event_time_ms, client_request_id, event
      ))
      return false;
   event.event_type = event_type;
   event.reason = reason;
   event.broker_retcode = broker_retcode;
   string body = "E|" + event.event_seq + "|" + event.event_time_ms + "|"
      + event.stream_id + "|" + event.boot_id + "|" + event.event_type
      + "|" + event.client_request_id + "|" + event.side + "|"
      + event.quantity_lots + "|" + event.reason + "|"
      + event.broker_retcode;
   if(!Py000JournalWriteLine(
         Py000JournalEventsFile(),
         body + "|" + Py000JournalChecksum(body),
         true
      ))
   {
      g_py000_execution_recovery_ready = false;
      return false;
   }
   if(!Py000JournalStoreEvent(event)
      || !Py000JournalValidateExecutionHistory())
   {
      g_py000_journal_ready = false;
      g_py000_execution_recovery_ready = false;
      return false;
   }
   return true;
}
bool Py000JournalAppendFilled(
   const string boot_id,
   const string event_time_ms,
   const string client_request_id,
   const string filled_quantity_lots,
   const string fill_price,
   const string venue_order_id,
   const string venue_deal_id,
   const string venue_position_id,
   const string commission,
   const string broker_retcode,
   Py000JournalEvent &event
)
{
   if(!Py000JsonPositiveDecimal(filled_quantity_lots)
      || !Py000JsonPositiveDecimal(fill_price)
      || !Py000JsonCanonicalUint64(venue_order_id, true)
      || !Py000JsonCanonicalUint64(venue_deal_id, true)
      || !Py000JsonCanonicalUint64(venue_position_id, true)
      || !Py000JsonSignedDecimal(commission)
      || !Py000JsonCanonicalUint64(broker_retcode, false)
      || !Py000JournalPrepareTerminal(
         boot_id, event_time_ms, client_request_id, event
      ))
      return false;
   event.event_type = "order_filled";
   event.filled_quantity_lots = filled_quantity_lots;
   event.fill_price = fill_price;
   event.venue_order_id = venue_order_id;
   event.venue_deal_id = venue_deal_id;
   event.venue_position_id = venue_position_id;
   event.commission = commission;
   event.broker_retcode = broker_retcode;
   string body = "E|" + event.event_seq + "|" + event.event_time_ms + "|"
      + event.stream_id + "|" + event.boot_id + "|" + event.event_type
      + "|" + event.client_request_id + "|" + event.side + "|"
      + event.quantity_lots + "|" + event.filled_quantity_lots + "|"
      + event.fill_price + "|" + event.venue_order_id + "|"
      + event.venue_deal_id + "|" + event.venue_position_id + "|"
      + event.commission + "|" + event.broker_retcode;
   if(!Py000JournalWriteLine(
         Py000JournalEventsFile(),
         body + "|" + Py000JournalChecksum(body),
         true
      ))
   {
      g_py000_execution_recovery_ready = false;
      return false;
   }
   if(!Py000JournalStoreEvent(event)
      || !Py000JournalValidateExecutionHistory())
   {
      g_py000_journal_ready = false;
      g_py000_execution_recovery_ready = false;
      return false;
   }
   return true;
}
#endif
