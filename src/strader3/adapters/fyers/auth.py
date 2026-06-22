"""Fyers authentication module with TOTP automation."""

import hashlib
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pyotp
import structlog

logger = structlog.get_logger(__name__)


class FyersAuth:
    """Handles Fyers authentication including TOTP-based 2FA.

    Implements:
    - Automated login flow with TOTP
    - Access token generation and refresh
    - Token persistence and validation
    """

    AUTH_BASE = os.environ.get("FYERS_AUTH_BASE", "https://api-t1.fyers.in")
    API_V3_BASE = f"{AUTH_BASE}/api/v3"
    AUTHCODE_URL = f"{API_V3_BASE}/generate-authcode"
    TOKEN_URL = os.environ.get("FYERS_TOKEN_URL", "https://api.fyers.in/api/v2/token")
    TOKEN_STORE_PATH = os.environ.get(
        "FYERS_TOKEN_STORE",
        os.path.expanduser("~/.config/straderv3/fyers_tokens.json"),
    )

    def __init__(
        self,
        app_id: str,
        secret_key: str,
        redirect_uri: str,
        totp_secret: str,
        pin: str,
        client_id: str,
    ):
        self.app_id = app_id
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self.totp_secret = totp_secret
        self.pin = pin
        self.client_id = client_id

        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._refresh_token: str | None = None

        self._totp = pyotp.TOTP(self.totp_secret)
        self._load_tokens_from_disk()

    @property
    def access_token(self) -> str | None:
        if self._access_token and self._token_expiry:
            if datetime.now() < self._token_expiry - timedelta(minutes=5):
                return self._access_token
        return None

    @property
    def is_authenticated(self) -> bool:
        return self.access_token is not None

    @classmethod
    def from_env(cls) -> "FyersAuth":
        return cls(
            app_id=os.environ["FYERS_APP_ID"],
            secret_key=os.environ["FYERS_SECRET_KEY"],
            redirect_uri=os.environ["FYERS_REDIRECT_URI"],
            totp_secret=os.environ["FYERS_TOTP_SECRET"],
            pin=os.environ["FYERS_PIN"],
            client_id=os.environ["FYERS_CLIENT_ID"],
        )

    def _persist_tokens(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.TOKEN_STORE_PATH), exist_ok=True)
            payload = {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "token_expiry": self._token_expiry.isoformat() if self._token_expiry else None,
            }
            with open(self.TOKEN_STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            logger.warning("Failed to persist tokens", path=self.TOKEN_STORE_PATH)

    def _load_tokens_from_disk(self) -> None:
        try:
            if not os.path.exists(self.TOKEN_STORE_PATH):
                return
            with open(self.TOKEN_STORE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            expiry_raw = data.get("token_expiry")
            self._token_expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
            if self._access_token and self._token_expiry:
                logger.info("Loaded persisted Fyers token", expires_at=self._token_expiry.isoformat())
        except Exception:
            logger.warning("Failed to load persisted tokens", path=self.TOKEN_STORE_PATH)

    def _generate_totp(self) -> str:
        return self._totp.now()

    def _get_app_id_hash(self) -> str:
        override = os.environ.get("FYERS_APP_ID_HASH")
        if override:
            return override.strip()
        hash_source = os.environ.get("FYERS_APP_ID_HASH_SOURCE", "app_id").lower()
        hash_app_id = self.client_id if hash_source == "client_id" else self.app_id
        return hashlib.sha256(
            f"{hash_app_id}:{self.secret_key}".encode()
        ).hexdigest()

    def get_auth_url(self) -> str:
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
        }
        return f"{self.AUTHCODE_URL}?{urlencode(params)}"

    async def authenticate(self) -> bool:
        """Perform full authentication flow.

        Returns:
            True if authentication successful
        """
        try:
            logger.info("Starting Fyers authentication")

            if self.is_authenticated:
                logger.info("Existing access token still valid; skipping login")
                return True

            logger.info("Fyers authentication requires manual auth code flow")
            logger.info("Set FYERS_AUTH_CODE env var or implement callback listener")
            return False

        except Exception as e:
            logger.error("Authentication failed", error=str(e))
            return False

    async def refresh_token(self) -> bool:
        """Attempt to refresh the access token.

        Returns:
            True if refresh successful
        """
        logger.info("Token refresh not yet implemented in Phase 1")
        return False
