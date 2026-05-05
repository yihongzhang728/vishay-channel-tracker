"""
DigiKey Product Information API v4 client.

Handles OAuth 2.0 client credentials flow (2-legged) and provides typed wrappers
around the endpoints we use: Manufacturers list, Categories list, KeywordSearch,
and ProductDetails.

DigiKey free-tier rate limits:
- 120 requests / minute
- 1000 requests / day
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.digikey.com"
TOKEN_URL = f"{API_BASE}/v1/oauth2/token"
KEYWORD_SEARCH_URL = f"{API_BASE}/products/v4/search/keyword"
PRODUCT_DETAILS_URL = f"{API_BASE}/products/v4/search/{{}}/productdetails"
MANUFACTURERS_URL = f"{API_BASE}/products/v4/search/manufacturers"
CATEGORIES_URL = f"{API_BASE}/products/v4/search/categories"

# Vishay sub-brands all roll up to manufacturer name "Vishay" on DigiKey.
# We resolve the actual manufacturer ID(s) at runtime via the Manufacturers endpoint.
VISHAY_NAME_PREFIX = "Vishay"


@dataclass
class PartObservation:
    """One day's snapshot of a tracked part."""

    digikey_part_number: str
    manufacturer_part_number: str
    manufacturer: str
    category: str
    quantity_available: int
    unit_price_usd: float
    price_break_qty: int
    lead_time_weeks: int | None
    product_status: str
    raw_payload: dict[str, Any] | None = None


class DigiKeyClient:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        locale_site: str = "US",
        locale_currency: str = "USD",
        locale_language: str = "en",
        request_pause_sec: float = 0.6,
    ):
        self.client_id = client_id or os.environ["DIGIKEY_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["DIGIKEY_CLIENT_SECRET"]
        self.locale_site = locale_site
        self.locale_currency = locale_currency
        self.locale_language = locale_language
        self.request_pause_sec = request_pause_sec  # ~100 req/min, under the 120 ceiling

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()

    # ------------------------------------------------------------------ auth

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        log.info("Requesting new DigiKey OAuth token")
        resp = self._session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # DigiKey tokens typically last ~10 minutes
        self._token_expires_at = time.time() + int(data.get("expires_in", 600))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "X-DIGIKEY-Locale-Site": self.locale_site,
            "X-DIGIKEY-Locale-Language": self.locale_language,
            "X-DIGIKEY-Locale-Currency": self.locale_currency,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ http

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        max_retries: int = 4,
    ) -> dict:
        """Polite request with exponential backoff on 429 / 5xx."""
        for attempt in range(max_retries):
            time.sleep(self.request_pause_sec)
            resp = self._session.request(
                method, url, headers=self._headers(), json=json_body, timeout=30
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2**attempt
                log.warning(
                    "DigiKey returned %d, sleeping %ds (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                # token may have expired despite our cache
                self._token = None
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"DigiKey request failed after {max_retries} retries: {url}")

    # ------------------------------------------------------------------ endpoints

    def list_manufacturers(self) -> list[dict]:
        return self._request("GET", MANUFACTURERS_URL).get("Manufacturers", [])

    def list_categories(self) -> list[dict]:
        return self._request("GET", CATEGORIES_URL).get("Categories", [])

    def keyword_search(
        self,
        *,
        keywords: str = "",
        manufacturer_ids: list[int] | None = None,
        category_ids: list[int] | None = None,
        limit: int = 50,
        offset: int = 0,
        in_stock_only: bool = True,
        min_qty_available: int = 1,
        sort_field: str = "QuantityAvailable",
        sort_direction: str = "Descending",
    ) -> dict:
        """
        Run a paged keyword search.

        DigiKey caps `limit` at 50 per call. Use offset to paginate.
        """
        body: dict[str, Any] = {
            "Keywords": keywords or "*",
            "Limit": min(limit, 50),
            "Offset": offset,
            "FilterOptionsRequest": {},
            "SortOptions": {
                "Field": sort_field,
                "SortOrder": sort_direction,
            },
        }
        if manufacturer_ids:
            body["FilterOptionsRequest"]["ManufacturerFilter"] = [
                {"Id": str(m)} for m in manufacturer_ids
            ]
        if category_ids:
            body["FilterOptionsRequest"]["CategoryFilter"] = [
                {"Id": str(c)} for c in category_ids
            ]
        if in_stock_only:
            body["FilterOptionsRequest"]["MinimumQuantityAvailable"] = max(min_qty_available, 1)
            body["FilterOptionsRequest"]["SearchOptions"] = ["InStock"]

        return self._request("POST", KEYWORD_SEARCH_URL, json_body=body)

    def product_details(self, digikey_part_number: str) -> dict:
        url = PRODUCT_DETAILS_URL.format(requests.utils.quote(digikey_part_number, safe=""))
        return self._request("GET", url)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def extract_observation(
        product: dict,
        target_break_qty: int = 1000,
    ) -> PartObservation:
        """
        Normalize a Product dict (from KeywordSearch result or ProductDetails) into
        a PartObservation. Picks the unit price at the price break closest to
        `target_break_qty` (default 1000-piece, the standard channel-pricing tier).
        """
        # KeywordSearch wraps the product as the dict itself; ProductDetails
        # wraps it under "Product".
        if "Product" in product:
            product = product["Product"]

        mfr = (product.get("Manufacturer") or {}).get("Name") or ""
        category = (product.get("Category") or {}).get("Name") or ""
        mpn = product.get("ManufacturerProductNumber") or product.get("ManufacturerPartNumber") or ""

        # Pick a representative DigiKey part number variant.
        # ProductVariations holds package-type variants; pick the one with most stock.
        variations = product.get("ProductVariations") or []
        if variations:
            best = max(
                variations,
                key=lambda v: (v.get("QuantityAvailableforPackageType") or 0),
            )
            dk_pn = best.get("DigiKeyProductNumber") or product.get("DigiKeyPartNumber") or ""
            qty = best.get("QuantityAvailableforPackageType") or product.get("QuantityAvailable") or 0
            price_breaks = best.get("StandardPricing") or product.get("StandardPricing") or []
        else:
            dk_pn = product.get("DigiKeyPartNumber") or product.get("DigiKeyProductNumber") or ""
            qty = product.get("QuantityAvailable") or 0
            price_breaks = product.get("StandardPricing") or []

        # Pick price at the break closest to target_break_qty (preferring break >= target)
        unit_price = None
        chosen_break = None
        if price_breaks:
            at_or_above = [b for b in price_breaks if (b.get("BreakQuantity") or 0) >= target_break_qty]
            if at_or_above:
                pick = min(at_or_above, key=lambda b: b["BreakQuantity"])
            else:
                pick = max(price_breaks, key=lambda b: b.get("BreakQuantity") or 0)
            unit_price = float(pick.get("UnitPrice") or 0.0)
            chosen_break = int(pick.get("BreakQuantity") or 0)

        # Manufacturer lead time is in weeks
        lead_time = product.get("ManufacturerLeadWeeks")
        try:
            lead_time = int(lead_time) if lead_time is not None else None
        except (ValueError, TypeError):
            lead_time = None

        product_status = (product.get("ProductStatus") or {}).get("Status") or ""

        return PartObservation(
            digikey_part_number=dk_pn,
            manufacturer_part_number=mpn,
            manufacturer=mfr,
            category=category,
            quantity_available=int(qty or 0),
            unit_price_usd=float(unit_price or 0.0),
            price_break_qty=int(chosen_break or 0),
            lead_time_weeks=lead_time,
            product_status=product_status,
        )


def load_client_from_env() -> DigiKeyClient:
    if "DIGIKEY_CLIENT_ID" not in os.environ or "DIGIKEY_CLIENT_SECRET" not in os.environ:
        raise EnvironmentError(
            "Set DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET environment variables. "
            "In GitHub Actions, add them as repo secrets."
        )
    return DigiKeyClient()
