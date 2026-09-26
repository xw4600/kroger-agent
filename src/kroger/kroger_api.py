"""Kroger API client.

Thin wrapper over the public Kroger API. OAuth/refresh is delegated to
kroger_cart (PKCE + token storage), but the product *search* path is ours: our
database is keyed by UPC, so we normally skip search entirely and add exact
products, which avoids the "first search result wins" ambiguity entirely.

Hard limit, not an oversight: the public API can ADD to a cart. It cannot read
the cart, remove items, or check out. Cart retrieval is Partner-API only.
Checkout is always done by a human on kroger.com.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests
from kroger_cart.api import (
    add_to_cart_batch,
    extract_product_info,
    find_location,
    get_headers,
    search_product,
)
from kroger_cart.auth import TokenManager
from kroger_cart.session import create_session

from .config import Config

logger = logging.getLogger(__name__)

MODALITIES = ("DELIVERY", "PICKUP")


class KrogerAuthError(RuntimeError):
    pass


class KrogerAPIError(RuntimeError):
    pass


@dataclass
class Product:
    upc: str
    name: str
    brand: str = ""
    price: float | None = None
    promo_price: float | None = None
    national_price: float | None = None
    on_sale: bool = False
    in_stock: bool = True
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def effective_price(self) -> float | None:
        return self.promo_price if self.on_sale and self.promo_price else self.price

    def as_dict(self) -> dict:
        return {
            "upc": self.upc,
            "name": self.name,
            "brand": self.brand,
            "price": self.price,
            "promo_price": self.promo_price,
            "effective_price": self.effective_price,
            "on_sale": self.on_sale,
            "in_stock": self.in_stock,
        }


def get_session() -> requests.Session:
    return create_session()


def get_access_token(cfg: Config) -> str:
    """Return a valid access token, running the browser flow if needed."""
    if not cfg.configured:
        raise KrogerAuthError(
            "No Kroger credentials. Copy .env.example to data/config.env and fill in "
            "KROGER_CLIENT_ID / KROGER_CLIENT_SECRET, then run: kroger auth login"
        )
    tm = TokenManager(cfg.token_manager_config(), get_session())
    try:
        return tm.get_access_token()
    except Exception as exc:  # noqa: BLE001 - surface a usable message
        raise KrogerAuthError(f"OAuth failed: {exc}") from exc


def resolve_location(cfg: Config, session: requests.Session, token: str) -> str:
    """Look up the locationId for the configured zip code."""
    if cfg.location_id:
        return cfg.location_id
    if not cfg.zip_code:
        raise KrogerAPIError(
            "No store configured. Run: kroger store set --zip <your-zip>"
        )
    location_id = find_location(session, token, cfg.api_base, cfg.zip_code, cfg.chain)
    return str(location_id)


def search(
    cfg: Config,
    session: requests.Session,
    token: str,
    term: str,
    location_id: str,
    limit: int = 5,
) -> list[Product]:
    """Free-text catalog search. Used for name -> UPC resolution."""
    results = search_product(session, token, cfg.api_base, term, location_id)
    out = []
    for raw in results[:limit]:
        info = extract_product_info(raw)
        out.append(Product(upc=info["upc"], name=info["name"], brand=info.get("brand", ""), raw=raw))
    return out


def product_by_upc(
    cfg: Config,
    session: requests.Session,
    token: str,
    upc: str,
    location_id: str | None = None,
) -> Product | None:
    """Exact lookup by 13-digit productId.

    This is the authoritative way to read a product's current price -- filter.term
    is a fuzzy match and can return a different product with a similar name.
    Requires a locationId or no price data comes back.
    """
    params = {"filter.productId": upc, "filter.limit": 1}
    if location_id:
        params["filter.locationId"] = location_id

    resp = session.get(
        f"{cfg.api_base}/products", headers=get_headers(token), params=params
    )
    if resp.status_code == 401:
        raise KrogerAuthError("Token rejected (401). Run: kroger auth login")
    resp.raise_for_status()

    data = resp.json().get("data") or []
    if not data:
        return None
    info = extract_product_info(data[0])
    return Product(
        upc=info["upc"],
        name=info["name"],
        brand=info.get("brand", ""),
        price=info.get("price"),
        promo_price=info.get("promo_price"),
        national_price=info.get("national_price"),
        on_sale=info.get("on_sale", False),
        in_stock=info.get("in_stock", True),
        raw=data[0],
    )


def add_items(
    cfg: Config,
    session: requests.Session,
    token: str,
    items: list[dict],
    modality: str = "DELIVERY",
) -> dict:
    """Add items to the cart in one API call.

    items: [{"upc": "0001111041700", "quantity": 2}, ...]

    DELIVERY and PICKUP are separate carts -- adding to one does not touch the
    other, so the modality has to match the order you intend to place.
    """
    modality = modality.upper()
    if modality not in MODALITIES:
        raise KrogerAPIError(f"modality must be one of {MODALITIES}, got {modality!r}")

    clean = [{"upc": str(i["upc"]), "quantity": int(i.get("quantity", 1))} for i in items]
    if not clean:
        return {"status": 0, "items": 0}

    logger.info("Adding %d item(s) to %s cart", len(clean), modality)
    result = add_to_cart_batch(session, token, cfg.api_base, clean, modality)
    return {"status": result.get("status"), "items": len(clean), "modality": modality}
