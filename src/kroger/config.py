"""Paths, credentials, and runtime settings.

Everything mutable lives under KROGER_HOME (default ~/kroger-agent) so the whole
thing is one directory you can copy to another machine.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Scopes required for product lookup + cart add. cart.basic:write is what
# actually mutates your account, so it is the one to be careful about.
SCOPES = "product.compact cart.basic:write profile.compact"

CERT_DOMAINS = {
    "PROD": "api.kroger.com",
    "CERT": "api-ce.kroger.com",
}


def home() -> Path:
    return Path(os.environ.get("KROGER_HOME", Path.home() / "kroger-agent")).expanduser()


def data_dir() -> Path:
    return home() / "data"


def db_path() -> Path:
    return data_dir() / "kroger.db"


def exports_dir() -> Path:
    return home() / "exports"


def config_env_path() -> Path:
    return data_dir() / "config.env"


def token_path() -> Path:
    return data_dir() / "tokens.json"


def gmail_dir() -> Path:
    return data_dir() / "gmail"


def ensure_dirs() -> None:
    for d in (data_dir(), exports_dir(), gmail_dir()):
        d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Config:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:3000"
    env: str = "PROD"
    zip_code: str = ""
    chain: str = "Kroger"
    location_id: str = ""
    scopes: str = SCOPES

    @property
    def base_domain(self) -> str:
        return CERT_DOMAINS.get(self.env.upper(), CERT_DOMAINS["PROD"])

    @property
    def api_base(self) -> str:
        return f"https://{self.base_domain}/v1"

    @property
    def auth_url(self) -> str:
        return f"https://{self.base_domain}/v1/connect/oauth2/authorize"

    @property
    def token_url(self) -> str:
        return f"https://{self.base_domain}/v1/connect/oauth2/token"

    @property
    def cart_url(self) -> str:
        # Where the human finishes the job. Checkout is never automated.
        suffix = {
            "Kroger": "kroger.com",
            "Smiths": "smithsfoodanddrug.com",
            "Fred Meyer": "fredmeyer.com",
            "Ralphs": "ralphs.com",
            "King Soopers": "kingsoopers.com",
            "QFC": "qfc.com",
            "Dillons": "dillons.com",
            "Food 4 Less": "food4less.com",
        }.get(self.chain, "kroger.com")
        return f"https://www.{suffix}/cart"

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    def token_manager_config(self) -> dict:
        """Config dict shaped for kroger_cart.auth.TokenManager."""
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "auth_url": self.auth_url,
            "token_url": self.token_url,
            "api_base": self.api_base,
            "token_file": str(token_path()),
            "scopes": self.scopes,
        }


def load_config() -> Config:
    ensure_dirs()
    p = config_env_path()
    if p.exists():
        load_dotenv(p, override=False)

    return Config(
        client_id=os.environ.get("KROGER_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("KROGER_CLIENT_SECRET", "").strip(),
        redirect_uri=os.environ.get(
            "KROGER_REDIRECT_URI", "http://localhost:3000"
        ).strip(),
        env=os.environ.get("KROGER_ENV", "PROD").strip().upper(),
        zip_code=os.environ.get("KROGER_ZIP", "").strip(),
        chain=os.environ.get("KROGER_CHAIN", "Kroger").strip(),
        location_id=os.environ.get("KROGER_LOCATION_ID", "").strip(),
    )


def write_token_file_mode(path: Path) -> None:
    if path.exists() and not os.name == "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
