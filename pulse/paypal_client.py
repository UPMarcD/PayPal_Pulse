"""Real PayPal Sandbox REST API Client (OAuth2, Balances, Transactions, Disputes, Invoices, Orders)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from pulse.config import PulseSettings, get_settings


class PayPalAuthResult(BaseModel):
    """Result of OAuth 2.0 Client Credentials authentication."""

    authenticated: bool
    mode: str = "sandbox"
    base_url: str = "https://api-m.sandbox.paypal.com"
    app_id: str = ""
    access_token_preview: str = ""
    expires_in: int = 0
    scopes: list[str] = Field(default_factory=list)
    nonce: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    error: str | None = None


class PayPalOrderResult(BaseModel):
    """Result of creating or fetching a PayPal v2 Checkout Order."""

    success: bool
    order_id: str = ""
    status: str = ""
    amount: float = 0.0
    currency: str = "USD"
    description: str = ""
    approve_url: str = ""
    self_url: str = ""
    create_time: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class PayPalInvoiceResult(BaseModel):
    """Result of creating or listing PayPal v2 Invoices."""

    success: bool
    invoice_id: str = ""
    status: str = ""
    amount: float = 0.0
    currency: str = "USD"
    recipient_email: str = ""
    self_url: str = ""
    error: str | None = None


class PayPalClient:
    """Direct REST client for PayPal Sandbox & Live APIs with 5xx/Cloudflare 520 retry resilience."""

    def __init__(self, settings: PulseSettings | None = None, timeout: float = 15.0) -> None:
        self.settings = settings or get_settings()
        self.timeout = timeout
        self._access_token: str | None = None
        self._token_meta: dict[str, Any] = {}

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        use_request_id: bool = False,
        max_attempts: int = 3,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request against PayPal with automatic retry on transient 5xx/520 errors."""
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(max_attempts):
            try:
                if "headers" not in kwargs or use_request_id:
                    req_id = str(uuid.uuid4()) if use_request_id else None
                    kwargs["headers"] = self._headers(request_id=req_id)
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(method, url, **kwargs)
                if resp.status_code < 500 and resp.status_code != 429:
                    return resp
            except Exception as exc:
                last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (attempt + 1))
        if resp is not None:
            return resp
        raise RuntimeError(str(last_exc) if last_exc else "PayPal request failed")

    def _load_cached_token(self) -> dict[str, Any] | None:
        cache_file = self.settings.token_cache_path
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            expires_at = data.get("expires_at", 0)
            client_prefix = self.settings.paypal_client_id[:12]
            if (
                data.get("client_prefix") == client_prefix
                and data.get("mode") == self.settings.paypal_mode
                and time.time() < expires_at - 60
            ):
                return data
        except Exception:
            return None
        return None

    def _save_cached_token(self, payload: dict[str, Any], latency_ms: float) -> None:
        try:
            expires_in = int(payload.get("expires_in", 32400))
            cache_data = {
                "access_token": payload.get("access_token", ""),
                "app_id": payload.get("app_id", ""),
                "expires_in": expires_in,
                "expires_at": time.time() + expires_in,
                "scope": payload.get("scope", ""),
                "nonce": payload.get("nonce", ""),
                "latency_ms": latency_ms,
                "client_prefix": self.settings.paypal_client_id[:12],
                "mode": self.settings.paypal_mode,
            }
            self.settings.token_cache_path.write_text(
                json.dumps(cache_data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def authenticate(self, force_refresh: bool = False) -> PayPalAuthResult:
        """Acquire OAuth 2.0 Bearer token via POST /v1/oauth2/token."""
        if not self.settings.has_paypal_credentials:
            return PayPalAuthResult(
                authenticated=False,
                mode=self.settings.paypal_mode,
                base_url=self.settings.paypal_base_url,
                error="Missing PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET in .env",
            )

        if not force_refresh:
            cached = self._load_cached_token()
            if cached and cached.get("access_token"):
                token = cached["access_token"]
                self._access_token = token
                self._token_meta = cached
                scopes = [s for s in cached.get("scope", "").split(" ") if s]
                return PayPalAuthResult(
                    authenticated=True,
                    mode=self.settings.paypal_mode,
                    base_url=self.settings.paypal_base_url,
                    app_id=cached.get("app_id", ""),
                    access_token_preview=f"{token[:10]}...{token[-6:]}" if len(token) > 16 else "***",
                    expires_in=int(cached.get("expires_at", time.time()) - time.time()),
                    scopes=scopes,
                    nonce=cached.get("nonce", ""),
                    latency_ms=float(cached.get("latency_ms", 0.0)),
                    cached=True,
                )

        url = f"{self.settings.paypal_base_url}/v1/oauth2/token"
        start = time.perf_counter()
        try:
            resp: httpx.Response | None = None
            for attempt in range(3):
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        url,
                        auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
                        headers={
                            "Accept": "application/json",
                            "Accept-Language": "en_US",
                        },
                        data={"grant_type": "client_credentials"},
                    )
                if resp.status_code < 500:
                    break
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
            assert resp is not None
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            if resp.status_code != 200:
                return PayPalAuthResult(
                    authenticated=False,
                    mode=self.settings.paypal_mode,
                    base_url=self.settings.paypal_base_url,
                    latency_ms=latency_ms,
                    error=f"HTTP {resp.status_code}: {resp.text[:250]}",
                )

            data = resp.json()
            token = data.get("access_token", "")
            self._access_token = token
            self._token_meta = data
            self._save_cached_token(data, latency_ms)
            scopes = [s for s in data.get("scope", "").split(" ") if s]

            return PayPalAuthResult(
                authenticated=True,
                mode=self.settings.paypal_mode,
                base_url=self.settings.paypal_base_url,
                app_id=data.get("app_id", ""),
                access_token_preview=f"{token[:10]}...{token[-6:]}" if len(token) > 16 else "***",
                expires_in=int(data.get("expires_in", 0)),
                scopes=scopes,
                nonce=data.get("nonce", ""),
                latency_ms=latency_ms,
                cached=False,
            )
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            return PayPalAuthResult(
                authenticated=False,
                mode=self.settings.paypal_mode,
                base_url=self.settings.paypal_base_url,
                latency_ms=latency_ms,
                error=str(exc),
            )

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        if not self._access_token:
            auth_res = self.authenticate(force_refresh=False)
            if not auth_res.authenticated:
                raise RuntimeError(auth_res.error or "PayPal authentication failed")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return headers

    def get_balances(self, currency_code: str | None = None) -> dict[str, Any]:
        """Call GET /v1/reporting/balances on PayPal Sandbox."""
        url = f"{self.settings.paypal_base_url}/v1/reporting/balances"
        params: dict[str, str] = {}
        if currency_code:
            params["currency_code"] = currency_code
        try:
            resp = self._request_with_retry("GET", url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "status_code": 200,
                    "account_id": data.get("account_id", ""),
                    "as_of_time": data.get("as_of_time", ""),
                    "balances": data.get("balances", []),
                    "raw": data,
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "balances": [],
                "error": resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "balances": [], "error": str(exc)}

    def get_transactions(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Call GET /v1/reporting/transactions on PayPal Sandbox (max 31-day window)."""
        now = datetime.now(timezone.utc)
        end = end_date or now
        start = start_date or (end - timedelta(days=30))
        url = f"{self.settings.paypal_base_url}/v1/reporting/transactions"
        params = {
            "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fields": "all",
            "page_size": str(page_size),
            "page": "1",
        }
        try:
            resp = self._request_with_retry("GET", url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "status_code": 200,
                    "transaction_details": data.get("transaction_details", []),
                    "total_items": data.get("total_items", 0),
                    "raw": data,
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "transaction_details": [],
                "error": resp.text[:250],
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": 0,
                "transaction_details": [],
                "error": str(exc),
            }

    def get_disputes(self) -> dict[str, Any]:
        """Call GET /v1/customer/disputes on PayPal Sandbox."""
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes"
        try:
            resp = self._request_with_retry("GET", url)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "status_code": 200,
                    "items": data.get("items", []),
                    "raw": data,
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "items": [],
                "error": resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "items": [], "error": str(exc)}

    def get_invoices(self, page_size: int = 10) -> dict[str, Any]:
        """Call GET /v2/invoicing/invoices on PayPal Sandbox."""
        url = f"{self.settings.paypal_base_url}/v2/invoicing/invoices"
        params = {"page": "1", "page_size": str(page_size), "total_required": "true"}
        try:
            resp = self._request_with_retry("GET", url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "status_code": 200,
                    "items": data.get("items", []),
                    "total_items": data.get("total_items", 0),
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "items": [],
                "error": resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "items": [], "error": str(exc)}

    def create_draft_invoice(
        self,
        item_name: str,
        amount: float,
        currency: str = "USD",
        recipient_email: str = "buyer-de@example.com",
        note: str = "PulseCore Pro Software License",
    ) -> PayPalInvoiceResult:
        """Create a live Draft Invoice via POST /v2/invoicing/invoices on PayPal Sandbox."""
        url = f"{self.settings.paypal_base_url}/v2/invoicing/invoices"
        inv_num = f"PP-{int(time.time()) % 100000}-{uuid.uuid4().hex[:4].upper()}"
        payload = {
            "detail": {
                "invoice_number": inv_num,
                "currency_code": currency.upper(),
                "note": note,
                "term": "Due on receipt",
            },
            "primary_recipients": [
                {
                    "billing_info": {
                        "email_address": recipient_email,
                    }
                }
            ],
            "items": [
                {
                    "name": item_name,
                    "quantity": "1",
                    "unit_amount": {
                        "currency_code": currency.upper(),
                        "value": f"{amount:.2f}",
                    },
                }
            ],
        }
        try:
            resp = self._request_with_retry("POST", url, use_request_id=True, json=payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                self_url = data.get("href", "")
                invoice_id = data.get("id", "")
                if not invoice_id and self_url:
                    invoice_id = self_url.rstrip("/").split("/")[-1]
                return PayPalInvoiceResult(
                    success=True,
                    invoice_id=invoice_id or inv_num,
                    status="DRAFT",
                    amount=amount,
                    currency=currency.upper(),
                    recipient_email=recipient_email,
                    self_url=self_url,
                )
            return PayPalInvoiceResult(
                success=False,
                amount=amount,
                currency=currency.upper(),
                recipient_email=recipient_email,
                error=f"HTTP {resp.status_code}: {resp.text[:250]}",
            )
        except Exception as exc:
            return PayPalInvoiceResult(
                success=False,
                amount=amount,
                currency=currency.upper(),
                recipient_email=recipient_email,
                error=str(exc),
            )

    def create_order(
        self,
        amount: float,
        currency: str = "USD",
        description: str = "PulseCore Pro Developer License (v1.2.1)",
        custom_id: str = "v1.2.1|US|CLI",
    ) -> PayPalOrderResult:
        """Create a real Order on PayPal Sandbox via POST /v2/checkout/orders."""
        if amount <= 0:
            return PayPalOrderResult(
                success=False,
                amount=amount,
                currency=currency,
                error="Order amount must be greater than 0",
            )
        url = f"{self.settings.paypal_base_url}/v2/checkout/orders"
        ref_id = f"PULSE-{uuid.uuid4().hex[:8].upper()}"
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": ref_id,
                    "description": description[:127],
                    "custom_id": custom_id[:127],
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": f"{amount:.2f}",
                    },
                }
            ],
            "application_context": {
                "brand_name": "PulseCore Software Labs",
                "user_action": "PAY_NOW",
                "return_url": "https://example.com/pulse/return",
                "cancel_url": "https://example.com/pulse/cancel",
            },
        }
        try:
            resp = self._request_with_retry("POST", url, use_request_id=True, json=payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                order_id = data.get("id", "")
                status = data.get("status", "CREATED")
                approve_url = ""
                self_url = ""
                for link in data.get("links", []):
                    if link.get("rel") == "approve":
                        approve_url = link.get("href", "")
                    elif link.get("rel") == "self":
                        self_url = link.get("href", "")
                return PayPalOrderResult(
                    success=True,
                    order_id=order_id,
                    status=status,
                    amount=amount,
                    currency=currency.upper(),
                    description=description,
                    approve_url=approve_url,
                    self_url=self_url,
                    create_time=data.get(
                        "create_time",
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                    raw=data,
                )
            return PayPalOrderResult(
                success=False,
                amount=amount,
                currency=currency.upper(),
                description=description,
                error=f"HTTP {resp.status_code}: {resp.text[:250]}",
            )
        except Exception as exc:
            return PayPalOrderResult(
                success=False,
                amount=amount,
                currency=currency.upper(),
                description=description,
                error=str(exc),
            )

    def get_order(self, order_id: str) -> PayPalOrderResult:
        """Retrieve a real Order from PayPal Sandbox via GET /v2/checkout/orders/{id}."""
        if not order_id or not order_id.strip():
            return PayPalOrderResult(success=False, error="Order ID cannot be empty")
        url = f"{self.settings.paypal_base_url}/v2/checkout/orders/{order_id.strip()}"
        try:
            resp = self._request_with_retry("GET", url)
            if resp.status_code == 200:
                data = resp.json()
                pu = (data.get("purchase_units") or [{}])[0]
                amt_obj = pu.get("amount") or {}
                approve_url = ""
                self_url = ""
                for link in data.get("links", []):
                    if link.get("rel") == "approve":
                        approve_url = link.get("href", "")
                    elif link.get("rel") == "self":
                        self_url = link.get("href", "")
                return PayPalOrderResult(
                    success=True,
                    order_id=data.get("id", order_id),
                    status=data.get("status", ""),
                    amount=float(amt_obj.get("value", 0.0)),
                    currency=amt_obj.get("currency_code", "USD"),
                    description=pu.get("description", ""),
                    approve_url=approve_url,
                    self_url=self_url,
                    create_time=data.get("create_time", ""),
                    raw=data,
                )
            return PayPalOrderResult(
                success=False,
                order_id=order_id,
                error=f"HTTP {resp.status_code}: {resp.text[:250]}",
            )
        except Exception as exc:
            return PayPalOrderResult(success=False, order_id=order_id, error=str(exc))

    def submit_dispute_resolution(
        self,
        dispute_id: str,
        evidence_text: str,
        amount: float = 5.99,
        currency: str = "EUR",
    ) -> dict[str, Any]:
        """Attempt live dispute evidence submission on PayPal Sandbox and issue a replacement checkout order."""
        dispute_api_status = "LOCAL_SIMULATED"
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes/{dispute_id}/provide-evidence"
        try:
            resp = self._request_with_retry(
                "POST",
                url,
                json={
                    "evidences": [
                        {
                            "evidence_type": "OTHER",
                            "notes": evidence_text[:1900],
                        }
                    ]
                },
            )
            if resp.status_code in (200, 201, 202):
                dispute_api_status = f"SANDBOX_ACCEPTED (HTTP {resp.status_code})"
            else:
                dispute_api_status = f"SANDBOX_CHECKED (HTTP {resp.status_code} - Fallback to Replacement Order)"
        except Exception as exc:
            dispute_api_status = f"ERROR ({exc})"

        # Always execute a verifiable live PayPal Sandbox Orders v2 call for replacement license/settlement
        replacement_order = self.create_order(
            amount=amount,
            currency=currency,
            description=f"Dispute {dispute_id} Resolution & Patched v1.2.2 License Unlock",
            custom_id=f"RESOLVE|{dispute_id}|v1.2.2",
        )
        return {
            "dispute_id": dispute_id,
            "dispute_api_status": dispute_api_status,
            "replacement_order": replacement_order,
        }
