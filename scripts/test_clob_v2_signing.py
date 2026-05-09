#!/usr/bin/env python3
"""
Standalone validator: does py-clob-client-v2 1.0.0 actually work for our
Gnosis Safe / Polymarket Proxy account post CLOB v2 cutover (2026-04-28)?

This script does NOT modify any project code or chain state. It instantiates
a fresh v2 ClobClient with the same wallet credentials used by the running
system, attempts a single tiny order at an extreme price, and on success
prints the placed order id (does NOT auto-cancel; user cancels manually).

The server validates signatures BEFORE checking balance, so a 0-pUSD wallet
still gets clean diagnostic output:
  - signature wrong → 400 order_version_mismatch / invalid_signature
  - signature right → 400 not_enough_balance / similar (= signing OK)

Usage:
    POLYMARKET_PRIVATE_KEY=0x... python3 scripts/test_clob_v2_signing.py <token_id>

Verdicts:
    OK                      v2 signs correctly + order placed; safe to migrate.
    NOT_ENOUGH_BALANCE      v2 signs correctly; next blocker is on-chain wrap+approve.
    ORDER_VERSION_MISMATCH  v2 SDK still mis-signs for Safe; v2 issue #32 confirmed.
    INVALID_SIGNATURE       Different SDK bug; capture full error and report upstream.
    OTHER                   See traceback; likely SDK API drift or a new venue check.

Install (one-time, before first run):
    PATH="$HOME/.cargo/bin:$PATH" \\
      uv pip install py-clob-client-v2 \\
      --python /Users/miller/nautilus_trader/.venv/bin/python
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("token_id", help="Polymarket token_id (asset_id) to place test order against")
    parser.add_argument("--price", type=float, default=0.01,
                        help="Extreme limit price (default 0.01, will not fill)")
    parser.add_argument("--size", type=float, default=5.0, help="Order size in shares (default 5)")
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--neg-risk", action="store_true",
                        help="Set if the market is a neg-risk market (changes the exchange contract)")
    parser.add_argument("--config",
                        default="src/arbitrage/services/web_gateway/default_config.json",
                        help="Path to config JSON for API creds, funder, EOA")
    args = parser.parse_args()

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        print("ERROR: POLYMARKET_PRIVATE_KEY env var not set (use the EOA private key, not the proxy address).",
              file=sys.stderr)
        return 1

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"ERROR: config file not found: {cfg_path}", file=sys.stderr)
        return 1
    raw = json.loads(cfg_path.read_text())
    required = ["polymarket_clob_url", "polymarket_funder",
                "polymarket_clob_api_key", "polymarket_clob_api_secret", "polymarket_clob_passphrase"]

    def find_section(d):
        if isinstance(d, dict):
            if all(k in d for k in required):
                return d
            for v in d.values():
                found = find_section(v)
                if found is not None:
                    return found
        return None

    cfg = find_section(raw)
    if cfg is None:
        print(f"ERROR: could not locate polymarket creds in config; required keys: {required}",
              file=sys.stderr)
        return 1

    try:
        from py_clob_client_v2 import (
            ClobClient,
            OrderArgs,
            ApiCreds,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )
    except ImportError as e:
        print("ERROR: py-clob-client-v2 not installed in this interpreter.\n"
              "Install with:\n"
              '  PATH="$HOME/.cargo/bin:$PATH" \\\n'
              "    uv pip install py-clob-client-v2 \\\n"
              "    --python /Users/miller/nautilus_trader/.venv/bin/python\n"
              f"\n(import error: {e})", file=sys.stderr)
        return 1

    try:
        from py_clob_client_v2.exceptions import PolyApiException
    except ImportError:
        PolyApiException = Exception  # fallback

    print("--- v2 SDK signing validation ---")
    print(f"funder (proxy):  {cfg['polymarket_funder']}")
    print(f"signer (EOA):    {cfg.get('polymarket_eoa_address', '<not in config>')}")
    print(f"sig_type:        2 (Gnosis Safe / Polymarket Proxy)")
    print(f"token_id:        {args.token_id}")
    print(f"neg_risk:        {args.neg_risk}")
    print(f"order:           {args.side} {args.size} @ {args.price}")
    print()

    creds = ApiCreds(
        api_key=cfg["polymarket_clob_api_key"],
        api_secret=cfg["polymarket_clob_api_secret"],
        api_passphrase=cfg["polymarket_clob_passphrase"],
    )

    client = ClobClient(
        host=cfg["polymarket_clob_url"],
        chain_id=137,
        key=pk,
        creds=creds,
        signature_type=2,
        funder=cfg["polymarket_funder"],
    )
    print("[1/3] ClobClient v2 instantiated.")

    side_const = Side.BUY if args.side == "BUY" else Side.SELL
    order_args = OrderArgs(
        token_id=args.token_id,
        price=args.price,
        size=args.size,
        side=side_const,
    )
    options = PartialCreateOrderOptions(neg_risk=args.neg_risk)

    try:
        signed = client.create_order(order_args, options=options)
        sig_preview = getattr(signed, "signature", "<no signature attr>")
        salt_preview = getattr(signed, "salt", "<n/a>")
        print(f"[2/3] Order signed locally. signature={str(sig_preview)[:32]}... salt={salt_preview}")
    except Exception as e:
        print(f"\nFAIL [signing]: {e!r}", file=sys.stderr)
        traceback.print_exc()
        print("\nVERDICT: OTHER (signing-side error)")
        return 5

    try:
        resp = client.post_order(signed, OrderType.GTC)
        print(f"[3/3] post_order response: {resp}")
        oid = None
        if isinstance(resp, dict):
            oid = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        if oid:
            print(f"\n*** Order placed: id={oid} ***")
            print("    NOT auto-cancelling. Cancel via polymarket.com or run cancel_order manually.")
        print("\nVERDICT: OK — v2 SDK signs correctly for our Gnosis Safe wallet.")
        return 0
    except PolyApiException as e:
        msg = str(e)
        print(f"\npost_order error: {msg}", file=sys.stderr)
        if "order_version_mismatch" in msg:
            print("\nVERDICT: ORDER_VERSION_MISMATCH — v2 SDK is still mis-signing for our setup. "
                  "Tracks v2 issue #32.")
            return 3
        if "invalid signature" in msg.lower() or "invalid_signature" in msg:
            print("\nVERDICT: INVALID_SIGNATURE — v2 SDK signature struct mismatch (different from version). "
                  "Capture full error and report upstream.")
            return 4
        if "not enough balance" in msg.lower() or "allowance" in msg.lower() or "insufficient" in msg.lower():
            print("\nVERDICT: NOT_ENOUGH_BALANCE — signing is OK; next blocker is on-chain "
                  "(USDC.e → pUSD wrap + approve v2 exchange).")
            return 0
        print("\nVERDICT: OTHER — see error above; may indicate SDK API drift or new venue check.")
        return 5
    except Exception as e:
        print(f"\nFAIL [post_order, unexpected]: {e!r}", file=sys.stderr)
        traceback.print_exc()
        print("\nVERDICT: OTHER")
        return 6


if __name__ == "__main__":
    sys.exit(main())
