"""Thin async JSON-RPC client for the Solana / Helius endpoint."""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

import state

log = logging.getLogger("market-fucker")

_ids = itertools.count(1)
_client: Optional[httpx.AsyncClient] = None

# Helius' free plan allows ~10 requests/sec. Analysis is RPC-heavy (a busy launch
# costs >100 getTransaction calls), so without a global pacer the calls collide,
# come back 429, and every metric silently degrades to "no data" = coin rejected.
RPC_RPS = float(os.getenv("RPC_RPS", "9"))
stats = {"calls": 0, "rate_limited": 0, "failed": 0}


class _Pacer:
    """Spaces out every RPC call across the whole process."""

    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            start = max(now, self._next)
            self._next = start + self.interval
            delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)


_pacer = _Pacer(RPC_RPS)


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def call(method: str, params: List[Any], retries: int = 2) -> Any:
    """Returns the `result` field, or None on any failure (never raises)."""
    payload = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
    for attempt in range(retries + 1):
        try:
            await _pacer.wait()
            stats["calls"] += 1
            resp = await client().post(state.rpc_url(), json=payload)
            if resp.status_code == 429:
                stats["rate_limited"] += 1
                if stats["rate_limited"] % 25 == 1:
                    log.warning("RPC hiz limiti (429) - RPC_RPS dusurun veya plani yukseltin")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                log.debug("RPC %s hata: %s", method, body["error"])
                return None
            return body.get("result")
        except Exception as exc:
            if attempt >= retries:
                stats["failed"] += 1
                log.debug("RPC %s basarisiz: %s", method, exc)
                return None
            await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def get_balance_sol(pubkey: str) -> float:
    res = await call("getBalance", [pubkey, {"commitment": "confirmed"}])
    if isinstance(res, dict) and "value" in res:
        return float(res["value"]) / state.LAMPORTS_PER_SOL
    return 0.0


async def get_token_supply(mint: str) -> Optional[Dict[str, Any]]:
    res = await call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    if isinstance(res, dict):
        return res.get("value")
    return None


async def get_token_largest_accounts(mint: str) -> List[Dict[str, Any]]:
    res = await call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    if isinstance(res, dict) and isinstance(res.get("value"), list):
        return res["value"]
    return []


async def get_multiple_accounts_parsed(pubkeys: List[str]) -> List[Optional[Dict[str, Any]]]:
    if not pubkeys:
        return []
    res = await call("getMultipleAccounts", [pubkeys, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    if isinstance(res, dict) and isinstance(res.get("value"), list):
        return res["value"]
    return []


async def get_account_parsed(pubkey: str) -> Optional[Dict[str, Any]]:
    res = await call("getAccountInfo", [pubkey, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    if isinstance(res, dict):
        return res.get("value")
    return None


async def get_token_balance_of_owner(owner: str, mint: str) -> float:
    res = await call(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    total = 0.0
    if isinstance(res, dict):
        for acc in res.get("value", []) or []:
            try:
                info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
                total += float(info.get("uiAmount") or 0.0)
            except Exception:
                continue
    return total


async def get_signatures(address: str, limit: int = 100, before: Optional[str] = None,
                         until: Optional[str] = None) -> List[Dict[str, Any]]:
    opts: Dict[str, Any] = {"limit": limit}
    if before:
        opts["before"] = before
    if until:
        opts["until"] = until
    res = await call("getSignaturesForAddress", [address, opts])
    return res if isinstance(res, list) else []


async def get_transaction(signature: str) -> Optional[Dict[str, Any]]:
    res = await call(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                     "commitment": "confirmed"}],
    )
    return res if isinstance(res, dict) else None


async def get_latest_blockhash() -> Optional[str]:
    res = await call("getLatestBlockhash", [{"commitment": "confirmed"}])
    if isinstance(res, dict):
        return (res.get("value") or {}).get("blockhash")
    return None


async def send_raw_transaction(b64_tx: str) -> Optional[str]:
    return await call(
        "sendTransaction",
        [b64_tx, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
        retries=1,
    )


async def confirm_signature(signature: str, timeout: float = 60.0) -> bool:
    """Poll signature status until finalized/confirmed or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        res = await call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        if isinstance(res, dict):
            value = (res.get("value") or [None])[0]
            if value:
                if value.get("err"):
                    return False
                status = value.get("confirmationStatus")
                if status in ("confirmed", "finalized"):
                    return True
        await asyncio.sleep(2.0)
    return False
