#property strict
#property description "Pure result-classifier tests; no trading or journal operations"
#include "../include/Py000Json.mqh"
#include "../include/Py000Journal.mqh"
#include "../include/Py000Execution.mqh"

int g_retcode_checks = 0;
int g_retcode_failures = 0;

void Py000RetcodeCheck(
   const string label,
   const MqlTradeResult &result,
   const bool expected
)
{
   g_retcode_checks++;
   bool actual = Py000ExecutionResultIsRejected(result);
   if(actual != expected)
   {
      g_retcode_failures++;
      PrintFormat("FAIL %s retcode=%u expected=%d actual=%d",
         label, result.retcode, (int)expected, (int)actual);
   }
}

int OnStart()
{
   double invalid_volume = MathArcsin(2.0);
   double infinite_volume = MathExp(1000.0);
   if(MathIsValidNumber(invalid_volume) || MathIsValidNumber(infinite_volume))
   {
      Print("FAIL non-finite test fixtures are finite");
      return 1;
   }
   uint rejected[] = {
      TRADE_RETCODE_REJECT, TRADE_RETCODE_MARKET_CLOSED, TRADE_RETCODE_NO_MONEY,
      TRADE_RETCODE_REQUOTE, TRADE_RETCODE_PRICE_CHANGED, TRADE_RETCODE_PRICE_OFF
   };
   for(int index = 0; index < ArraySize(rejected); index++)
   {
      MqlTradeResult result = {};
      result.retcode = rejected[index];
      Py000RetcodeCheck("known rejection", result, true);
      result.order = 1;
      Py000RetcodeCheck("contradictory order", result, false);
      result.order = 0;
      result.deal = 1;
      Py000RetcodeCheck("contradictory deal", result, false);
      result.deal = 0;
      result.volume = 0.01;
      Py000RetcodeCheck("positive volume", result, false);
      result.volume = -0.01;
      Py000RetcodeCheck("negative volume", result, false);
      result.volume = invalid_volume;
      Py000RetcodeCheck("NaN volume", result, false);
      result.volume = infinite_volume;
      Py000RetcodeCheck("infinite volume", result, false);
      result.volume = -0.0;
      Py000RetcodeCheck("negative zero volume", result, true);
      result.volume = 0.0;
      result.retcode_external = 1;
      Py000RetcodeCheck("unknown positive external code", result, false);
      result.retcode_external = -1;
      Py000RetcodeCheck("unknown negative external code", result, false);
   }
   uint unproven[] = {
      0, 1, 10001, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL,
      TRADE_RETCODE_TIMEOUT, TRADE_RETCODE_CONNECTION, TRADE_RETCODE_PLACED,
      TRADE_RETCODE_ERROR, TRADE_RETCODE_CANCEL, TRADE_RETCODE_INVALID,
      TRADE_RETCODE_TOO_MANY_REQUESTS, 0xFFFFFFFF
   };
   for(int index = 0; index < ArraySize(unproven); index++)
   {
      MqlTradeResult result = {};
      result.retcode = unproven[index];
      Py000RetcodeCheck("not a proven rejection", result, false);
   }
   PrintFormat("PY000_RETCODE_TEST checks=%d failures=%d",
      g_retcode_checks, g_retcode_failures);
   return g_retcode_failures == 0 ? 0 : 1;
}
