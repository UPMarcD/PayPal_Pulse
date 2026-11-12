"""Real PayPal Sandbox REST API Client (OAuth2, Balances, Transactions, Disputes, Invoices, Orders)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from pulse.config import PulseSettings, get_settings

_GLOBAL_AUTH_LOCK = threading.Lock()


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
    status_code: int | None = None
    error_type: str | None = None
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
    payer_view_url: str = ""
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
        """Execute an HTTP request against PayPal with automatic retry on transient 5xx/520 errors and 401 token refresh."""
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        token_refreshed: bool = False
        # Preserve idempotency key across all retry attempts under RFC 7231 / PayPal Idempotency spec
        req_id = str(uuid.uuid4()) if use_request_id else None
        for attempt in range(max_attempts):
            try:
                hdrs = self._headers(request_id=req_id)
                if "headers" in kwargs and isinstance(kwargs["headers"], dict):
                    hdrs.update(kwargs["headers"])
                if "files" in kwargs:
                    hdrs.pop("Content-Type", None)
                kwargs["headers"] = hdrs
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(method, url, **kwargs)

                # If token expired in-memory (401 Unauthorized), re-authenticate once and retry with same idempotency key
                if resp.status_code == 401 and not token_refreshed:
                    auth_res = self.authenticate(force_refresh=True)
                    if auth_res.authenticated:
                        token_refreshed = True
                        hdrs = self._headers(request_id=req_id)
                        if "headers" in kwargs and isinstance(kwargs["headers"], dict):
                            hdrs.update(kwargs["headers"])
                        if "files" in kwargs:
                            hdrs.pop("Content-Type", None)
                        kwargs["headers"] = hdrs
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
                and time.time() < expires_at - 300
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
            cache_file = self.settings.token_cache_path
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache_file = cache_file.parent / f".tmp_token_{uuid.uuid4().hex[:8]}.json"
            tmp_cache_file.write_text(
                json.dumps(cache_data, indent=2), encoding="utf-8"
            )
            os.replace(tmp_cache_file, cache_file)
        except Exception:
            pass

    def authenticate(self, force_refresh: bool = False) -> PayPalAuthResult:
        """Acquire OAuth 2.0 Bearer token via POST /v1/oauth2/token with double-checked thread locking."""
        if self.settings.demo_mode:
            return PayPalAuthResult(
                authenticated=True,
                mode="demo",
                base_url="mock://demo.local",
                app_id="PULSE-DEMO-SIMULATOR",
                access_token_preview="demo_tok_...0000",
                expires_in=86400,
                scopes=[
                    "https://uri.paypal.com/services/reporting",
                    "https://uri.paypal.com/services/disputes",
                    "https://uri.paypal.com/services/invoicing",
                    "https://uri.paypal.com/services/checkout",
                ],
                latency_ms=1.2,
                cached=True,
            )

        if not self.settings.has_paypal_credentials:
            return PayPalAuthResult(
                authenticated=False,
                mode=self.settings.paypal_mode,
                base_url=self.settings.paypal_base_url,
                error_type="MISSING_CREDENTIALS",
                error="Missing PayPal credentials. Run 'pp setup' to configure ~/.pulse/config.json or set PAYPAL_CLIENT_ID in .env",
            )

        # 1. Fast path: check cached token without acquiring lock
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

        # 2. Synchronized path: acquire mutex lock and re-validate cache (double-checked locking)
        with _GLOBAL_AUTH_LOCK:
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
                    err_type = "AUTH_FAILED" if resp.status_code in (400, 401, 403) else "GATEWAY_ERROR"
                    return PayPalAuthResult(
                        authenticated=False,
                        mode=self.settings.paypal_mode,
                        base_url=self.settings.paypal_base_url,
                        latency_ms=latency_ms,
                        status_code=resp.status_code,
                        error_type=err_type,
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
                    status_code=200,
                    cached=False,
                )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - start) * 1000, 1)
                err_str = str(exc)
                is_net = any(k in type(exc).__name__.lower() for k in ["timeout", "connect", "network", "dns"])
                return PayPalAuthResult(
                    authenticated=False,
                    mode=self.settings.paypal_mode,
                    base_url=self.settings.paypal_base_url,
                    latency_ms=latency_ms,
                    error_type="NETWORK_UNAVAILABLE" if is_net else "CLIENT_ERROR",
                    error=err_str,
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
        if self.settings.demo_mode:
            return {
                "ok": True,
                "status_code": 200,
                "account_id": "DEMO-ACCOUNT-001",
                "as_of_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "balances": [
                    {"currency": "USD", "total_balance": {"value": "5420.50", "currency_code": "USD"}},
                    {"currency": "EUR", "total_balance": {"value": "1280.00", "currency_code": "EUR"}},
                    {"currency": "PHP", "total_balance": {"value": "48500.00", "currency_code": "PHP"}},
                ],
                "raw": {},
            }
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
        """Call GET /v1/reporting/transactions on PayPal Sandbox (max 31-day window).

        PayPal pages this endpoint. A single request returns at most page_size
        rows, so the client follows total_pages until the window is exhausted.
        """
        if self.settings.demo_mode:
            return {"ok": True, "transaction_details": [], "total_items": 0}
        now = datetime.now(timezone.utc)
        end = end_date or now
        start = start_date or (end - timedelta(days=30))
        url = f"{self.settings.paypal_base_url}/v1/reporting/transactions"
        details: list[dict[str, Any]] = []
        total_items = 0
        page = 1
        try:
            while page <= 100:
                params = {
                    "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "fields": "all",
                    "page_size": str(page_size),
                    "page": str(page),
                }
                resp = self._request_with_retry("GET", url, params=params)
                if resp.status_code != 200:
                    return {
                        "ok": False,
                        "status_code": resp.status_code,
                        "transaction_details": details,
                        "total_items": total_items,
                        "error": resp.text[:250],
                    }
                data = resp.json()
                page_rows = data.get("transaction_details", [])
                details.extend(page_rows)
                total_items = int(data.get("total_items", len(details)) or len(details))
                total_pages = int(data.get("total_pages", 1) or 1)
                if page >= total_pages or not page_rows:
                    break
                page += 1
            return {
                "ok": True,
                "status_code": 200,
                "transaction_details": details,
                "total_items": total_items,
                "raw": {"transaction_details": details, "total_items": total_items},
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": 0,
                "transaction_details": details,
                "total_items": total_items,
                "error": str(exc),
            }

    def get_dispute_details(self, dispute_id: str) -> dict[str, Any]:
        """Call GET /v1/customer/disputes/{id} to retrieve full buyer messages, items, custom tags, and HATEOAS links."""
        if not dispute_id or not dispute_id.strip():
            return {"ok": False, "status_code": 400, "dispute": {}, "error": "Dispute ID cannot be empty."}
        if self.settings.demo_mode:
            return {"ok": True, "status_code": 200, "dispute": {"dispute_id": dispute_id.strip(), "status": "OPEN"}}
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes/{dispute_id.strip()}"
        try:
            resp = self._request_with_retry("GET", url)
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "status_code": 200, "dispute": data}
            return {
                "ok": False,
                "status_code": resp.status_code,
                "dispute": {},
                "error": resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "dispute": {}, "error": str(exc)}

    def get_disputes(self, enrich_details: bool = True) -> dict[str, Any]:
        """Call GET /v1/customer/disputes on PayPal Sandbox and optionally enrich with full dispute details."""
        if self.settings.demo_mode:
            return {"ok": True, "status_code": 200, "items": [], "total_items": 0}
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes"
        try:
            resp = self._request_with_retry("GET", url)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                if enrich_details and items:
                    enriched_items: list[dict[str, Any]] = []
                    for item in items[:10]:
                        disp_id = item.get("dispute_id", "")
                        if disp_id and "messages" not in item:
                            detail_res = self.get_dispute_details(disp_id)
                            if detail_res.get("ok") and isinstance(detail_res.get("dispute"), dict) and detail_res["dispute"].get("dispute_id"):
                                merged = dict(item)
                                merged.update(detail_res["dispute"])
                                enriched_items.append(merged)
                                continue
                        enriched_items.append(item)
                    items = enriched_items + items[10:]
                return {
                    "ok": True,
                    "status_code": 200,
                    "items": items,
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
        if self.settings.demo_mode:
            return {"ok": True, "status_code": 200, "items": [], "total_items": 0}
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
        note: str | None = None,
        auto_send: bool = False,
    ) -> PayPalInvoiceResult:
        """Create a live Draft Invoice via POST /v2/invoicing/invoices on PayPal Sandbox."""
        from pulse.store import detect_project_context, format_currency_amount

        proj_ctx = detect_project_context()
        derived_sku = proj_ctx.get("default_sku") or proj_ctx.get("project_name")
        effective_note = note or (f"{derived_sku} Software License" if derived_sku else "PulseCore Pro Software License")
        formatted_val = format_currency_amount(amount, currency)

        if self.settings.demo_mode:
            mock_inv = f"INV2-DEMO-{uuid.uuid4().hex[:8].upper()}"
            return PayPalInvoiceResult(
                success=True,
                invoice_id=mock_inv,
                status="DRAFT",
                amount=amount,
                currency=currency.upper(),
                recipient_email=recipient_email,
                payer_view_url=f"https://www.sandbox.paypal.com/invoice/payerView/details/{mock_inv}",
            )
        url = f"{self.settings.paypal_base_url}/v2/invoicing/invoices"
        inv_num = f"PP-{int(time.time()) % 100000}-{uuid.uuid4().hex[:4].upper()}"
        payload = {
            "detail": {
                "invoice_number": inv_num,
                "currency_code": currency.upper(),
                "note": effective_note,
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
                        "value": formatted_val,
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
                final_id = invoice_id or inv_num
                payer_view_base = (
                    "https://www.sandbox.paypal.com/invoice/payerView/details"
                    if self.settings.paypal_mode == "sandbox"
                    else "https://www.paypal.com/invoice/payerView/details"
                )
                status = "DRAFT"
                payer_view_url = ""
                if auto_send and final_id:
                    send_res = self.send_invoice(final_id, send_to_recipient=False)
                    if send_res.get("ok"):
                        status = "SENT"
                        payer_view_url = f"{payer_view_base}/{final_id}"

                return PayPalInvoiceResult(
                    success=True,
                    invoice_id=final_id,
                    status=status,
                    amount=amount,
                    currency=currency.upper(),
                    recipient_email=recipient_email,
                    self_url=self_url,
                    payer_view_url=payer_view_url,
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

    def send_invoice(
        self,
        invoice_id: str,
        send_to_recipient: bool = True,
        send_to_invoicer: bool = False,
    ) -> dict[str, Any]:
        """Send an invoice via POST /v2/invoicing/invoices/{id}/send to make it payable and viewable by the buyer."""
        if self.settings.demo_mode:
            return {
                "ok": True,
                "invoice_id": invoice_id,
                "status": "SENT",
                "payer_view_url": f"https://www.sandbox.paypal.com/invoice/payerView/details/{invoice_id}",
            }
        url = f"{self.settings.paypal_base_url}/v2/invoicing/invoices/{invoice_id}/send"
        payer_view_base = (
            "https://www.sandbox.paypal.com/invoice/payerView/details"
            if self.settings.paypal_mode == "sandbox"
            else "https://www.paypal.com/invoice/payerView/details"
        )
        payer_view_url = f"{payer_view_base}/{invoice_id}"
        payload = {
            "send_to_recipient": send_to_recipient,
            "send_to_invoicer": send_to_invoicer,
        }
        try:
            resp = self._request_with_retry("POST", url, use_request_id=True, json=payload)
            if resp.status_code in (200, 201, 202):
                return {
                    "ok": True,
                    "invoice_id": invoice_id,
                    "status": "SENT",
                    "payer_view_url": payer_view_url,
                }
            return {
                "ok": False,
                "invoice_id": invoice_id,
                "payer_view_url": payer_view_url,
                "error": f"HTTP {resp.status_code}: {resp.text[:250]}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "invoice_id": invoice_id,
                "payer_view_url": payer_view_url,
                "error": str(exc),
            }

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
        if self.settings.demo_mode:
            mock_id = f"DEMO-{uuid.uuid4().hex[:12].upper()}"
            return PayPalOrderResult(
                success=True,
                order_id=mock_id,
                status="CREATED",
                amount=amount,
                currency=currency.upper(),
                description=description,
                approve_url=f"https://www.sandbox.paypal.com/checkoutnow?token={mock_id}",
                self_url=f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{mock_id}",
                create_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                raw={"id": mock_id, "status": "CREATED"},
            )
        from pulse.store import detect_project_context, format_currency_amount

        proj_ctx = detect_project_context()
        derived_brand = proj_ctx.get("default_sku") or proj_ctx.get("project_name")
        brand_name = (
            str(derived_brand)[:127]
            if derived_brand
            else "PulseCore Software Labs"
        )
        url = f"{self.settings.paypal_base_url}/v2/checkout/orders"
        ref_id = f"PULSE-{uuid.uuid4().hex[:8].upper()}"
        formatted_val = format_currency_amount(amount, currency)
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": ref_id,
                    "description": description[:127],
                    "custom_id": custom_id[:127],
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": formatted_val,
                    },
                }
            ],
            "application_context": {
                "brand_name": brand_name,
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
        if self.settings.demo_mode:
            return PayPalOrderResult(
                success=True,
                order_id=order_id.strip(),
                status="CREATED",
                amount=19.99,
                currency="USD",
                description="Demo order",
                approve_url=f"https://www.sandbox.paypal.com/checkoutnow?token={order_id.strip()}",
                self_url=f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{order_id.strip()}",
                create_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                raw={"id": order_id.strip(), "status": "CREATED"},
            )
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

    def capture_order(self, order_id: str) -> PayPalOrderResult:
        """Capture payment for an approved PayPal Order via POST /v2/checkout/orders/{id}/capture."""
        if not order_id or not order_id.strip():
            return PayPalOrderResult(success=False, error="Order ID cannot be empty")

        if self.settings.demo_mode:
            return PayPalOrderResult(
                success=True,
                order_id=order_id.strip(),
                status="COMPLETED",
                amount=19.99,
                currency="USD",
                description="Captured order (Demo Mode)",
                raw={"id": order_id.strip(), "status": "COMPLETED"},
            )

        url = f"{self.settings.paypal_base_url}/v2/checkout/orders/{order_id.strip()}/capture"
        det_req_id = f"PULSE-CAP-{order_id.strip()}"
        try:
            resp = self._request_with_retry(
                "POST",
                url,
                use_request_id=False,
                headers={
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": det_req_id,
                },
                content="{}",
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                pu = (data.get("purchase_units") or [{}])[0]
                payments = pu.get("payments") or {}
                captures = payments.get("captures") or [{}]
                cap_amt = (captures[0].get("amount") or {})
                val = float(cap_amt.get("value", 0.0))
                cur = cap_amt.get("currency_code", "USD")
                return PayPalOrderResult(
                    success=True,
                    order_id=data.get("id", order_id),
                    status=data.get("status", "COMPLETED"),
                    amount=val,
                    currency=cur,
                    create_time=data.get("create_time", ""),
                    raw=data,
                )
            if resp.status_code == 422 and "ORDER_ALREADY_CAPTURED" in resp.text:
                existing = self.get_order(order_id.strip())
                if existing.success and existing.status == "COMPLETED":
                    return existing
            return PayPalOrderResult(
                success=False,
                order_id=order_id,
                error=f"HTTP {resp.status_code}: {resp.text[:250]}",
            )
        except Exception as exc:
            return PayPalOrderResult(
                success=False,
                order_id=order_id,
                error=str(exc),
            )

    def send_dispute_message(self, dispute_id: str, message: str) -> dict[str, Any]:
        """Call POST /v1/customer/disputes/{id}/send-message to reply directly in the buyer conversation thread."""
        if not dispute_id or not message:
            return {"ok": False, "status_code": 400, "error": "Dispute ID and message are required."}
        if self.settings.demo_mode:
            return {"ok": True, "status_code": 200, "status": "DEMO_MESSAGE_SENT"}
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes/{dispute_id.strip()}/send-message"
        try:
            resp = self._request_with_retry(
                "POST",
                url,
                headers={"Content-Type": "application/json"},
                json={"message": message[:1950]},
            )
            return {
                "ok": resp.status_code in (200, 201, 202, 204),
                "status_code": resp.status_code,
                "error": "" if resp.status_code in (200, 201, 202, 204) else resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "error": str(exc)}

    def accept_dispute_claim(
        self,
        dispute_id: str,
        note: str,
        accept_claim_reason: str = "DID_NOT_SHIP_ITEM",
    ) -> dict[str, Any]:
        """Call POST /v1/customer/disputes/{id}/accept-claim for amicable full refund resolution."""
        if not dispute_id:
            return {"ok": False, "status_code": 400, "error": "Dispute ID is required."}
        if self.settings.demo_mode:
            return {"ok": True, "status_code": 200, "status": "DEMO_CLAIM_ACCEPTED"}
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes/{dispute_id.strip()}/accept-claim"
        try:
            resp = self._request_with_retry(
                "POST",
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "note": (note or "Amicable claim resolution and full refund processed.")[:1950],
                    "accept_claim_reason": accept_claim_reason,
                },
            )
            return {
                "ok": resp.status_code in (200, 201, 202, 204),
                "status_code": resp.status_code,
                "error": "" if resp.status_code in (200, 201, 202, 204) else resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "error": str(exc)}

    def make_dispute_offer(
        self,
        dispute_id: str,
        note: str,
        offer_type: str = "REFUND",
        amount: float = 5.99,
        currency: str = "USD",
    ) -> dict[str, Any]:
        """Call POST /v1/customer/disputes/{id}/make-offer to propose a settlement offer during INQUIRY."""
        if not dispute_id:
            return {"ok": False, "status_code": 400, "error": "Dispute ID is required."}
        if self.settings.demo_mode:
            return {"ok": True, "status_code": 200, "status": "DEMO_OFFER_MADE"}
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes/{dispute_id.strip()}/make-offer"
        payload: dict[str, Any] = {
            "note": (note or "Settlement offer proposed.")[:1950],
            "offer_type": offer_type,
        }
        if offer_type in ("REFUND", "REFUND_WITH_REPLACEMENT", "REFUND_WITH_RETURN"):
            payload["offer_amount"] = {"currency_code": currency.upper(), "value": f"{amount:.2f}"}
        try:
            resp = self._request_with_retry(
                "POST",
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            return {
                "ok": resp.status_code in (200, 201, 202, 204),
                "status_code": resp.status_code,
                "error": "" if resp.status_code in (200, 201, 202, 204) else resp.text[:250],
            }
        except Exception as exc:
            return {"ok": False, "status_code": 0, "error": str(exc)}

    @staticmethod
    def select_evidence_type_for_reason(reason: str) -> str:
        """Map PayPal dispute reason to the most appropriate PayPal evidence_type."""
        r_up = (reason or "").upper()
        if r_up in (
            "DUPLICATE_TRANSACTION",
            "INCORRECT_AMOUNT",
            "CREDIT_NOT_PROCESSED",
            "CANCELED_RECURRING_BILLING",
            "PAYMENT_BY_OTHER_MEANS",
        ):
            return "PROOF_OF_REFUND"
        if r_up in ("UNAUTHORISED", "OTHER", "PROBLEM_WITH_REMITTANCE"):
            return "OTHER"
        if r_up == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
            return "ITEM_DESCRIPTION"
        return "PROOF_OF_FULFILLMENT"

    def submit_dispute_resolution(
        self,
        dispute_id: str,
        evidence_text: str,
        amount: float = 5.99,
        currency: str = "EUR",
        reason: str = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        buyer_message: str | None = None,
        resolution_strategy: str = "FULFILL_AND_EVIDENCE",
        replacement_order: PayPalOrderResult | None = None,
        tracking_info: list[dict[str, Any]] | None = None,
        refund_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute multi-channel dispute remediation on PayPal Sandbox (evidence, buyer message, claim/offer)."""
        if self.settings.demo_mode:
            if replacement_order is None:
                rep_id = f"DEMO-RESOLVE-{uuid.uuid4().hex[:8].upper()}"
                replacement_order = PayPalOrderResult(
                    success=True,
                    order_id=rep_id,
                    status="CREATED",
                    amount=amount,
                    currency=currency,
                    description=f"Demo remediation entitlement for dispute {dispute_id}",
                    approve_url=f"https://www.sandbox.paypal.com/checkoutnow?token={rep_id}",
                )
            return {
                "dispute_id": dispute_id,
                "dispute_api_status": "DEMO_RESOLVED",
                "replacement_order": replacement_order,
                "strategy_used": resolution_strategy,
            }

        dispute_api_status = "NOT_SENT"
        notes_to_send = (buyer_message or evidence_text)[:1900]
        url = f"{self.settings.paypal_base_url}/v1/customer/disputes/{dispute_id}/provide-evidence"

        # Accepting the claim refunds the buyer through PayPal. Evidence is not
        # required on this path, so do not block on missing tracking data.
        accept_claim = resolution_strategy == "ACCEPT_AND_REFUND"
        evidence_type = self.select_evidence_type_for_reason(reason)

        ok = False
        api_error = ""
        if not accept_claim:
            evidence: dict[str, Any] = {
                "evidence_type": evidence_type,
                "notes": notes_to_send,
            }
            if evidence_type == "PROOF_OF_FULFILLMENT":
                if tracking_info:
                    evidence["evidence_info"] = {"tracking_info": tracking_info}
                else:
                    # Digital delivery has no carrier tracking. Use the item
                    # description evidence type, which accepts notes alone.
                    evidence_type = "ITEM_DESCRIPTION"
                    evidence["evidence_type"] = evidence_type
            elif evidence_type == "PROOF_OF_REFUND":
                if not refund_ids:
                    return {
                        "ok": False,
                        "dispute_id": dispute_id,
                        "dispute_api_status": "BLOCKED_MISSING_EVIDENCE",
                        "error": "PROOF_OF_REFUND requires refund_ids.",
                    }
                evidence["evidence_info"] = {"refund_ids": refund_ids}

            try:
                input_payload = json.dumps({"evidences": [evidence]})
                resp = self._request_with_retry(
                    "POST",
                    url,
                    files={"input": (None, input_payload, "application/json")},
                )
                if resp.status_code in (200, 201, 202):
                    ok = True
                    dispute_api_status = f"SUBMITTED (HTTP {resp.status_code})"
                else:
                    dispute_api_status = f"FAILED (HTTP {resp.status_code})"
                    api_error = resp.text[:400]
            except Exception as exc:
                dispute_api_status = f"ERROR ({exc})"
                api_error = str(exc)

        # Also dispatch a direct conversational reply if buyer_message is provided
        message_api_status = "SKIPPED"
        if buyer_message:
            msg_res = self.send_dispute_message(dispute_id, buyer_message)
            if msg_res.get("ok"):
                message_api_status = f"MESSAGE_SENT (HTTP {msg_res.get('status_code')})"
                if not ok:
                    dispute_api_status = f"{dispute_api_status} + {message_api_status}"

        if accept_claim:
            acc_res = self.accept_dispute_claim(dispute_id, note=notes_to_send)
            if acc_res.get("ok"):
                ok = True
                dispute_api_status = f"CLAIM_REFUNDED (HTTP {acc_res.get('status_code')})"
            else:
                dispute_api_status = f"CLAIM_REFUND_FAILED (HTTP {acc_res.get('status_code')})"
                api_error = str(acc_res.get("error", ""))

        if replacement_order is None:
            replacement_order = self.create_order(
                amount=amount,
                currency=currency,
                description=f"Dispute {dispute_id} Remediation Entitlement",
                custom_id=f"RESOLVE|{dispute_id}",
            )

        # Query gateway for updated lifecycle status post-submission
        updated_status = "UNDER_REVIEW"
        try:
            det = self.get_dispute_details(dispute_id)
            if det.get("ok") and det.get("dispute"):
                updated_status = det["dispute"].get("status", "UNDER_REVIEW")
        except Exception:
            pass

        return {
            "ok": ok,
            "dispute_id": dispute_id,
            "dispute_api_status": dispute_api_status,
            "error": api_error,
            "message_api_status": message_api_status,
            "strategy_used": resolution_strategy,
            "replacement_order": replacement_order,
            "updated_status": updated_status,
        }
