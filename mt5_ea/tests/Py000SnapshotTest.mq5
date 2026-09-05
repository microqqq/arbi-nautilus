#property strict
#property description "Injected positions snapshots; no native account or journal access"
#include "../include/Py000Json.mqh"
#include "../include/Py000Journal.mqh"
#include "../include/Py000Execution.mqh"

struct Py000SnapshotFixture
{
   ulong ticket;
   long ticket_property, identifier, magic, side, time_msc;
   string symbol, comment;
   double volume, open_price, current_price, sl, tp, profit, swap;
};
Py000SnapshotFixture g_snapshot_rows[6];
int g_snapshot_totals[4];
int g_snapshot_total_calls = 0;
int g_snapshot_selected = 0;
int g_snapshot_fail_kind = 0;
int g_snapshot_fail_property = 0;
int g_snapshot_fail_pass = 0;
int g_snapshot_checks = 0;
int g_snapshot_failures = 0;

int Py000SnapshotPass()
{
   return g_snapshot_total_calls > 2 ? 1 : 0;
}
bool Py000SnapshotGetterFails(const int kind, const int property)
{
   return kind == g_snapshot_fail_kind && property == g_snapshot_fail_property
      && Py000SnapshotPass() == g_snapshot_fail_pass && g_snapshot_selected == 1;
}
int Py000TestPositionsTotal()
{
   int call = g_snapshot_total_calls++;
   if(call >= 4) return -1;
   if(g_snapshot_fail_kind == 1) SetUserError(17);
   return g_snapshot_totals[call];
}
ulong Py000TestPositionTicket(const int index)
{
   g_snapshot_selected = index;
   if(index < 0 || index >= 3 || Py000SnapshotGetterFails(2, 0)) return 0;
   return g_snapshot_rows[Py000SnapshotPass() * 3 + index].ticket;
}
bool Py000TestPositionInteger(const ENUM_POSITION_PROPERTY_INTEGER property, long &value)
{
   if(Py000SnapshotGetterFails(3, (int)property)) return false;
   Py000SnapshotFixture row = g_snapshot_rows[Py000SnapshotPass() * 3 + g_snapshot_selected];
   switch(property)
   {
      case POSITION_TICKET: value = row.ticket_property; return true;
      case POSITION_IDENTIFIER: value = row.identifier; return true;
      case POSITION_MAGIC: value = row.magic; return true;
      case POSITION_TYPE: value = row.side; return true;
      case POSITION_TIME_MSC: value = row.time_msc; return true;
      default: return false;
   }
}
bool Py000TestPositionString(const ENUM_POSITION_PROPERTY_STRING property, string &value)
{
   if(Py000SnapshotGetterFails(4, (int)property)) return false;
   Py000SnapshotFixture row = g_snapshot_rows[Py000SnapshotPass() * 3 + g_snapshot_selected];
   switch(property)
   {
      case POSITION_SYMBOL: value = row.symbol; return true;
      case POSITION_COMMENT: value = row.comment; return true;
      default: return false;
   }
}
bool Py000TestPositionDouble(const ENUM_POSITION_PROPERTY_DOUBLE property, double &value)
{
   if(Py000SnapshotGetterFails(5, (int)property)) return false;
   Py000SnapshotFixture row = g_snapshot_rows[Py000SnapshotPass() * 3 + g_snapshot_selected];
   switch(property)
   {
      case POSITION_VOLUME: value = row.volume; return true;
      case POSITION_PRICE_OPEN: value = row.open_price; return true;
      case POSITION_PRICE_CURRENT: value = row.current_price; return true;
      case POSITION_SL: value = row.sl; return true;
      case POSITION_TP: value = row.tp; return true;
      case POSITION_PROFIT: value = row.profit; return true;
      case POSITION_SWAP: value = row.swap; return true;
      default: return false;
   }
}
#define PY000_SNAPSHOT_TOTAL Py000TestPositionsTotal
#define PY000_SNAPSHOT_TICKET Py000TestPositionTicket
#define PY000_SNAPSHOT_INTEGER Py000TestPositionInteger
#define PY000_SNAPSHOT_STRING Py000TestPositionString
#define PY000_SNAPSHOT_DOUBLE Py000TestPositionDouble
#include "../include/Py000Protocol.mqh"

void Py000SnapshotReset()
{
   g_snapshot_total_calls = 0;
   g_snapshot_selected = 0;
   g_snapshot_fail_kind = 0;
   g_snapshot_fail_property = 0;
   g_snapshot_fail_pass = 0;
   for(int index = 0; index < 4; index++) g_snapshot_totals[index] = 3;
   for(int index = 0; index < 3; index++)
   {
      Py000SnapshotFixture row = {};
      row.ticket = (ulong)(101 + index);
      row.ticket_property = (long)row.ticket;
      row.identifier = 201 + index;
      row.magic = 7;
      row.side = index % 2 == 0 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
      row.time_msc = 1700000000000;
      row.symbol = _Symbol;
      row.comment = "snapshot-test";
      row.volume = 1.0 + index;
      row.open_price = 2400.0;
      row.current_price = 2401.0;
      row.profit = 1.0;
      g_snapshot_rows[index] = row;
      g_snapshot_rows[index + 3] = row;
   }
}
void Py000SnapshotCheck(const string label, const bool condition)
{
   g_snapshot_checks++;
   if(condition) return;
   g_snapshot_failures++;
   Print("FAIL ", label);
}
string Py000SnapshotExpect(
   const string label,
   const string expected_error = "SNAPSHOT_UNAVAILABLE",
   const int expected_positions = 3
)
{
   string json = "must not survive a failed sample", error_code;
   bool success = Py000BuildPositionsJson(json, error_code);
   Py000SnapshotCheck(label + " status", success == (expected_error == ""));
   Py000SnapshotCheck(label + " error", error_code == expected_error);
   if(expected_error != "")
      Py000SnapshotCheck(label + " no partial output", json == "");
   else
   {
      int count = 0, offset = 0;
      while((offset = StringFind(json, "\"ticket\":", offset)) >= 0)
      {
         count++;
         offset++;
      }
      Py000SnapshotCheck(label + " complete count", count == expected_positions);
      Py000SnapshotCheck(label + " exactly two samples", g_snapshot_total_calls == 4);
      if(expected_positions == 0) Py000SnapshotCheck(label + " empty", json == "[]");
   }
   return json;
}
int OnStart()
{
   Py000SnapshotReset();
   Py000SnapshotExpect("stable", "");
   Py000SnapshotReset();
   for(int index = 0; index < 4; index++) g_snapshot_totals[index] = 0;
   Py000SnapshotExpect("complete empty", "", 0);
   Py000SnapshotReset();
   for(int index = 0; index < 3; index++)
   {
      g_snapshot_rows[3 + index] = g_snapshot_rows[2 - index];
      g_snapshot_rows[3 + index].current_price = 2500.0;
      g_snapshot_rows[3 + index].profit = 100.0;
      g_snapshot_rows[3 + index].swap = -2.0;
   }
   string json = Py000SnapshotExpect("reordered and floating values changed", "");
   Py000SnapshotCheck("second sample values used", StringFind(json, "\"price_current\":\"2500\"") >= 0);
   Py000SnapshotReset();
   g_snapshot_rows[1].symbol = _Symbol + "-OTHER";
   g_snapshot_rows[4].symbol = _Symbol + "-OTHER";
   g_snapshot_fail_kind = 3; // Filter succeeds before reading other-symbol fields.
   g_snapshot_fail_property = (int)POSITION_IDENTIFIER;
   Py000SnapshotExpect("foreign symbol", "", 2);
   Py000SnapshotReset();
   for(int index = 0; index < 6; index++) g_snapshot_rows[index].symbol = _Symbol + "-OTHER";
   Py000SnapshotExpect("complete symbol-scoped empty", "", 0);
   Py000SnapshotReset();
   g_snapshot_rows[1].magic = 0;
   g_snapshot_rows[4].magic = 0;
   Py000SnapshotExpect("manual position remains in snapshot", "");

   int integer_properties[] = {POSITION_TICKET, POSITION_IDENTIFIER, POSITION_MAGIC, POSITION_TYPE, POSITION_TIME_MSC};
   int string_properties[] = {POSITION_SYMBOL, POSITION_COMMENT};
   int double_properties[] = {POSITION_VOLUME, POSITION_PRICE_OPEN, POSITION_PRICE_CURRENT, POSITION_SL, POSITION_TP, POSITION_PROFIT, POSITION_SWAP};
   for(int pass = 0; pass < 2; pass++)
   {
      for(int kind = 2; kind <= 5; kind++)
      {
         int count = kind == 2 ? 1 : (kind == 3 ? ArraySize(integer_properties)
            : (kind == 4 ? ArraySize(string_properties) : ArraySize(double_properties)));
         for(int index = 0; index < count; index++)
         {
            Py000SnapshotReset();
            g_snapshot_fail_kind = kind;
            g_snapshot_fail_pass = pass;
            g_snapshot_fail_property = kind == 2 ? 0 : (kind == 3 ? integer_properties[index]
               : (kind == 4 ? string_properties[index] : double_properties[index]));
            Py000SnapshotExpect(StringFormat("getter failure pass=%d kind=%d property=%d",
               pass, kind, g_snapshot_fail_property));
         }
      }
   }
   Py000SnapshotReset();
   g_snapshot_fail_kind = 1;
   Py000SnapshotExpect("positions total error");
   Py000SnapshotReset();
   g_snapshot_totals[0] = -1;
   Py000SnapshotExpect("invalid total");
   for(int index = 1; index < 4; index++)
   {
      Py000SnapshotReset();
      g_snapshot_totals[index] = 2;
      Py000SnapshotExpect("enumeration count changed");
   }
   Py000SnapshotReset();
   g_snapshot_totals[2] = 2;
   g_snapshot_totals[3] = 2;
   Py000SnapshotExpect("position removed between samples");
   Py000SnapshotReset();
   g_snapshot_totals[0] = 2;
   g_snapshot_totals[1] = 2;
   Py000SnapshotExpect("position added between samples");
   for(int field = 0; field < 6; field++)
   {
      Py000SnapshotReset();
      if(field == 0) { g_snapshot_rows[4].ticket = 900; g_snapshot_rows[4].ticket_property = 900; }
      if(field == 1) g_snapshot_rows[4].identifier = 900;
      if(field == 2) g_snapshot_rows[4].symbol = _Symbol + "-OTHER";
      if(field == 3) g_snapshot_rows[4].magic++;
      if(field == 4) g_snapshot_rows[4].side = POSITION_TYPE_BUY;
      if(field == 5) g_snapshot_rows[4].volume += 0.5;
      Py000SnapshotExpect(StringFormat("stable field %d changed", field));
   }
   Py000SnapshotReset();
   g_snapshot_rows[4].volume += 1e-10;
   Py000SnapshotExpect("volume changes below wire rounding still detected");
   for(int invalid = 0; invalid < 10; invalid++)
   {
      Py000SnapshotReset();
      if(invalid == 0) g_snapshot_rows[1].ticket = 0;
      if(invalid == 1) g_snapshot_rows[1].ticket_property = 999;
      if(invalid == 2) g_snapshot_rows[1].identifier = 0;
      if(invalid == 3) g_snapshot_rows[1].identifier = g_snapshot_rows[0].identifier;
      if(invalid == 4) { g_snapshot_rows[1].ticket = 101; g_snapshot_rows[1].ticket_property = 101; }
      if(invalid == 5) g_snapshot_rows[1].side = 999;
      if(invalid == 6) g_snapshot_rows[1].volume = 0.0;
      if(invalid == 7) g_snapshot_rows[1].volume = MathArcsin(2.0);
      if(invalid == 8) g_snapshot_rows[1].magic = -1;
      if(invalid == 9) g_snapshot_rows[1].time_msc = 0;
      Py000SnapshotExpect(StringFormat("invalid native fact %d", invalid));
   }
   Py000SnapshotReset();
   g_snapshot_rows[1].comment = "\ninvalid";
   Py000SnapshotExpect("malformed comment stays schema error", "SCHEMA_MISMATCH");
   Py000SnapshotReset();
   g_snapshot_rows[1].current_price = MathArcsin(2.0);
   Py000SnapshotExpect("malformed price stays schema error", "SCHEMA_MISMATCH");
   PrintFormat("PY000_SNAPSHOT_TEST checks=%d failures=%d", g_snapshot_checks, g_snapshot_failures);
   return g_snapshot_failures == 0 ? 0 : 1;
}
