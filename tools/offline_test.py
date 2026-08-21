"""Ag ve Helius anahtari gerektirmeyen offline test.

httpx stub'lanir, RPC sahte cevaplarla beslenir; bonding curve cozumleme,
launch penceresi bolme, kural motoru ve paper slipaj modeli dogrulanir.
Calistirma:  python tools/offline_test.py
"""
import asyncio, base64, struct, sys, types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
HERE = Path(__file__).resolve().parent

# --- httpx stub: ag yok ---
_hx = types.ModuleType("httpx")
class _StubClient:
    def __init__(self, *a, **k): self.is_closed = False
    async def aclose(self): self.is_closed = True
_hx.AsyncClient = _StubClient
_hx.Timeout = lambda *a, **k: None
sys.modules["httpx"] = _hx

import state, rpc, pumpfun, analyzer

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    if not cond: fails.append(name)


# ---------- 1) bonding curve decode ----------
DEC = 6
vtok, vsol, rtok, rsol, supply = 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 0, 1_000_000_000_000_000
raw = b"\x00"*8 + struct.pack("<QQQQQ", vtok, vsol, rtok, rsol, supply) + b"\x00"
c = pumpfun._decode("addr", base64.b64encode(raw).decode(), DEC)
check("curve decode", c is not None)
if c:
    check("curve price", abs(c.price_sol - 30/1_073_000_000) < 1e-15, c.price_sol)
    check("curve mcap SOL ~27.96", 27.0 < c.market_cap_sol() < 29.0, c.market_cap_sol())
    check("curve sol_in_curve 0", c.sol_in_curve == 0.0)

# curve after ~12 SOL of buys
raw2 = b"\x00"*8 + struct.pack("<QQQQQ", 900_000_000_000_000, 42_000_000_000,
                               700_000_000_000_000, 12_000_000_000, supply) + b"\x00"
c2 = pumpfun._decode("addr", base64.b64encode(raw2).decode(), DEC)
check("curve2 sol_in_curve 12", c2 and abs(c2.sol_in_curve - 12.0) < 1e-9)
check("curve2 mcap > curve1", c2 and c2.market_cap_sol() > c.market_cap_sol())

# garbage must be rejected
junk = b"\x01"*80
check("curve garbage reddedildi", pumpfun._decode("a", base64.b64encode(junk).decode(), 6) is None)

# ---------- 2) launch penceresi bolme ----------
L = 1_700_000_000
sigs = [{"signature":"L","slot":100,"blockTime":L}]
sigs += [{"signature":"b%d"%i,"slot":100,"blockTime":L} for i in range(3)]      # ayni slot = bundle
sigs += [{"signature":"s%d"%i,"slot":101,"blockTime":L+5} for i in range(4)]    # 5sn sonra = sniper
sigs += [{"signature":"x%d"%i,"slot":200,"blockTime":L+60} for i in range(5)]   # pencere disi
sigs += [{"signature":"nb","slot":300,"blockTime":None}]                        # blockTime yok
b, s, lt, tr = analyzer._split_launch_window(sigs)
check("bundle sayisi 4", len(b) == 4, len(b))
check("sniper sayisi 4", len(s) == 4, len(s))
check("truncated degil", tr is False)
check("blockTime=None pencereye girmedi", all(x["signature"] != "nb" for x in b+s))
check("launch_time", lt == L)

# cok yogun pencere -> truncated, veri yok degil
busy = [{"signature":"L","slot":100,"blockTime":L}]
busy += [{"signature":"s%d"%i,"slot":101,"blockTime":L+3} for i in range(500)]
b2, s2, _, tr2 = analyzer._split_launch_window(busy)
check("yogun pencere truncated", tr2 is True)
check("yogun pencere butcesi", len(b2)+len(s2) == analyzer.MAX_EARLY_TX, len(b2)+len(s2))

# ---------- 3) evaluate ----------
cfg = state.Settings()
M = analyzer.Metrics
good = M(mint="m", name="ok", price_usd=0.00002, market_cap=12000, bundler=1.0, sniper=2.0,
         dev=1.0, top10=12.0, lp_burned=True, curve_sol=6.0, curve_complete=False,
         total_supply=1e9, supply=2e8)
r = analyzer.evaluate(good, cfg)
check("saglikli coin PASS", r.passed, r.reason_text)

no_dex = M(mint="m", price_usd=0.00002, market_cap=12000, bundler=1.0, sniper=2.0, dev=1.0,
           top10=12.0, lp_burned=True, curve_sol=6.0, curve_complete=False, volume_5m=None)
check("dexscreener yokken PASS", analyzer.evaluate(no_dex, cfg).passed,
      analyzer.evaluate(no_dex, cfg).reason_text)

thin = M(mint="m", price_usd=1e-5, market_cap=6000, bundler=1.0, sniper=1.0, dev=1.0,
         top10=12.0, lp_burned=True, curve_sol=0.2, curve_complete=False)
rr = analyzer.evaluate(thin, cfg)
check("talep yok -> RED", not rr.passed and any("curve" in x for x in rr.reasons), rr.reason_text)

busyc = M(mint="m", price_usd=1e-5, market_cap=12000, bundler=1.0, sniper=2.0, dev=1.0,
          top10=12.0, lp_burned=True, curve_sol=6.0, curve_complete=False, sniper_truncated=True)
rr = analyzer.evaluate(busyc, cfg)
# e710398: yogun launch penceresi artik KOSULSUZ red degil - runner'in imzasi
# oldugu icin varsayilan kapali (reject_on_busy_launch=False), yalnizca olculur.
check("yogun launch varsayilan olarak elemez", rr.passed, rr.reason_text)
cfg_busy = state.Settings(reject_on_busy_launch=True)
rb = analyzer.evaluate(busyc, cfg_busy)
check("yogun launch -> RED (kural acikken)",
      not rb.passed and any("yogun" in x for x in rb.reasons), rb.reason_text)

missing = M(mint="m")
rr = analyzer.evaluate(missing, cfg)
check("veri yoksa RED", not rr.passed and len(rr.reasons) >= 5, rr.reason_text)

# eski esiklerin eledigi gercek olcumler artik gecmeli
chevy = M(mint="m", price_usd=1e-5, market_cap=7030, bundler=2.0, sniper=3.0, dev=2.1,
          top10=12.0, lp_burned=True, curve_sol=4.0, curve_complete=False)
check("ChevyNova (dev 2.1, top10 12) PASS", analyzer.evaluate(chevy, cfg).passed,
      analyzer.evaluate(chevy, cfg).reason_text)

newbie = M(mint="m", price_usd=1e-5, market_cap=7_500_000, bundler=100.0, sniper=0.0, dev=80.4,
           top10=100.0, lp_burned=True, curve_sol=40.0, curve_complete=True, volume_5m=95000)
check("Newbie (dev %80, mcap 7.5M) hala RED", not analyzer.evaluate(newbie, cfg).passed)

# ---------- 4) settings kalibrasyonu ----------
check("analyze_delay >= sniper penceresi", cfg.analyze_delay >= analyzer.SNIPER_WINDOW_SEC,
      cfg.analyze_delay)
check("max_top10 toplam arza gore makul", 10 <= cfg.max_top10 <= 40, cfg.max_top10)
check("min_curve_sol var", hasattr(cfg, "min_curve_sol"))
check("paper_slippage_pct var", cfg.paper_slippage_pct > 0)

# ---------- 5) rpc imza sayfasi ----------
check("SIG_PAGE_SIZE 1000", analyzer.SIG_PAGE_SIZE == rpc.SIG_MAX_LIMIT == 1000)

captured = {}
async def fake_call(method, params, retries=2):
    captured["opts"] = params[1]
    return []
rpc.call = fake_call
asyncio.run(rpc.get_signatures("mint", limit=5000))
check("limit 1000'e kirpiliyor", captured["opts"]["limit"] == 1000, captured["opts"])

print("\n%d/%d gecti" % (0, len(fails)) if fails else "\nTUM TESTLER GECTI")
if fails: print("BASARISIZ:", fails); sys.exit(1)

# ========== uctan uca: sahte RPC ==========
MINT="TESTmintpump"; CURVE="CURVEaddr"; DEV="DEVwallet"; SNIPER="SNIPERwallet"
L=1_700_000_000; SLOT=100
SUPPLY_RAW=1_000_000_000_000_000  # 1e9 token, 6 decimals
SUPPLY_UI=1_000_000_000.0

def tx(slot, bt, balances, signer=DEV):
    return {"slot":slot,"blockTime":bt,
            "transaction":{"message":{"accountKeys":[{"pubkey":signer,"signer":True,"writable":True}]}},
            "meta":{"err":None,"preTokenBalances":[],
                    "postTokenBalances":[{"mint":MINT,"owner":o,"uiTokenAmount":{"uiAmount":a}}
                                         for o,a in balances]}}

# launch tx: curve tum arzi alir + dev ayni islemde 20M token alir (=%2)
TXS={
 "L":  tx(SLOT, L, [(CURVE, 793_100_000.0), (DEV, 20_000_000.0)]),
 "s1": tx(SLOT+1, L+4, [(SNIPER, 10_000_000.0)], signer=SNIPER),
 "s2": tx(SLOT+2, L+90, [(SNIPER, 50_000_000.0)], signer=SNIPER),  # pencere disi
}
CURVE_RAW = b"\x00"*8 + struct.pack("<QQQQQ", 900_000_000_000_000, 42_000_000_000,
                                    700_000_000_000_000, 12_000_000_000, SUPPLY_RAW) + b"\x00"

async def fake_get_transaction(sig): return TXS.get(sig)
async def fake_get_signatures(address, limit=1000, before=None, until=None):
    return [{"signature":"s2","slot":SLOT+2,"blockTime":L+90,"err":None},
            {"signature":"s1","slot":SLOT+1,"blockTime":L+4,"err":None}]
async def fake_get_token_supply(m): return {"uiAmount":SUPPLY_UI,"decimals":6}
async def fake_largest(m): return [{"address":"ataC"},{"address":"ataD"},{"address":"ataS"}]
async def fake_multi(keys):
    m={"ataC":{"data":{"parsed":{"info":{"owner":CURVE,"tokenAmount":{"uiAmount":793_100_000.0}}}}},
       "ataD":{"data":{"parsed":{"info":{"owner":DEV,"tokenAmount":{"uiAmount":20_000_000.0}}}}},
       "ataS":{"data":{"parsed":{"info":{"owner":SNIPER,"tokenAmount":{"uiAmount":10_000_000.0}}}}},
       CURVE:{"owner":"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"},
       DEV:None, SNIPER:None}
    return [m.get(k) for k in keys]
async def fake_account_parsed(pk): return {"data":{"parsed":{"info":{"decimals":6,"freezeAuthority":None}}}}
async def fake_account_raw(pk):
    if pk!=CURVE: return None
    return {"owner":state.PUMP_FUN_PROGRAM,"data":[base64.b64encode(CURVE_RAW).decode(),"base64"]}
async def fake_owner_balance(o,m): return 0.0
async def fake_asset(m): return {"name":"Test Coin","symbol":"TEST"}

rpc.get_transaction=fake_get_transaction; rpc.get_signatures=fake_get_signatures
rpc.get_token_supply=fake_get_token_supply; rpc.get_token_largest_accounts=fake_largest
rpc.get_multiple_accounts_parsed=fake_multi; rpc.get_account_parsed=fake_account_parsed
rpc.get_account_raw=fake_account_raw; rpc.get_token_balance_of_owner=fake_owner_balance
rpc.get_asset_metadata=fake_asset
async def no_dex(mint, retries=3): return None
analyzer.fetch_dexscreener=no_dex
analyzer._sol_price.update({"price":160.0,"ts":9e18})

m = asyncio.run(analyzer.collect_metrics(MINT, creator=DEV, launch_signature="L"))
print(" ->", {k:getattr(m,k) for k in ("price_usd","market_cap","curve_sol","bundler","sniper","dev","top10","lp_burned","source")})
check("dexscreener yokken fiyat var", m.price_usd and m.price_usd>0, m.price_usd)
check("kaynak bonding-curve", m.source=="bonding-curve")
check("mcap curve'den", m.market_cap and 6000 < m.market_cap < 9000, m.market_cap)
check("curve_sol 12", abs((m.curve_sol or 0)-12.0)<1e-9, m.curve_sol)
check("bundler = dev alimi %2 (curve haric)", abs((m.bundler or 0)-2.0)<0.01, m.bundler)
check("sniper = %1 (15sn ici)", abs((m.sniper or 0)-1.0)<0.01, m.sniper)
check("dev = toplam arzin %2'si", abs((m.dev or 0)-2.0)<0.01, m.dev)
check("top10 toplam arza gore (30M/1e9)", m.top10 and 2.9 < m.top10 < 3.1, m.top10)
check("isim DAS metadata'dan geldi", m.name == "Test Coin", m.name)
check("lp_burned true", m.lp_burned is True)
check("truncated degil", m.sniper_truncated is False)
r = analyzer.evaluate(m, state.Settings())
check("bu coin PASS", r.passed, r.reason_text)

# ---- paper slipaj ----
import database as db
db.DB_PATH = HERE / "offline_test.db"
if db.DB_PATH.exists(): db.DB_PATH.unlink()
import trader
db.init_db()
state.settings.paper_trading=True
state.settings.paper_balance_sol=1.0; state.settings.paper_funded_sol=1.0
# Alim buyuklugu fiyat etkisine giriyor; yerel settings.json'dan gelmesin,
# yoksa panelden auto_buy_sol degistirmek bu testi kiriyor.
state.settings.auto_buy_sol=0.01
state.settings.save=lambda: None
async def fake_sol(): return 160.0
trader.sol_price_usd=fake_sol; analyzer.sol_price_usd=fake_sol

async def run_paper():
    pid = await trader.buy(MINT, "test", price_hint=0.00001, liquidity_hint=5000.0)
    pos = db.get_position(pid)
    return pid, pos
pid, pos = asyncio.run(run_paper())
check("paper alim acildi", pid is not None)
check("dolum fiyati kotu yonde", pos["entry_price"] > 0.00001, pos["entry_price"])
impact = 0.00001*(1+ (state.settings.paper_slippage_pct/100
                     + (state.settings.auto_buy_sol*160/5000)))
check("slipaj = taban + etki", abs(pos["entry_price"]-impact) < 1e-12, (pos["entry_price"], impact))

async def run_sell():
    async def price(m): return 0.00002
    analyzer.current_price = price
    return await trader.sell(pid, reason="test")
ok = asyncio.run(run_sell())
tr = db.get_trades(1)[0]
check("paper satis islendi", ok and tr["exit_price"] < 0.00002, tr["exit_price"])
check("paper satis kari pozitif", tr["pnl_sol"] > 0, tr["pnl_sol"])

print("\nTUM TESTLER GECTI" if not fails else "\nBASARISIZ: %s" % fails)
sys.exit(1 if fails else 0)
