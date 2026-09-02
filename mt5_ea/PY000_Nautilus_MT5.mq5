#property strict
#property version "1.00"
#property description "PY000 MT5 v1 read-only plus explicit demo MARKET/FOK canary"
#include "include/Py000Json.mqh"
#include "include/Py000Journal.mqh"
#include "include/Py000Execution.mqh"
#include "include/Py000Protocol.mqh"
#include "include/Py000Zmq.mqh"
input string InpPubBind = "tcp://127.0.0.1:6101";
input string InpRepBind = "tcp://127.0.0.1:6102";
input ulong InpMagic = 900000001;
input string InpEaBuildId = "py000-mt5-ea-v1-demo-canary";
input string InpDeclaredSourceSha256 =
   "0000000000000000000000000000000000000000000000000000000000000000";
input string InpServerTimezone = "Europe/Athens";
input int InpSessionFreshnessSeconds = 10;
input Py000ExecutionMode InpExecutionMode = PY000_EXECUTION_DISABLED;
input double InpMaxOrderLots = 0.01;
input ulong InpDeviationPoints = 20;
input int InpPumpMaxRequests = 8;
input int InpPumpBudgetMicros = 2000;
input int InpHeartbeatMs = 1000;
int g_py000_owner_handle = INVALID_HANDLE;
bool g_py000_pump_active = false;
bool g_py000_transport_failed = false;
string g_py000_last_tick_time_ms = "";
ulong g_py000_last_heartbeat_mono = 0;
string Py000BuildNamespace()
{
   string server = AccountInfoString(ACCOUNT_SERVER);
   string login = Py000UlongString((ulong)AccountInfoInteger(ACCOUNT_LOGIN));
   string account;
   string magic = Py000UlongString(InpMagic);
   if(!Py000DeriveAccountId(server, login, account)
      || !Py000OptionalBrokerText(_Symbol, 128))
      return "";
   uchar domain[], account_bytes[], symbol_bytes[], magic_bytes[];
   int domain_count = StringToCharArray(
      "py000.mt5/journal-namespace/v1", domain, 0, WHOLE_ARRAY, CP_UTF8
   );
   int account_count = StringToCharArray(account, account_bytes, 0, WHOLE_ARRAY, CP_UTF8);
   int symbol_count = StringToCharArray(_Symbol, symbol_bytes, 0, WHOLE_ARRAY, CP_UTF8);
   int magic_count = StringToCharArray(magic, magic_bytes, 0, WHOLE_ARRAY, CP_UTF8);
   if(domain_count <= 1 || account_count <= 1 || symbol_count <= 1 || magic_count <= 1)
      return "";
   uchar canonical[];
   ArrayResize(canonical, domain_count + account_count + symbol_count + magic_count - 1);
   int offset = 0;
   for(int index = 0; index < domain_count; index++) canonical[offset++] = domain[index];
   for(int index = 0; index < account_count; index++) canonical[offset++] = account_bytes[index];
   for(int index = 0; index < symbol_count; index++) canonical[offset++] = symbol_bytes[index];
   for(int index = 0; index < magic_count - 1; index++) canonical[offset++] = magic_bytes[index];
   string digest;
   if(!Py000Sha256Hex(canonical, digest)) return "";
   return PY000_JOURNAL_PREFIX + digest;
}
bool Py000AcquireOwner(const string journal_namespace)
{
   string owner_name = journal_namespace + ".owner.lock";
   g_py000_owner_handle = FileOpen(
      owner_name,
      FILE_READ | FILE_WRITE | FILE_BIN | FILE_COMMON
   );
   if(g_py000_owner_handle == INVALID_HANDLE)
      return false;
   FileSeek(g_py000_owner_handle, 0, SEEK_SET);
   string custody = journal_namespace + "|" + IntegerToString((long)ChartID());
   if(FileWriteString(g_py000_owner_handle, custody) <= 0)
   {
      FileClose(g_py000_owner_handle);
      g_py000_owner_handle = INVALID_HANDLE;
      return false;
   }
   FileFlush(g_py000_owner_handle);
   return true;
}
void Py000ReleaseOwner()
{
   if(g_py000_owner_handle != INVALID_HANDLE)
      FileClose(g_py000_owner_handle);
   g_py000_owner_handle = INVALID_HANDLE;
}
void Py000FailTransport(
   const string reason,
   const bool has_errno,
   const int error_code
)
{
   if(g_py000_transport_failed)
      return;
   g_py000_transport_failed = true;
   if(has_errno)
      PrintFormat("PY000 transport failed closed: %s errno=%d", reason, error_code);
   else
      PrintFormat("PY000 transport failed closed: %s", reason);
   ExpertRemove();
}
void Py000PumpRep()
{
   if(g_py000_pump_active)
      return;
   g_py000_pump_active = true;
   ulong started = GetMicrosecondCount();
   int maximum = MathMax(1, MathMin(InpPumpMaxRequests, 64));
   for(int handled = 0; handled < maximum; handled++)
   {
      if(GetMicrosecondCount() - started > (ulong)MathMax(InpPumpBudgetMicros, 100))
         break;
      string request;
      string malformed_reason;
      string fatal_reason;
      int fatal_errno = 0;
      bool fatal_has_errno = false;
      int receive_status = Py000ZmqRepTryRecv(
         request,
         malformed_reason,
         fatal_reason,
         fatal_errno,
         fatal_has_errno
      );
      if(receive_status == PY000_ZMQ_RECV_NONE)
         break;
      if(receive_status == PY000_ZMQ_RECV_FATAL)
      {
         g_py000_pump_active = false;
         Py000FailTransport(fatal_reason, fatal_has_errno, fatal_errno);
         return;
      }
      string response;
      if(malformed_reason != "")
         response = Py000ErrorResponse(
            "invalid", "unknown", "MALFORMED", malformed_reason
         );
      else
         response = Py000HandleRequest(request);
      if(!Py000ZmqRepSend(response, fatal_reason, fatal_errno, fatal_has_errno))
      {
         g_py000_pump_active = false;
         Py000FailTransport(fatal_reason, fatal_has_errno, fatal_errno);
         return;
      }
   }
   g_py000_pump_active = false;
}
int OnInit()
{
   if(InpPumpMaxRequests < 1 || InpPumpMaxRequests > 64
      || InpPumpBudgetMicros < 100 || InpPumpBudgetMicros > 50000
      || InpHeartbeatMs < 250 || InpHeartbeatMs > 60000
      || InpSessionFreshnessSeconds < 1 || InpSessionFreshnessSeconds > 300)
      return INIT_FAILED;
   g_py000_session_freshness_seconds = InpSessionFreshnessSeconds;
   if(!Py000ExecutionConfigure(
         InpExecutionMode,
         InpMaxOrderLots,
         InpDeviationPoints,
         InpMagic,
         InpSessionFreshnessSeconds
      ))
      return INIT_FAILED;
   string journal_namespace = Py000BuildNamespace();
   if(journal_namespace == "")
      return INIT_FAILED;
   if(!Py000AcquireOwner(journal_namespace))
      return INIT_FAILED;
   if(!Py000JournalInit(journal_namespace))
   {
      Py000ReleaseOwner();
      return INIT_FAILED;
   }
   string boot_id = Py000JournalNewId("boot");
   if(!Py000BuildIdentity(
         InpEaBuildId,
         InpDeclaredSourceSha256,
         InpServerTimezone,
         InpMagic,
         g_py000_journal_stream_id,
         boot_id,
         g_py000_execution_enabled
      ))
      {
      Py000ReleaseOwner();
      return INIT_FAILED;
   }
   if(!Py000ZmqInit(InpPubBind, InpRepBind))
   {
      Py000ReleaseOwner();
      return INIT_FAILED;
   }
   if(!EventSetMillisecondTimer(250))
   {
      Py000ZmqShutdown();
      Py000ReleaseOwner();
      return INIT_FAILED;
   }
   if(g_py000_journal_ready
      && !Py000JournalAppendStreamStarted(
         boot_id,
         Py000ObservedUtcMs(),
         InpEaBuildId,
         g_py000_execution_enabled
      ))
      {
      EventKillTimer();
      Py000ZmqShutdown();
      Py000ReleaseOwner();
      return INIT_FAILED;
   }
   g_py000_last_heartbeat_mono = GetTickCount64();
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason)
{
   EventKillTimer();
   Py000ZmqShutdown();
   Py000ReleaseOwner();
}
void OnTick()
{
   if(g_py000_transport_failed)
      return;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.time_msc <= 0)
      return;
   bool market_open_hint = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE)
      != SYMBOL_TRADE_MODE_DISABLED && tick.bid > 0.0 && tick.ask > 0.0;
   string payload = Py000TickJson(tick, market_open_hint);
   if(payload == "")
      return;
   g_py000_last_tick_time_ms = Py000UlongString((ulong)tick.time_msc);
   Py000ZmqPubSend(_Symbol, payload);
   Py000PumpRep();
}
void OnTimer()
{
   if(g_py000_transport_failed)
      return;
   Py000PumpRep();
   ulong now_mono = GetTickCount64();
   if(now_mono - g_py000_last_heartbeat_mono < (ulong)InpHeartbeatMs)
      return;
   MqlTick tick;
   bool tick_available = SymbolInfoTick(_Symbol, tick);
   bool market_open_hint = tick_available
      && SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_DISABLED
      && tick.bid > 0.0 && tick.ask > 0.0;
   string payload = Py000HeartbeatJson(
      Py000ObservedUtcMs(),
      g_py000_last_tick_time_ms,
      market_open_hint
   );
   Py000ZmqPubSend(_Symbol, payload);
   g_py000_last_heartbeat_mono = now_mono;
}
