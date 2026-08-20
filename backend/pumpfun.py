"""pump.fun bonding-curve reader.

Dexscreener needs 30-90 seconds to index a brand new mint, so for the coins this
bot actually cares about (age < 1 minute) `price`, `marketCap` and `liquidity`
come back empty and every coin gets rejected for "missing data". The bonding
curve itself carries all three from the very first slot, so it is read directly.

Account layout (Anchor, pump.fun program):

    0..8    discriminator
    8..16   virtual_token_reserves  u64
    16..24  virtual_sol_reserves    u64
    24..32  real_token_reserves     u64
    32..40  real_sol_reserves       u64
    40..48  token_total_supply      u64
    48      complete                bool

Every field is sanity-checked before use; if anything looks off the reader
returns None and the caller falls back to Dexscreener.
"""
from __future__ import annotations

import base64
import logging
import struct
from dataclasses import dataclass
from typing import Any, Dict, Optional

import rpc
import state

log = logging.getLogger("market-dominanter")

CURVE_SEED = b"bonding-curve"
_curve_cache: Dict[str, Optional[str]] = {}

try:  # solders ships with the project but the reader must survive without it
    from solders.pubkey import Pubkey  # type: ignore
    SOLDERS_OK = True
except Exception:  # pragma: no cover - optional dependency
    Pubkey = None  # type: ignore
    SOLDERS_OK = False


@dataclass
class Curve:
    address: str
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    decimals: int = 6

    @property
    def sol_in_curve(self) -> float:
        """Real SOL paid into the curve so far = the launch's actual buy volume."""
        return self.real_sol_reserves / state.LAMPORTS_PER_SOL

    @property
    def price_sol(self) -> Optional[float]:
        """Price of one token in SOL, from the virtual reserves."""
        tokens = self.virtual_token_reserves / (10 ** self.decimals)
        if tokens <= 0:
            return None
        return (self.virtual_sol_reserves / state.LAMPORTS_PER_SOL) / tokens

    @property
    def supply_ui(self) -> float:
        return self.token_total_supply / (10 ** self.decimals)

    def market_cap_sol(self) -> Optional[float]:
        price = self.price_sol
        if price is None:
            return None
        return price * self.supply_ui


def curve_address(mint: str) -> Optional[str]:
    """PDA of the bonding curve: ["bonding-curve", mint] under the pump.fun program."""
    if not SOLDERS_OK:
        return None
    try:
        pda, _ = Pubkey.find_program_address(
            [CURVE_SEED, bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(state.PUMP_FUN_PROGRAM),
        )
        return str(pda)
    except Exception as exc:
        log.debug("Bonding curve PDA hesaplanamadi (%s): %s", mint, exc)
        return None


def curve_from_launch_tx(tx: Dict[str, Any], mint: str) -> Optional[str]:
    """Bonding curve address taken straight out of the create transaction.

    The curve's associated token account is credited with the whole supply in the
    create tx, and `postTokenBalances[].owner` is the curve PDA - no key
    derivation (and no solders) required.

    Picks the LARGEST balance, not the first one: a create-with-buy transaction
    also credits the dev's own account, and if that entry happens to come first
    the curve is never found (price, mcap and curve SOL all stay empty).
    """
    meta = (tx or {}).get("meta") or {}
    best_owner, best_amount = None, -1.0
    for bal in meta.get("postTokenBalances") or []:
        if bal.get("mint") != mint:
            continue
        owner = bal.get("owner")
        if not owner or owner == state.PUMP_FUN_PROGRAM:
            continue
        try:
            amount = float((bal.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount > best_amount:
            best_owner, best_amount = owner, amount
    return best_owner


# Bounds a genuine curve always sits inside. They exist to reject an account
# that merely happens to be 49+ bytes long, so they are wide but not toothless.
VSOL_MIN = 20 * state.LAMPORTS_PER_SOL      # curve is seeded with ~30 virtual SOL
VSOL_MAX = 1000 * state.LAMPORTS_PER_SOL    # it migrates long before this
VTOK_MIN = 10 ** 11
VTOK_MAX = 2 * 10 ** 15


def _plausible(c: Curve) -> bool:
    """Guards against decoding a differently-shaped account as a curve."""
    if min(c.virtual_token_reserves, c.virtual_sol_reserves, c.token_total_supply) <= 0:
        return False
    if c.real_token_reserves < 0 or c.real_sol_reserves < 0:
        return False
    if not (VSOL_MIN <= c.virtual_sol_reserves <= VSOL_MAX):
        return False
    if not (VTOK_MIN <= c.virtual_token_reserves <= VTOK_MAX):
        return False
    # The virtual reserves always sit above the real ones by the seeded offset.
    if c.real_sol_reserves >= c.virtual_sol_reserves:
        return False
    if c.real_token_reserves >= c.virtual_token_reserves:
        return False
    # pump.fun mints are 1e9 tokens at 6 decimals; allow a wide band anyway.
    if not (10 ** 9 <= c.token_total_supply <= 10 ** 18):
        return False
    if c.real_token_reserves > c.token_total_supply:
        return False
    price = c.price_sol
    return price is not None and 0 < price < 1.0


def _decode(address: str, data_b64: str, decimals: int) -> Optional[Curve]:
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    if len(raw) < 49:
        return None
    if raw[48] not in (0, 1):  # `complete` is a bool: anything else is not a curve
        return None
    vtok, vsol, rtok, rsol, supply = struct.unpack_from("<QQQQQ", raw, 8)
    curve = Curve(address=address, virtual_token_reserves=vtok, virtual_sol_reserves=vsol,
                  real_token_reserves=rtok, real_sol_reserves=rsol, token_total_supply=supply,
                  complete=bool(raw[48]), decimals=decimals)
    return curve if _plausible(curve) else None


async def _try_address(address: str, decimals: int) -> Optional[Curve]:
    account = await rpc.get_account_raw(address)
    if not account or account.get("owner") != state.PUMP_FUN_PROGRAM:
        return None
    data = account.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, str):
        return None
    return _decode(address, data, decimals)


def _remember(mint: str, address: str) -> None:
    if len(_curve_cache) > 2000:
        _curve_cache.clear()
    _curve_cache[mint] = address


async def read_curve(mint: str, launch_tx: Optional[Dict[str, Any]] = None,
                     decimals: int = 6) -> Optional[Curve]:
    """Fetch and decode the bonding curve for a mint, or None if unavailable.

    Only an address that actually decoded is cached; caching a guess that turned
    out to be wrong would keep the coin's price empty for its whole lifetime.
    """
    cached = _curve_cache.get(mint)
    candidates = []
    if cached:
        candidates.append(cached)
    if launch_tx:
        candidates.append(curve_from_launch_tx(launch_tx, mint))
    candidates.append(curve_address(mint))

    seen = set()
    for address in candidates:
        if not address or address in seen:
            continue
        seen.add(address)
        curve = await _try_address(address, decimals)
        if curve:
            _remember(mint, address)
            return curve
    return None
