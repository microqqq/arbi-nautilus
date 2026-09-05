#property strict
#property description "Independent temporary-file journal write probes; no EA startup or trading"

int g_write_checks = 0;
int g_write_failures = 0;
string g_write_prefix;
enum Py000WriteFault
{
   WRITE_OK, SEEK_FALSE, SEEK_ERROR, SHORT_WRITE, ZERO_WRITE, WRITE_ERROR, FLUSH_ERROR
};
Py000WriteFault g_write_fault = WRITE_OK;
int g_write_seek_calls = 0, g_write_write_calls = 0, g_write_flush_calls = 0;
int g_write_send_calls = 0, g_write_check_calls = 0;
int g_write_last_handle = INVALID_HANDLE;

bool Py000TestJournalSeek(const int handle, const long offset, const ENUM_FILE_POSITION origin)
{
   g_write_seek_calls++;
   g_write_last_handle = handle;
   if(g_write_fault == SEEK_FALSE)
   {
      SetUserError(71);
      return false;
   }
   bool result = FileSeek(handle, offset, origin);
   if(g_write_fault == SEEK_ERROR) SetUserError(72);
   return result;
}
uint Py000TestJournalWrite(const int handle, const string text)
{
   g_write_write_calls++;
   g_write_last_handle = handle;
   if(g_write_fault == ZERO_WRITE)
   {
      SetUserError(73);
      return 0;
   }
   // FILE_TXT ignores FileWriteString's length argument: actually write a prefix.
   string value = g_write_fault == SHORT_WRITE ? StringSubstr(text, 0, 1) : text;
   uint result = FileWriteString(handle, value);
   if(g_write_fault == WRITE_ERROR) SetUserError(74);
   return result;
}
void Py000TestJournalFlush(const int handle)
{
   g_write_flush_calls++;
   FileFlush(handle);
   if(g_write_fault == FLUSH_ERROR) SetUserError(75);
}
bool Py000TestNeverSend(const MqlTradeRequest &request, MqlTradeResult &result)
{
   g_write_send_calls++;
   return false;
}
bool Py000TestNeverCheck(const MqlTradeRequest &request, MqlTradeCheckResult &result)
{
   g_write_check_calls++;
   return false;
}
#define PY000_JOURNAL_SEEK Py000TestJournalSeek
#define PY000_JOURNAL_WRITE Py000TestJournalWrite
#define PY000_JOURNAL_FLUSH Py000TestJournalFlush
#include "../include/Py000Json.mqh"
#include "../include/Py000Journal.mqh"
// These aliases apply to the actual implementation: this script cannot send/check a trade.
#define OrderSend Py000TestNeverSend
#define OrderCheck Py000TestNeverCheck
#include "../include/Py000Execution.mqh"
#undef OrderSend
#undef OrderCheck

void Py000WriteCheck(const string label, const bool condition)
{
   g_write_checks++;
   if(condition) return;
   g_write_failures++;
   Print("FAIL ", label);
}
string Py000ProbeHex(const uchar &bytes[])
{
   string hex = "";
   for(int index = 0; index < ArraySize(bytes); index++)
      hex += StringFormat("%02x", (uint)bytes[index]);
   return hex;
}
bool Py000ProbeFreshFile(const string file_name)
{
   bool fresh = StringFind(file_name, g_write_prefix) == 0
      && !FileIsExist(file_name, FILE_COMMON);
   Py000WriteCheck("fresh probe path " + file_name, fresh);
   return fresh;
}
void Py000ProbeCleanup(const string file_name, const int failures_before)
{
   if(g_write_failures == failures_before)
      Py000WriteCheck("cleanup " + file_name, FileDelete(file_name, FILE_COMMON));
   else
      Print("PRESERVED_FAILED_PROBE ", file_name);
}
string Py000ProbeFileHex(const string file_name)
{
   int handle = FileOpen(file_name, FILE_READ | FILE_BIN | FILE_COMMON);
   Py000WriteCheck("read probe " + file_name, handle != INVALID_HANDLE);
   if(handle == INVALID_HANDLE) return "";
   int size = (int)FileSize(handle);
   uchar bytes[];
   ArrayResize(bytes, size);
   uint read = FileReadArray(handle, bytes, 0, size);
   FileClose(handle);
   Py000WriteCheck("complete probe read " + file_name, read == (uint)size);
   return Py000ProbeHex(bytes);
}
void Py000ProbeResetFault(const Py000WriteFault fault)
{
   g_write_fault = fault;
   g_write_seek_calls = 0;
   g_write_write_calls = 0;
   g_write_flush_calls = 0;
   g_write_send_calls = 0;
   g_write_check_calls = 0;
   g_write_last_handle = INVALID_HANDLE;
}
void Py000ProbeWriterClosed()
{
   ResetLastError();
   FileTell(g_write_last_handle);
   Py000WriteCheck("writer always closes handle", GetLastError() != 0);
}
void Py000ProbeNativeTextWrite(const string label, const string text, const string expected_hex)
{
   string file_name = g_write_prefix + label + ".probe";
   if(FileIsExist(file_name, FILE_COMMON))
   {
      Py000WriteCheck("refuse existing probe file " + file_name, false);
      return;
   }
   int failures_before = g_write_failures;
   int handle = FileOpen(file_name,
      FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON, 0, CP_UTF8);
   Py000WriteCheck(label + " open", handle != INVALID_HANDLE);
   if(handle == INVALID_HANDLE) return;
   ulong start = FileTell(handle);
   ResetLastError();
   uint written = FileWriteString(handle, text);
   int write_error = GetLastError();
   ulong end = FileTell(handle);
   ResetLastError();
   FileFlush(handle);
   int flush_error = GetLastError();
   ulong size = FileSize(handle);
   FileClose(handle);
   handle = FileOpen(file_name, FILE_READ | FILE_BIN | FILE_COMMON);
   Py000WriteCheck(label + " reopen binary", handle != INVALID_HANDLE);
   if(handle == INVALID_HANDLE) return;
   uchar bytes[];
   ArrayResize(bytes, (int)size);
   uint read = FileReadArray(handle, bytes, 0, (int)size);
   FileClose(handle);
   string actual_hex = Py000ProbeHex(bytes);
   PrintFormat("PY000_WRITE_PROBE %s written=%u offset=%I64u size=%I64u write_error=%d flush_error=%d hex=%s",
      label, written, end - start, size, write_error, flush_error, actual_hex);
   Py000WriteCheck(label + " exact bytes", actual_hex == expected_hex);
   Py000WriteCheck(label + " returned byte count", written == size && end - start == size);
   Py000WriteCheck(label + " read bytes", read == size);
   Py000WriteCheck(label + " no write or flush error", write_error == 0 && flush_error == 0);
   if(g_write_failures == failures_before)
      Py000WriteCheck(label + " cleanup", FileDelete(file_name, FILE_COMMON));
   else
      Print("PRESERVED_FAILED_PROBE ", file_name);
}
void Py000ProbeActualWriter(
   const string label, const string contents, const string expected_hex, const bool append
)
{
   string file_name = g_write_prefix + label + ".probe";
   if(!Py000ProbeFreshFile(file_name)) return;
   int failures_before = g_write_failures;
   Py000ProbeResetFault(WRITE_OK);
   if(append) Py000WriteCheck(label + " seed", Py000JournalWriteLine(file_name, "old", false));
   Py000ProbeResetFault(WRITE_OK);
   SetUserError(76); // An unrelated earlier error must not poison a successful write.
   Py000WriteCheck(label + " writer success", Py000JournalWriteLine(file_name, contents, append));
   Py000WriteCheck(label + " write sequence", g_write_seek_calls == (append ? 1 : 0)
      && g_write_write_calls == 1 && g_write_flush_calls == 1);
   Py000ProbeWriterClosed();
   string actual = Py000ProbeFileHex(file_name);
   Py000WriteCheck(label + " unchanged wire bytes", actual == expected_hex);
   Py000ProbeCleanup(file_name, failures_before);
}
void Py000ProbeWriterFault(const Py000WriteFault fault)
{
   string file_name = g_write_prefix + "fault_" + IntegerToString((int)fault) + ".probe";
   if(!Py000ProbeFreshFile(file_name)) return;
   int failures_before = g_write_failures;
   Py000ProbeResetFault(WRITE_OK);
   Py000WriteCheck("fault seed", Py000JournalWriteLine(file_name, "old", false));
   Py000ProbeResetFault(fault);
   Py000WriteCheck("fault is not success", !Py000JournalWriteLine(file_name, "abc", true));
   bool seek_failed = fault == SEEK_FALSE || fault == SEEK_ERROR;
   Py000WriteCheck("failed seek never writes", g_write_seek_calls == 1
      && g_write_write_calls == (seek_failed ? 0 : 1)
      && g_write_flush_calls == (seek_failed ? 0 : 1));
   Py000ProbeWriterClosed();
   string expected = "6f6c640d0a";
   if(fault == SHORT_WRITE) expected += "61";
   else if(fault == WRITE_ERROR || fault == FLUSH_ERROR) expected += "6162630d0a";
   string actual = Py000ProbeFileHex(file_name);
   Py000WriteCheck("failed write retains original and partial bytes", actual == expected);
   Py000ProbeCleanup(file_name, failures_before);
}
bool Py000ProbeJournalFixture(const string label, string &file_name)
{
   g_py000_journal_namespace = g_write_prefix + label;
   file_name = Py000JournalEventsFile();
   if(!Py000ProbeFreshFile(file_name)) return false;
   g_py000_journal_stream_id = "stream-write-test";
   g_py000_journal_first_cursor = "0";
   ArrayResize(g_py000_journal_events, 0);
   g_py000_journal_ready = true;
   g_py000_execution_recovery_ready = true;
   g_py000_execution_enabled = true;
   Py000ProbeResetFault(WRITE_OK);
   string header = "H|1|" + g_py000_journal_namespace + "|" + g_py000_journal_stream_id + "|0";
   bool seeded = Py000JournalWriteLine(file_name, header + "|" + Py000JournalChecksum(header), false);
   Py000WriteCheck("synthetic journal header", seeded);
   if(!seeded) Print("PRESERVED_FAILED_PROBE ", file_name);
   return seeded;
}
void Py000ProbeReservationFault(const Py000WriteFault fault)
{
   string file_name;
   if(!Py000ProbeJournalFixture("reserve_" + IntegerToString((int)fault), file_name)) return;
   int failures_before = g_write_failures;
   Py000ProbeResetFault(fault);
   Py000Request request = {};
   request.op = "submit_market_delta";
   request.client_request_id = "request-write-test";
   request.side = "buy";
   request.quantity_lots = "0.01";
   Py000JournalEvent outcome;
   string error_code, error_message;
   bool result = Py000ExecutionSubmit(request, "boot-write-test", outcome, error_code, error_message);
   Py000WriteCheck("reserve fault blocks submit", !result && error_code == "RECOVERY_BLOCKED"
      && error_message == "submission reservation could not be persisted");
   Py000WriteCheck("reserve fault sends nothing", g_write_send_calls == 0 && g_write_check_calls == 0);
   Py000WriteCheck("reserve fault not committed in memory", ArraySize(g_py000_journal_events) == 0
      && !g_py000_execution_recovery_ready);
   Py000ProbeWriterClosed();
   Py000ProbeCleanup(file_name, failures_before);
}
void Py000ProbeTerminalFault(const Py000WriteFault fault, const bool filled)
{
   string file_name;
   string label = (filled ? "filled_" : "unknown_") + IntegerToString((int)fault);
   if(!Py000ProbeJournalFixture(label, file_name)) return;
   int failures_before = g_write_failures;
   Py000JournalEvent event;
   Py000WriteCheck("reserve before synthetic result", Py000JournalAppendReserved(
      "boot-write-test", "1000", "request-write-test", "buy", "0.01", "", "", event));
   Py000ProbeResetFault(fault);
   bool result;
   if(filled)
      result = Py000JournalAppendFilled("boot-write-test", "1001", "request-write-test",
         "0.01", "2400", "11", "12", "13", "-1.25", "10009", event);
   else
      result = Py000JournalAppendRejectedOrUnknown("boot-write-test", "1001", "order_unknown",
         "request-write-test", "ORDER_SEND_UNCERTAIN", "10006", event);
   Py000WriteCheck("terminal fault stays unresolved", !result
      && !g_py000_execution_recovery_ready && Py000JournalOutcomeIndex("request-write-test") == -1);
   Py000WriteCheck("terminal fault keeps reservation", ArraySize(g_py000_journal_events) == 1
      && g_py000_journal_events[0].event_type == "submission_reserved");
   Py000WriteCheck("terminal test never trades", g_write_send_calls == 0 && g_write_check_calls == 0);
   Py000ProbeWriterClosed();
   Py000ProbeCleanup(file_name, failures_before);
}
void Py000ProbeOldUnknownReplay()
{
   string file_name;
   if(!Py000ProbeJournalFixture("old_unknown", file_name)) return;
   int failures_before = g_write_failures;
   Py000JournalEvent event;
   Py000WriteCheck("old unknown reserve", Py000JournalAppendReserved(
      "boot-write-test", "1000", "request-write-test", "buy", "0.01", "", "", event));
   Py000WriteCheck("persist old unknown", Py000JournalAppendRejectedOrUnknown(
      "boot-write-test", "1001", "order_unknown", "request-write-test",
      "ORDER_SEND_UNCERTAIN", "10006", event));
   Py000WriteCheck("reload existing format", Py000JournalLoadEvents());
   Py000WriteCheck("old unknown remains blocked", !g_py000_execution_recovery_ready
      && ArraySize(g_py000_journal_events) == 2
      && g_py000_journal_events[1].event_type == "order_unknown");
   Py000ProbeResetFault(WRITE_OK);
   Py000Request request = {};
   request.client_request_id = "request-write-test";
   request.side = "buy";
   request.quantity_lots = "0.01";
   string error_code, error_message;
   Py000WriteCheck("old unknown replay unchanged", Py000ExecutionSubmit(
      request, "boot-next-test", event, error_code, error_message)
      && event.event_type == "order_unknown" && event.broker_retcode == "10006");
   Py000WriteCheck("old unknown no rewrite or resend", g_write_write_calls == 0
      && g_write_send_calls == 0 && g_write_check_calls == 0 && !g_py000_execution_recovery_ready);
   Py000ProbeCleanup(file_name, failures_before);
}
int OnStart()
{
   g_write_prefix = "PY000_W2C_PROBE_" + IntegerToString((long)GetTickCount64())
      + "_" + IntegerToString((long)GetMicrosecondCount()) + "_";
   Py000ProbeNativeTextWrite("ascii_lf", "abc\n", "6162630d0a");
   string unicode_text = ShortToString(0x8d39) + ShortToString(0x7528);
   Py000ProbeNativeTextWrite("utf8_lf", unicode_text + "\n", "e8b4b9e794a80d0a");
   string supplementary = ShortToString(0xd83d) + ShortToString(0xde00);
   Py000ProbeNativeTextWrite("utf8_pair", supplementary + "\n", "f09f98800d0a");
   Py000ProbeNativeTextWrite("mixed_newlines", "ab\r\ncd\n", "61620d0a63640d0a");
   Py000ProbeNativeTextWrite("existing_cr", "\r\r\n", "0d0d0a");
   ResetLastError();
   FileFlush(INVALID_HANDLE);
   int invalid_flush_error = GetLastError();
   PrintFormat("PY000_FLUSH_PROBE invalid_handle_error=%d", invalid_flush_error);
   Py000WriteCheck("invalid flush is observable", invalid_flush_error != 0);
   Py000ProbeActualWriter("writer_ascii", "abc", "6162630d0a", false);
   Py000ProbeActualWriter("writer_utf8", unicode_text, "e8b4b9e794a80d0a", false);
   Py000ProbeActualWriter("writer_pair", supplementary, "f09f98800d0a", false);
   Py000ProbeActualWriter("writer_crlf", "ab\r\ncd", "61620d0a63640d0a", false);
   Py000ProbeActualWriter("writer_cr", "\r\r", "0d0d0a", false);
   Py000ProbeActualWriter("writer_empty", "", "0d0a", false);
   Py000ProbeActualWriter("writer_append", unicode_text, "6f6c640d0ae8b4b9e794a80d0a", true);
   for(int fault = (int)SEEK_FALSE; fault <= (int)FLUSH_ERROR; fault++)
   {
      Py000ProbeWriterFault((Py000WriteFault)fault);
      Py000ProbeReservationFault((Py000WriteFault)fault);
      Py000ProbeTerminalFault((Py000WriteFault)fault, true);
      Py000ProbeTerminalFault((Py000WriteFault)fault, false);
   }
   Py000ProbeOldUnknownReplay();
   PrintFormat("PY000_JOURNAL_WRITE_TEST checks=%d failures=%d", g_write_checks, g_write_failures);
   return g_write_failures == 0 ? 0 : 1;
}
