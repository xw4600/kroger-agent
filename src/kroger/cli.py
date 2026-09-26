"""Command line interface.

Output convention: human-readable tables on a TTY, JSON when stdout is piped.
That way the agent gets structured output for free without a flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__, config, db
from .kroger_api import (
    KrogerAPIError,
    KrogerAuthError,
    add_items,
    get_access_token,
    get_session,
    product_by_upc,
    resolve_location,
)


def _is_tty() -> bool:
    return sys.stdout.isatty()


def emit(payload: Any, text: str | None = None) -> None:
    """Print JSON when piped, text when interactive."""
    if _is_tty() and text is not None:
        print(text)
    else:
        print(json.dumps(payload, indent=2, default=str))


def _load():
    cfg = config.load_config()
    return cfg, db.init()


# ─── doctor ──────────────────────────────────────────────────────────────────


def cmd_doctor(args) -> int:
    cfg, conn = _load()
    summary = db.db_summary(conn)
    checks: list[dict] = []

    checks.append(
        {
            "check": "home",
            "ok": True,
            "detail": str(config.home()),
        }
    )
    checks.append(
        {
            "check": "config.env",
            "ok": cfg.configured,
            "detail": (
                "loaded" if cfg.configured else f"missing -- see {config.config_env_path()}"
            ),
        }
    )
    checks.append(
        {
            "check": "environment",
            "ok": cfg.env == "PROD",
            "detail": f"{cfg.env} ({cfg.base_domain})"
            + ("" if cfg.env == "PROD" else "  <- sandbox, not your real account"),
        }
    )
    checks.append(
        {
            "check": "database",
            "ok": True,
            "detail": f"{summary['orders']} orders, {summary['items']} items, "
            f"{summary['items_unresolved_upc']} missing UPC",
        }
    )

    token_detail = "not checked"
    token_ok = False
    if cfg.configured:
        try:
            get_access_token(cfg)
            token_ok, token_detail = True, "valid"
        except KrogerAuthError as exc:
            token_detail = str(exc)
    else:
        token_detail = "no credentials configured"
    checks.append({"check": "oauth token", "ok": token_ok, "detail": token_detail})

    loc_detail = cfg.location_id or ("zip " + cfg.zip_code if cfg.zip_code else "unset")
    checks.append({"check": "store", "ok": bool(cfg.location_id or cfg.zip_code), "detail": loc_detail})

    if cfg.configured and token_ok:
        try:
            session = get_session()
            token = get_access_token(cfg)
            loc = resolve_location(cfg, session, token)
            checks.append({"check": "location lookup", "ok": True, "detail": f"locationId={loc}"})
        except (KrogerAPIError, KrogerAuthError) as exc:
            checks.append({"check": "location lookup", "ok": False, "detail": str(exc)})

    lines = [f"{'OK ' if c['ok'] else 'FAIL'}  {c['check']:<18} {c['detail']}" for c in checks]
    emit({"checks": checks, "summary": summary}, "\n".join(lines) + f"\n\nconfig: {summary}")
    return 0 if all(c["ok"] for c in checks) else 1


# ─── auth ────────────────────────────────────────────────────────────────────


def cmd_auth(args) -> int:
    cfg = config.load_config()
    if not cfg.configured:
        print(
            f"No credentials found. Copy .env.example to {config.config_env_path()}\n"
            "and fill in KROGER_CLIENT_ID and KROGER_CLIENT_SECRET.",
            file=sys.stderr,
        )
        return 2

    try:
        token = get_access_token(cfg)
    except KrogerAuthError as exc:
        emit({"ok": False, "error": str(exc)})
        return 1

    if args.command == "login":
        emit({"ok": True, "message": "authenticated"}, "Authentication complete. Token cached.")
    else:
        masked = f"{token[:8]}...{token[-4:]}" if len(token) > 16 else "***"
        emit(
            {"ok": True, "token_prefix": masked, "scopes": cfg.scopes},
            f"Token valid ({masked})\nScopes: {cfg.scopes}",
        )
    return 0


# ─── store ───────────────────────────────────────────────────────────────────


def cmd_store(args) -> int:
    cfg = config.load_config()
    if args.command == "set":
        if args.zip:
            cfg = config.load_config()
            import os

            os.environ["KROGER_ZIP"] = args.zip
        session = get_session()
        token = get_access_token(cfg)
        location_id = resolve_location(cfg, session, token)
        emit(
            {"location_id": location_id, "zip": args.zip},
            f"locationId={location_id}  (save KROGER_LOCATION_ID={location_id} to config.env)",
        )
        return 0

    emit(
        {
            "zip": cfg.zip_code,
            "chain": cfg.chain,
            "location_id": cfg.location_id,
            "cart_url": cfg.cart_url,
        },
        f"zip={cfg.zip_code or '-'}  chain={cfg.chain}  locationId={cfg.location_id or '-'}\n"
        f"cart: {cfg.cart_url}",
    )
    return 0


# ─── product ─────────────────────────────────────────────────────────────────


def cmd_product(args) -> int:
    cfg = config.load_config()
    session = get_session()
    token = get_access_token(cfg)
    location_id = resolve_location(cfg, session, token) if args.location_id is None else args.location_id

    products = []
    for upc in args.upc:
        p = product_by_upc(cfg, session, token, upc, location_id)
        products.append(p.as_dict() if p else {"upc": upc, "error": "not found"})

    lines = []
    for p in products:
        if "error" in p:
            lines.append(f"{p['upc']}  not found")
        else:
            price = f"${p['effective_price']}" if p.get("effective_price") else "no price"
            sale = "  ON SALE" if p.get("on_sale") else ""
            lines.append(f"{p['upc']}  {p['name'][:48]:<48}  {price}{sale}")
    emit(products, "\n".join(lines))
    return 0


# ─── cart ────────────────────────────────────────────────────────────────────


def cmd_cart(args) -> int:
    if args.command != "test":
        print("cart subcommands other than 'test' are not built yet", file=sys.stderr)
        return 2

    cfg = config.load_config()
    if not args.upc:
        print("cart test requires --upc", file=sys.stderr)
        return 2

    session = get_session()
    token = get_access_token(cfg)

    if not args.yes:
        print(
            f"This will add UPC {args.upc} (qty {args.quantity}) to your "
            f"{args.modality} cart. Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2

    result = add_items(
        cfg, session, token, [{"upc": args.upc, "quantity": args.quantity}], args.modality
    )
    text = (
        f"Added UPC {args.upc} x{args.quantity} to the {args.modality} cart.\n"
        f"Open {cfg.cart_url} and confirm it is there."
    )
    emit(result, text)
    return 0


# ─── db ──────────────────────────────────────────────────────────────────────


def cmd_db(args) -> int:
    _, conn = _load()
    summary = db.db_summary(conn)

    if args.command == "stats":
        lines = [
            f"db           {summary['path']}",
            f"schema       v{summary['schema_version']}",
            f"orders       {summary['orders']}",
            f"items        {summary['items']}",
            f"order_items  {summary['order_items']}",
            f"no upc yet   {summary['items_unresolved_upc']}",
            f"categories   {summary['categories']}",
            f"open proposals {summary['proposals_open']}",
            f"date range   {summary['first_order'] or '-'} .. {summary['last_order'] or '-'}",
        ]
        emit(summary, "\n".join(lines))
    return 0


# ─── parser ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kroger",
        description="Taste-aware Kroger ordering from a local purchase history.",
    )
    p.add_argument("--version", action="version", version=f"kroger {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, credentials, database, store").set_defaults(
        func=cmd_doctor
    )

    a = sub.add_parser("auth", help="manage Kroger OAuth").add_subparsers(
        dest="command", required=True
    )
    a.add_parser("login", help="run the browser consent flow").set_defaults(func=cmd_auth)
    a.add_parser("status", help="check the cached token").set_defaults(func=cmd_auth)

    s = sub.add_parser("store", help="default store").add_subparsers(
        dest="command", required=True
    )
    st = s.add_parser("set", help="resolve locationId from zip")
    st.add_argument("--zip", required=True)
    st.set_defaults(func=cmd_store)
    s.add_parser("show", help="show current store").set_defaults(func=cmd_store)

    pr = sub.add_parser("product", help="product lookups").add_subparsers(
        dest="command", required=True
    )
    pl = pr.add_parser("lookup", help="exact lookup by UPC (authoritative price)")
    pl.add_argument("--upc", action="append", required=True)
    pl.add_argument("--location-id", dest="location_id", default=None)
    pl.set_defaults(func=cmd_product)

    c = sub.add_parser("cart", help="cart operations").add_subparsers(
        dest="command", required=True
    )
    ct = c.add_parser("test", help="add a single item (Phase 0 gate)")
    ct.add_argument("--upc", required=True)
    ct.add_argument("--quantity", type=int, default=1)
    ct.add_argument("--modality", default="DELIVERY", choices=["DELIVERY", "PICKUP"])
    ct.add_argument("--yes", action="store_true")
    ct.set_defaults(func=cmd_cart)

    d = sub.add_parser("db", help="database utilities").add_subparsers(
        dest="command", required=True
    )
    d.add_parser("init", help="create/upgrade the database").set_defaults(func=cmd_db)
    d.add_parser("stats", help="summarise the database").set_defaults(func=cmd_db)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KrogerAuthError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 1
    except KrogerAPIError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
