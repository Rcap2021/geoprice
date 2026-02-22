"""
Price Engine - Multi-geo hotel price scraper
Uses Playwright with residential proxies to check prices from different countries
"""

import asyncio
import os
import re
import json
from typing import List, Dict, Any, Optional, AsyncGenerator
from datetime import date, datetime
from dataclasses import dataclass, asdict
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeout

# Pydantic model reference (will be imported properly in production)
from pydantic import BaseModel


class TravelIntent(BaseModel):
    destination_city: Optional[str] = None
    destination_country: Optional[str] = None
    check_in: Optional[date] = None
    check_out: Optional[date] = None
    guests: int = 2
    rooms: int = 1
    hotel_tier: Optional[str] = None


@dataclass
class HotelPrice:
    hotel_name: str
    hotel_id: str
    stars: Optional[int]
    location: str
    price: float
    currency: str
    usd_price: float
    room_type: Optional[str]
    includes_breakfast: bool
    free_cancellation: bool
    review_score: Optional[float]
    booking_url: str


class PriceEngine:
    """
    Multi-geo price comparison engine for Booking.com
    """
    
    # Countries to check (ordered by typical savings potential)
    GEO_COUNTRIES = {
        # Original 12
        "BR": {"name": "Brazil", "currency": "BRL", "locale": "pt-BR", "lang": "pt"},
        "IN": {"name": "India", "currency": "INR", "locale": "en-IN", "lang": "en"},
        "AR": {"name": "Argentina", "currency": "ARS", "locale": "es-AR", "lang": "es"},
        "TR": {"name": "Turkey", "currency": "TRY", "locale": "tr-TR", "lang": "tr"},
        "ID": {"name": "Indonesia", "currency": "IDR", "locale": "id-ID", "lang": "id"},
        "TH": {"name": "Thailand", "currency": "THB", "locale": "th-TH", "lang": "th"},
        "PL": {"name": "Poland", "currency": "PLN", "locale": "pl-PL", "lang": "pl"},
        "MX": {"name": "Mexico", "currency": "MXN", "locale": "es-MX", "lang": "es"},
        "ZA": {"name": "South Africa", "currency": "ZAR", "locale": "en-ZA", "lang": "en"},
        "PT": {"name": "Portugal", "currency": "EUR", "locale": "pt-PT", "lang": "pt"},
        "US": {"name": "United States", "currency": "USD", "locale": "en-US", "lang": "en"},
        "GB": {"name": "United Kingdom", "currency": "GBP", "locale": "en-GB", "lang": "en"},
        # New 20 (proxy-verified)
        "VN": {"name": "Vietnam", "currency": "VND", "locale": "vi-VN", "lang": "vi"},
        "MY": {"name": "Malaysia", "currency": "MYR", "locale": "ms-MY", "lang": "ms"},
        "BD": {"name": "Bangladesh", "currency": "BDT", "locale": "bn-BD", "lang": "bn"},
        "PK": {"name": "Pakistan", "currency": "PKR", "locale": "ur-PK", "lang": "ur"},
        "BG": {"name": "Bulgaria", "currency": "BGN", "locale": "bg-BG", "lang": "bg"},
        "SK": {"name": "Slovakia", "currency": "EUR", "locale": "sk-SK", "lang": "sk"},
        "RS": {"name": "Serbia", "currency": "RSD", "locale": "sr-RS", "lang": "sr"},
        "HU": {"name": "Hungary", "currency": "HUF", "locale": "hu-HU", "lang": "hu"},
        "KZ": {"name": "Kazakhstan", "currency": "KZT", "locale": "kk-KZ", "lang": "kk"},
        "NG": {"name": "Nigeria", "currency": "NGN", "locale": "en-NG", "lang": "en"},
        "MA": {"name": "Morocco", "currency": "MAD", "locale": "ar-MA", "lang": "ar"},
        "TN": {"name": "Tunisia", "currency": "TND", "locale": "ar-TN", "lang": "ar"},
        "PE": {"name": "Peru", "currency": "PEN", "locale": "es-PE", "lang": "es"},
        "CL": {"name": "Chile", "currency": "CLP", "locale": "es-CL", "lang": "es"},
        "JP": {"name": "Japan", "currency": "JPY", "locale": "ja-JP", "lang": "ja"},
        "KR": {"name": "South Korea", "currency": "KRW", "locale": "ko-KR", "lang": "ko"},
        "SG": {"name": "Singapore", "currency": "SGD", "locale": "en-SG", "lang": "en"},
        "HK": {"name": "Hong Kong", "currency": "HKD", "locale": "zh-HK", "lang": "zh"},
        "FR": {"name": "France", "currency": "EUR", "locale": "fr-FR", "lang": "fr"},
        "CA": {"name": "Canada", "currency": "CAD", "locale": "en-CA", "lang": "en"},
        # data.json additional geos
        "AE": {"name": "UAE", "currency": "AED", "locale": "ar-AE", "lang": "ar"},
        "AT": {"name": "Austria", "currency": "EUR", "locale": "de-AT", "lang": "de"},
        "AU": {"name": "Australia", "currency": "AUD", "locale": "en-AU", "lang": "en"},
        "CH": {"name": "Switzerland", "currency": "CHF", "locale": "de-CH", "lang": "de"},
        "DE": {"name": "Germany", "currency": "EUR", "locale": "de-DE", "lang": "de"},
        "ES": {"name": "Spain", "currency": "EUR", "locale": "es-ES", "lang": "es"},
        "IE": {"name": "Ireland", "currency": "EUR", "locale": "en-IE", "lang": "en"},
        "IT": {"name": "Italy", "currency": "EUR", "locale": "it-IT", "lang": "it"},
        "KE": {"name": "Kenya", "currency": "KES", "locale": "en-KE", "lang": "en"},
        "NL": {"name": "Netherlands", "currency": "EUR", "locale": "nl-NL", "lang": "nl"},
        "NZ": {"name": "New Zealand", "currency": "NZD", "locale": "en-NZ", "lang": "en"},
    }
    
    # Approximate exchange rates (in production, fetch from API)
    EXCHANGE_RATES = {
        "USD": 1.0,
        "BRL": 5.0,
        "INR": 83.0,
        "ARS": 850.0,
        "TRY": 32.0,
        "IDR": 15500.0,
        "THB": 35.0,
        "PLN": 4.0,
        "MXN": 17.0,
        "ZAR": 18.5,
        "EUR": 0.92,
        "GBP": 0.79,
        # New 20
        "VND": 25000.0,
        "MYR": 4.7,
        "BDT": 110.0,
        "PKR": 280.0,
        "BGN": 1.8,
        "RSD": 108.0,
        "HUF": 360.0,
        "KZT": 450.0,
        "NGN": 1500.0,
        "MAD": 10.0,
        "TND": 3.1,
        "PEN": 3.7,
        "CLP": 900.0,
        "JPY": 150.0,
        "KRW": 1330.0,
        "SGD": 1.35,
        "HKD": 7.8,
        "CAD": 1.36,
        "AED": 3.67,
        "AUD": 1.60,
        "CHF": 0.89,
        "KES": 130.0,
        "NZD": 1.68,
    }
    
    # Direct proxy mapping from data.json (addr:port, no auth required)
    # Updated from cleaned data.json — bad peers removed by upstream provider
    GEO_PROXIES = {
        # ── From cleaned data.json (reliable) ────────────────────────────────
        "AE": "174.138.161.154:8228",   # UAE
        "AG": "174.138.161.138:8009",   # Antigua
        "AT": "131.153.163.234:8014",   # Austria
        "AU": "174.138.165.138:8013",   # Australia
        "BB": "174.138.165.250:8019",   # Barbados
        "CA": "23.235.247.82:8038",     # Canada
        "CH": "131.153.163.146:8211",   # Switzerland
        "CL": "131.153.1.42:8043",      # Chile
        "DE": "174.138.167.250:8080",   # Germany
        "ES": "174.138.162.242:8202",   # Spain
        "FR": "174.138.161.194:8072",   # France
        "GB": "131.153.1.42:8229",      # UK
        "IE": "174.138.162.138:8104",   # Ireland
        "IL": "131.153.163.178:8105",   # Israel
        "IT": "174.138.161.186:8106",   # Italy
        "JM": "174.138.165.146:8109",   # Jamaica
        "JP": "131.153.163.154:8110",   # Japan
        "KE": "131.153.163.218:8113",   # Kenya
        "KR": "169.150.222.221:8116",   # South Korea
        "MX": "174.138.162.218:8142",   # Mexico
        "NL": "174.138.161.202:8155",   # Netherlands
        "NZ": "131.153.163.26:8158",    # New Zealand
        "PR": "174.138.162.194:8178",   # Puerto Rico
        "TT": "89.187.182.233:8220",    # Trinidad
        "VE": "131.153.1.42:8236",      # Venezuela
        "ZA": "174.138.161.218:8200",   # South Africa
        # US: no dedicated proxy — server is US-based, direct connection = US prices
        # ── Legacy IPs not in data.json but confirmed working ─────────────────
        "BD": "79.127.248.199:8018",    # Bangladesh
        "BG": "79.127.248.200:8033",    # Bulgaria
        "BR": "79.127.137.165:8030",    # Brazil
        "HK": "79.127.248.201:8096",    # Hong Kong
        "HU": "79.127.248.199:8097",    # Hungary
        "ID": "143.244.60.33:8101",     # Indonesia
        "KZ": "79.127.248.200:8112",    # Kazakhstan
        "MA": "143.244.60.33:8149",     # Morocco
        "MY": "143.244.60.33:8133",     # Malaysia
        "NG": "79.127.248.201:8161",    # Nigeria
        "PE": "79.127.248.199:8173",    # Peru
        "PL": "79.127.248.200:8176",    # Poland
        "RS": "79.127.248.199:8192",    # Serbia
        "SG": "79.127.248.200:8195",    # Singapore
        "SK": "79.127.248.199:8196",    # Slovakia
        "TN": "79.127.248.201:8221",    # Tunisia
        "VN": "143.244.60.33:8237",     # Vietnam
        # AR, IN, PK removed — no longer in data.json and returning 0 results
    }

    # Hotel tier to star mapping for filtering
    TIER_STARS = {
        "budget": [1, 2, 3],
        "mid-range": [4],
        "luxury": [5],
        "ultra-luxury": [5],
    }
    
    def __init__(self):
        # Proxy configuration (to be set via environment or config)
        # Supports multiple formats:
        #   1. Base URL with geo appended: http://user:pass@proxy.exalive.ai:PORT-country-{geo}
        #   2. Username with geo: http://user-country-{geo}:pass@proxy.exalive.ai:PORT
        #   3. Per-country env vars: PROXY_BR, PROXY_IN, etc.
        self.proxy_base_url = os.getenv("PROXY_BASE_URL", "")
        self.proxy_username = os.getenv("PROXY_USERNAME", "")
        self.proxy_password = os.getenv("PROXY_PASSWORD", "")
        self.proxy_format = os.getenv("PROXY_FORMAT", "username")  # "url", "username", or "env"
        
        # Concurrency settings
        self.max_concurrent = int(os.getenv("MAX_CONCURRENT_SCRAPERS", "4"))
        self.timeout = int(os.getenv("SCRAPE_TIMEOUT_MS", "90000"))
        # Max pages to scrape per geo per search.
        # Live search: 2 (fast). Scanner daemon: 5 (comprehensive, set via env).
        self.max_pages = int(os.getenv("MAX_PAGES_PER_GEO", "2"))

        # Debug mode - set to True to see browser
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
    
    def _build_booking_url(self, intent: TravelIntent, currency: str = "USD", offset: int = 0) -> str:
        """Build Booking.com search URL.

        Always uses USD so all geo prices are directly comparable.
        changed_currency=1 is required to actually apply the currency override.
        The arbitrage exists because Booking.com applies geo-IP-based price tiers
        regardless of currency — same hotel costs less when browsed from certain countries.
        """
        # Use en-us locale in path so results are in English
        base = "https://www.booking.com/searchresults.en-us.html"

        # Star filter: single nflt param (Booking.com supports one class at a time)
        star_filter = ""
        if intent.hotel_tier and intent.hotel_tier in self.TIER_STARS:
            stars = self.TIER_STARS[intent.hotel_tier]
            # Use the primary star value for the tier
            star_filter = f"nflt=class%3D{stars[0]}"

        params = {
            "ss": intent.destination_city,
            "checkin": intent.check_in.isoformat() if intent.check_in else "",
            "checkout": intent.check_out.isoformat() if intent.check_out else "",
            "group_adults": str(intent.guests),
            "no_rooms": str(intent.rooms),
            "selected_currency": "USD",   # Always USD for direct comparison
            "changed_currency": "1",      # REQUIRED: forces Booking.com to apply currency
            "lang": "en-us",
            "sb_travel_purpose": "leisure",
        }
        if offset > 0:
            params["offset"] = str(offset)

        query = "&".join([f"{k}={v}" for k, v in params.items() if v])
        if star_filter:
            query += "&" + star_filter

        return f"{base}?{query}"
    
    def _get_proxy_for_geo(self, geo_code: str) -> Optional[Dict[str, str]]:
        """
        Get proxy configuration for a specific country.

        Supported formats (PROXY_FORMAT env var):
        1. "env"      - Per-country env vars: PROXY_BR, PROXY_IN, etc.
        2. "viprox"   - Viprox.net format: {user}-rc_{geo}:pass@host:port
        3. "username" - Generic username geo: {user}-country-{geo}:pass@host:port
        4. "url"      - URL geo suffix: http://host:port-country-{geo}
        """
        geo_lower = geo_code.lower()

        # Option 1: Check for per-country environment variable
        country_proxy = os.getenv(f"PROXY_{geo_code}")
        if country_proxy:
            return {"server": country_proxy}

        # Option 2: Direct proxy from GEO_PROXIES (no auth required)
        if self.proxy_format == "direct":
            server = self.GEO_PROXIES.get(geo_code)
            if server:
                return {"server": f"http://{server}"}
            print(f"[{geo_code}] No direct proxy entry, skipping")
            return None

        # If no base URL configured, run without proxy (for testing)
        if not self.proxy_base_url:
            print(f"[{geo_code}] No proxy configured, using direct connection")
            return None

        # Option 3: Viprox.net format
        # Username pattern: {base_user}-rc_{geo_lower}
        # e.g. chatleg-rc_us, chatleg-rc_br
        if self.proxy_format == "viprox":
            geo_username = f"{self.proxy_username}-rc_{geo_lower}"
            return {
                "server": self.proxy_base_url,
                "username": geo_username,
                "password": self.proxy_password,
            }

        # Option 3: Generic username-based geo targeting
        # Format: username-country-br:password@host:port
        if self.proxy_format == "username":
            if self.proxy_username and self.proxy_password:
                geo_username = f"{self.proxy_username}-country-{geo_lower}"
                return {
                    "server": self.proxy_base_url,
                    "username": geo_username,
                    "password": self.proxy_password,
                }
            else:
                return {"server": f"{self.proxy_base_url}-country-{geo_lower}"}

        # Option 4: URL-based geo targeting
        # Format: http://host:port-country-br
        elif self.proxy_format == "url":
            proxy_url = f"{self.proxy_base_url}-country-{geo_lower}"
            if self.proxy_username and self.proxy_password:
                return {
                    "server": proxy_url,
                    "username": self.proxy_username,
                    "password": self.proxy_password,
                }
            return {"server": proxy_url}

        # Default fallback
        return {
            "server": self.proxy_base_url,
            "username": self.proxy_username,
            "password": self.proxy_password,
        } if self.proxy_base_url else None
    
    def _to_usd(self, amount: float, currency: str) -> float:
        """Convert amount to USD"""
        rate = self.EXCHANGE_RATES.get(currency, 1.0)
        return round(amount / rate, 2)
    
    async def _verify_proxy_country(self, context, geo_code: str) -> bool:
        """
        Verify the proxy is actually routing through the expected country.
        - Correct country  → True
        - Wrong country    → False (skip)
        - ECONNREFUSED     → False (proxy is down, skip)
        - Timeout / other  → True  (benefit of the doubt; booking.com may still work)
        """
        try:
            resp = await context.request.get("https://ipinfo.io/json", timeout=8000)
            data = await resp.json()
            actual = data.get("country", "?").upper()
            expected = geo_code.upper()
            if actual == expected:
                print(f"[{geo_code}] ✓ Proxy verified: IP {data.get('ip')} → {actual}")
                return True
            else:
                print(f"[{geo_code}] ✗ Proxy wrong country: got {actual}, expected {expected} — skipping")
                return False
        except Exception as e:
            err = str(e)
            if "ECONNREFUSED" in err or "Connection refused" in err:
                print(f"[{geo_code}] ✗ Proxy down (ECONNREFUSED) — skipping")
                return False
            print(f"[{geo_code}] IP check timed out ({err[:60]}) — proceeding anyway")
            return True  # Timeout: booking.com may still be reachable

    async def _scrape_geo(
        self,
        browser: Browser,
        intent: TravelIntent,
        geo_code: str
    ) -> List[Dict[str, Any]]:
        """
        Scrape hotel prices from Booking.com for a specific geo
        """
        geo_info = self.GEO_COUNTRIES[geo_code]
        results = []

        # Create context with geo-specific settings
        context_options = {
            "locale": geo_info["locale"],
            "timezone_id": self._get_timezone(geo_code),
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        }

        # Add proxy if configured; in direct mode, skip geos with no entry
        proxy = self._get_proxy_for_geo(geo_code)
        if proxy:
            context_options["proxy"] = proxy
        elif self.proxy_format == "direct":
            if geo_code != "US":
                # No proxy available for this geo in direct mode — skip to avoid
                # scraping with the server's own IP (gives wrong geo prices)
                print(f"[{geo_code}] No proxy available — skipping")
                return []
            # US: allow direct connection — all scanner servers are US-based

        context = await browser.new_context(**context_options)
        page = await context.new_page()

        try:
            # Verify proxy is routing through the correct country before scraping
            if proxy:
                ok = await self._verify_proxy_country(context, geo_code)
                if not ok:
                    return []

            # Set language header
            await page.set_extra_http_headers({
                "Accept-Language": f"{geo_info['lang']},{geo_info['lang'][:2]};q=0.9,en;q=0.8",
            })

            # Warm up with homepage to pick up cookies (avoids bot detection)
            warmup_timeout = min(self.timeout, 30000)
            try:
                await page.goto("https://www.booking.com/", timeout=warmup_timeout, wait_until="domcontentloaded")
                await asyncio.sleep(1)
            except Exception:
                pass  # If homepage times out, still try the search

            # Paginate — 25 hotels/page, up to self.max_pages pages.
            # Stop only when a page returns 0 cards (genuine end of results).
            PAGE_SIZE = 25
            MAX_HOTELS = self.max_pages * PAGE_SIZE
            all_hotels_raw = []
            seen_ids: set = set()

            for page_offset in range(0, MAX_HOTELS, PAGE_SIZE):
                url = self._build_booking_url(intent, "USD", offset=page_offset)
                if page_offset == 0:
                    print(f"[{geo_code}] Scraping: {url}")
                else:
                    print(f"[{geo_code}] Page offset={page_offset}")

                await page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")

                # Wait for results to load
                try:
                    await page.wait_for_selector('[data-testid="property-card"]', timeout=20000)
                except PlaywrightTimeout:
                    if page_offset == 0:
                        print(f"[{geo_code}] No results found or timeout")
                        return []
                    else:
                        print(f"[{geo_code}] No more results at offset={page_offset}")
                        break

                await asyncio.sleep(1)

                # Extract hotel data from this page
                page_hotels = await page.evaluate('''() => {
                const cards = document.querySelectorAll('[data-testid="property-card"]');
                return Array.from(cards).map(card => {
                    // Hotel name
                    const nameEl = card.querySelector('[data-testid="title"]');
                    const name = nameEl ? nameEl.innerText.trim() : '';
                    
                    // Price extraction
                    let price = '';
                    let taxText = '';
                    let isPerNight = false;  // true = price is per-night, false = price is total stay
                    const priceEl = card.querySelector('[data-testid="price-and-discounted-price"]')
                        || card.querySelector('.bui-price-display__value')
                        || card.querySelector('[class*="price"]');
                    if (priceEl) {
                        price = priceEl.innerText.trim();

                        // Walk up to find the price section container
                        let container = priceEl.parentElement;
                        for (let i = 0; i < 8 && container; i++) {
                            const text = container.innerText || '';

                            // Detect per-night label (US and some other locales)
                            if (/per\s*night|\/night|night\s*price/i.test(text)) {
                                isPerNight = true;
                            }

                            // When per-night: find the TOTAL stay price in the card.
                            // Booking.com always displays the total alongside the per-night price.
                            // Use this total directly — avoids tax/multiplication errors.
                            if (isPerNight) {
                                // Pattern 1: "Original price $200. Current price $70."
                                // — $70 is the discounted all-in total for the stay
                                const curMatch = text.match(/[Cc]urrent\s+price\s+(?:US)?\$?([\d,]+(?:\.\d+)?)/);
                                if (curMatch) {
                                    price = curMatch[1];
                                    isPerNight = false;
                                    // Don't break — still look for taxes below
                                }
                                // Pattern 2: "Per night $45 $146 Price $146"
                                // — standalone "Price $X" is the non-discounted all-in total
                                if (isPerNight) {
                                    const totMatch = text.match(/\bPrice\s+(?:US)?\$?([\d,]+(?:\.\d+)?)/);
                                    if (totMatch) {
                                        price = totMatch[1];
                                        isPerNight = false;
                                    }
                                }
                            }

                            // Tax with "+" prefix (most non-US locales):
                            // "+ $XX taxes / impuesto / taxe / ..."
                            const taxMatchPlus = text.match(
                                /[+＋]\s*[^\d]*[\d.,]+[^\d]*(?:tax|fee|charge|impuest|cargo|tasa|imposto|taxa|taxe|frais|pajak|biaya|ad[oó]|d[ií]j|podatek|op[łl]at|данък|такса|porez|naknada|da[nň]|thu[eế]|ph[ií]|cukai|vergi|салық|टैक्स|शुल्क|手数料|税|稅|세금|ภาษี|ضريبة|رسوم)/iu
                            );
                            if (taxMatchPlus) {
                                taxText = taxMatchPlus[0];
                                break;
                            }

                            // Tax without "+" prefix (US locale: "Taxes and fees: $XX"):
                            const taxMatchUS = text.match(
                                /(?:taxes?\s+(?:and\s+)?fees?|charges?)[^\d]*\$?\s*([\d.,]+)/i
                            );
                            if (taxMatchUS) {
                                taxText = taxMatchUS[0];
                                break;
                            }

                            container = container.parentElement;
                        }
                    }
                    
                    // Stars — try aria-label first, then SVG icon count
                    const starsEl = card.querySelector('[data-testid="rating-stars"]');
                    let stars = 0;
                    if (starsEl) {
                        const label = starsEl.getAttribute('aria-label') || '';
                        const labelMatch = label.match(/^(\d+)/);
                        if (labelMatch) {
                            stars = parseInt(labelMatch[1]);
                        } else {
                            // Count SVG star icons; fall back to span count / 2
                            const svgs = starsEl.querySelectorAll('svg');
                            stars = svgs.length > 0
                                ? svgs.length
                                : Math.round(starsEl.querySelectorAll('span').length / 2);
                        }
                    }
                    
                    // Review score
                    const reviewEl = card.querySelector('[data-testid="review-score"]');
                    let reviewScore = null;
                    if (reviewEl) {
                        const scoreText = reviewEl.innerText.match(/[\\d.]+/);
                        if (scoreText) reviewScore = parseFloat(scoreText[0]);
                    }
                    
                    // Location
                    const locationEl = card.querySelector('[data-testid="address"]');
                    const location = locationEl ? locationEl.innerText.trim() : '';
                    
                    // Link
                    const linkEl = card.querySelector('a[data-testid="title-link"]') || card.querySelector('a');
                    const link = linkEl ? linkEl.href : '';
                    
                    // Hotel ID from link
                    // URL format: /hotel/{country_code}/{hotel-slug}.html
                    // e.g. /hotel/fr/hotel-minerve.html → hotel-minerve
                    let hotelId = '';
                    const idMatch = link.match(/\\/hotel\\/[a-z]{2}\\/([^./?]+)/);
                    if (idMatch) hotelId = idMatch[1];

                    // Room block ID — identifies the specific room type.
                    // Format: all_sr_blocks=HOTELID_ROOMNUM_OCC_MEAL_?
                    // Same block prefix across geos = same room type = valid price comparison.
                    let roomBlockId = '';
                    const blockMatch = link.match(/all_sr_blocks=([^&]+)/);
                    if (blockMatch) {
                        // Strip the price suffix (__XXXXX) if present
                        roomBlockId = blockMatch[1].split('__')[0];
                    }
                    
                    // Free cancellation
                    const freeCancelEl = card.querySelector('[data-testid="free-cancellation-badge"]');
                    const freeCancel = !!freeCancelEl;
                    
                    // Breakfast
                    const breakfastEl = card.textContent.toLowerCase().includes('breakfast included');
                    
                    return {
                        name,
                        price,
                        taxText,
                        isPerNight,
                        stars,
                        reviewScore,
                        location,
                        link,
                        hotelId,
                        roomBlockId,
                        freeCancel,
                        breakfast: breakfastEl,
                        rawHtml: card.outerHTML
                    };
                }).filter(h => h.name && h.price);
                }''')

                # Deduplicate across pages by hotelId
                new_count = 0
                for h in page_hotels:
                    uid = h.get("hotelId") or h.get("name", "").lower().replace(" ", "-")
                    if uid and uid not in seen_ids:
                        seen_ids.add(uid)
                        all_hotels_raw.append(h)
                        new_count += 1

                print(f"[{geo_code}] offset={page_offset}: {len(page_hotels)} cards, {new_count} new (total {len(all_hotels_raw)})")

                # Stop when Booking.com returns an empty page (no more results).
                # Do NOT stop on new_count==0 — we still try all pages even if
                # this page happened to be all duplicates (happens with some geos).
                if len(page_hotels) == 0:
                    break

            # Booking.com shows per-night prices in some locales — compute stay length
            nights = 1
            if intent.check_in and intent.check_out:
                nights = max(1, (intent.check_out - intent.check_in).days)

            # All prices are in USD (changed_currency=1&selected_currency=USD in URL).
            # Arbitrage exists because Booking.com applies geo-IP-based pricing tiers
            # in the requested currency — same hotel is cheaper from some countries.
            for hotel in all_hotels_raw:
                # isPerNight=True  → multiply by stay length
                # isPerNight=False → total stay price, use as-is
                is_per_night = hotel.get("isPerNight", True)
                raw_price = self._parse_price(hotel["price"], "USD")
                price_value = raw_price * nights if is_per_night else raw_price

                # Taxes follow the same per-night / total convention as the main price
                tax_value = self._parse_price(hotel.get("taxText", ""), "USD")
                if tax_value > 0:
                    tax_total = tax_value * nights if is_per_night else tax_value
                    price_value += tax_total

                if price_value > 0:
                    results.append({
                        "hotel_name": hotel["name"],
                        "hotel_id": hotel["hotelId"] or hotel["name"].lower().replace(" ", "-"),
                        "stars": hotel["stars"] if hotel.get("stars") and 1 <= hotel["stars"] <= 5 else None,
                        "location": hotel["location"],
                        "geo_country": geo_code,
                        "geo_price": price_value,
                        "geo_currency": "USD",
                        "usd_price": price_value,  # Already in USD
                        "room_type": hotel.get("roomBlockId") or None,
                        "includes_breakfast": hotel["breakfast"],
                        "free_cancellation": hotel["freeCancel"],
                        "review_score": hotel["reviewScore"],
                        "booking_url": hotel["link"],
                        "raw_html": hotel.get("rawHtml", ""),
                    })

            print(f"[{geo_code}] Found {len(results)} hotels total")
            
        except Exception as e:
            print(f"[{geo_code}] Scraping error: {e}")
        
        finally:
            await context.close()
        
        return results
    
    def _parse_price(self, price_str: str, currency: str) -> float:
        """Parse price string to float"""
        if not price_str:
            return 0.0
        
        # Remove currency symbols and text
        cleaned = re.sub(r'[^\d.,]', '', price_str)
        
        # Handle different decimal formats
        if ',' in cleaned and '.' in cleaned:
            # Format: 1,234.56 or 1.234,56
            if cleaned.rfind(',') > cleaned.rfind('.'):
                # European format: 1.234,56
                cleaned = cleaned.replace('.', '').replace(',', '.')
            else:
                # US format: 1,234.56
                cleaned = cleaned.replace(',', '')
        elif ',' in cleaned:
            # Could be decimal (3,50) or thousands (1,234)
            if len(cleaned.split(',')[-1]) == 2:
                cleaned = cleaned.replace(',', '.')
            else:
                cleaned = cleaned.replace(',', '')
        elif '.' in cleaned:
            # Dot only: could be decimal (46.5) or European thousands (46.259 / 2.000.000)
            parts = cleaned.split('.')
            if len(parts) == 2 and len(parts[-1]) == 3:
                # Single dot as thousands separator: 46.259 → 46259
                cleaned = cleaned.replace('.', '')
            elif len(parts) > 2 and all(len(p) == 3 for p in parts[1:]):
                # Multiple dots as thousands separators: 2.000.000 → 2000000 (IDR, ARS, etc.)
                cleaned = cleaned.replace('.', '')
            # else: normal decimal like 46.50 — leave as-is

        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    
    def _get_timezone(self, geo_code: str) -> str:
        """Get timezone for geo"""
        timezones = {
            "BR": "America/Sao_Paulo",
            "IN": "Asia/Kolkata",
            "AR": "America/Buenos_Aires",
            "TR": "Europe/Istanbul",
            "ID": "Asia/Jakarta",
            "TH": "Asia/Bangkok",
            "PL": "Europe/Warsaw",
            "MX": "America/Mexico_City",
            "ZA": "Africa/Johannesburg",
            "PT": "Europe/Lisbon",
            "US": "America/New_York",
            "GB": "Europe/London",
            "VN": "Asia/Ho_Chi_Minh",
            "MY": "Asia/Kuala_Lumpur",
            "BD": "Asia/Dhaka",
            "PK": "Asia/Karachi",
            "BG": "Europe/Sofia",
            "SK": "Europe/Bratislava",
            "RS": "Europe/Belgrade",
            "HU": "Europe/Budapest",
            "KZ": "Asia/Almaty",
            "NG": "Africa/Lagos",
            "MA": "Africa/Casablanca",
            "TN": "Africa/Tunis",
            "PE": "America/Lima",
            "CL": "America/Santiago",
            "JP": "Asia/Tokyo",
            "KR": "Asia/Seoul",
            "SG": "Asia/Singapore",
            "HK": "Asia/Hong_Kong",
            "FR": "Europe/Paris",
            "CA": "America/Toronto",
        }
        return timezones.get(geo_code, "UTC")
    
    async def search_all_geos(
        self,
        intent: TravelIntent,
        geos: Optional[List[str]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Search all geos and yield progress updates.
        US is scraped first as the sole baseline, then all other geos run in parallel.
        Each other geo is verified via ipinfo.io before scraping.
        """
        all_geos = geos if geos is not None else list(self.GEO_COUNTRIES.keys())
        # US is the baseline — scrape it first, separately
        compare_geos = [g for g in all_geos if g != "US"]
        total = len(compare_geos) + 1  # +1 for US baseline
        completed = []
        all_results = {}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ]
            )

            try:
                # ── Step 1: Scrape US baseline first ──────────────────────────
                us_results = await self._scrape_geo(browser, intent, "US")
                completed.append("US")
                all_results["US"] = us_results
                yield {
                    "progress": int(1 / total * 100),
                    "geos_completed": completed.copy(),
                    "geo": "US",
                    "geo_results": us_results,
                }

                # ── Step 2: Scrape all comparison geos concurrently ───────────
                semaphore = asyncio.Semaphore(self.max_concurrent)

                async def scrape_with_semaphore(geo: str):
                    async with semaphore:
                        return geo, await self._scrape_geo(browser, intent, geo)

                tasks = [scrape_with_semaphore(geo) for geo in compare_geos]
                for coro in asyncio.as_completed(tasks):
                    geo, results = await coro
                    completed.append(geo)
                    all_results[geo] = results
                    yield {
                        "progress": int(len(completed) / total * 100),
                        "geos_completed": completed.copy(),
                        "geo": geo,
                        "geo_results": results,
                    }

            finally:
                await browser.close()
    
    @staticmethod
    def _room_id_from_block(block_id: str) -> str:
        """Extract the room-type ID (second segment) from an all_sr_blocks value.

        Booking.com block format: HOTEL_ROOM_OCC_MEAL_BOARD
        The HOTEL segment (first) varies by session/geo even for the same property,
        so we match only on the ROOM segment (second).  A value of '0' means no
        specific room was matched — treated as unknown and excluded from comparisons.

        Returns the room ID string, or '' if the block is generic/absent.
        """
        if not block_id:
            return ""
        parts = block_id.split("_")
        if len(parts) < 2:
            return ""
        room_id = parts[1]
        return room_id if room_id != "0" else ""

    def calculate_best_deals(
        self,
        all_results: Dict[str, List[Dict[str, Any]]],
        baseline_geo: str = "US"
    ) -> List[Dict[str, Any]]:
        """
        Calculate best deals across all geos.
        Compares each hotel's price across ALL geo pairs and finds the best savings.
        Only considers geo pairs where BOTH have the same specific room block ID
        (non-zero hotel+room segments in all_sr_blocks) — ensures we're comparing
        the exact same room type, not different rooms at the same hotel.
        """
        # Group hotels by ID/name, accumulating star data from any geo that has it
        hotels_by_id = {}

        for geo, hotels in all_results.items():
            for hotel in hotels:
                hotel_id = hotel["hotel_id"]
                if hotel_id not in hotels_by_id:
                    hotels_by_id[hotel_id] = {
                        "hotel_name": hotel["hotel_name"],
                        "hotel_id": hotel_id,
                        "stars": hotel.get("stars") or None,
                        "location": hotel.get("location", ""),
                        "review_score": hotel.get("review_score"),
                        "prices": {}
                    }
                hotels_by_id[hotel_id]["prices"][geo] = hotel
                # Accumulate stars: take the first non-null value found across geos
                if hotel.get("stars") and not hotels_by_id[hotel_id]["stars"]:
                    hotels_by_id[hotel_id]["stars"] = hotel["stars"]

        # Calculate best deal for each hotel
        best_deals = []

        for hotel_id, hotel_data in hotels_by_id.items():
            prices = hotel_data["prices"]

            if not prices:
                continue

            # Don't show a deal until the baseline country has been scraped —
            # avoids wild swings from using a random early-arriving geo as baseline
            effective_baseline_geo = baseline_geo if baseline_geo in prices else ("US" if "US" in prices else None)
            if effective_baseline_geo is None:
                continue
            baseline = prices[effective_baseline_geo]
            baseline_price = baseline["usd_price"]

            # Skip obviously bad baseline prices (parse failure → near-zero)
            if baseline_price < 5:
                continue

            # Find cheapest geo, excluding any with suspiciously low prices
            valid_prices = {g: p for g, p in prices.items() if p["usd_price"] >= 5}
            if not valid_prices:
                continue

            # Compare ALL geo pairs — find the single best saving combo
            # where cheapest_geo vs expensive_geo have the same room block ID.
            best_saving_pct = 0
            best_cheap_geo = None
            best_exp_geo = None

            geo_list = list(valid_prices.keys())
            for g_cheap in geo_list:
                for g_exp in geo_list:
                    if g_cheap == g_exp:
                        continue
                    p_cheap = valid_prices[g_cheap]["usd_price"]
                    p_exp   = valid_prices[g_exp]["usd_price"]
                    if p_exp <= p_cheap:
                        continue

                    # Room block matching: both must share the same specific room ID
                    # (second segment of all_sr_blocks).  The first segment (hotel_id)
                    # varies by session and geo, so we ignore it.  Room ID '0' means
                    # generic/unmatched — excluded to avoid cross-room-type comparisons.
                    room_cheap = self._room_id_from_block(valid_prices[g_cheap].get("room_type") or "")
                    room_exp   = self._room_id_from_block(valid_prices[g_exp].get("room_type") or "")
                    if not room_cheap or not room_exp or room_cheap != room_exp:
                        continue  # Can't confirm same room type — skip

                    # Sanity check: same room type shouldn't differ by more than 4x.
                    # A larger ratio means different products slipped through (e.g. a
                    # whole-unit rental vs a single hotel room).
                    if p_exp > p_cheap * 4:
                        continue

                    pct = (p_exp - p_cheap) / p_exp * 100
                    if pct > best_saving_pct:
                        best_saving_pct = pct
                        best_cheap_geo  = g_cheap
                        best_exp_geo    = g_exp

            # Also record all geo prices for debug/display
            all_geo_prices = {g: round(p["usd_price"], 2) for g, p in valid_prices.items()}

            if best_cheap_geo is None or best_saving_pct < 1:
                continue

            cheapest  = valid_prices[best_cheap_geo]
            expensive = valid_prices[best_exp_geo]
            savings_usd = expensive["usd_price"] - cheapest["usd_price"]

            best_deals.append({
                "hotel_name": hotel_data["hotel_name"],
                "hotel_id": hotel_id,
                "stars": hotel_data.get("stars"),
                "location": hotel_data.get("location"),
                "review_score": hotel_data.get("review_score"),
                "geo_country": best_cheap_geo,
                "geo_country_name": self.GEO_COUNTRIES[best_cheap_geo]["name"],
                "geo_price": cheapest["geo_price"],
                "geo_currency": cheapest["geo_currency"],
                "usd_price": cheapest["usd_price"],
                "baseline_usd_price": expensive["usd_price"],
                "baseline_geo": best_exp_geo,
                "baseline_geo_name": self.GEO_COUNTRIES.get(best_exp_geo, {}).get("name", best_exp_geo),
                "savings_percent": round(best_saving_pct, 1),
                "savings_usd": round(savings_usd, 2),
                "includes_breakfast": cheapest.get("includes_breakfast", False),
                "free_cancellation": cheapest.get("free_cancellation", False),
                "booking_url": cheapest["booking_url"],
                "baseline_url": expensive.get("booking_url", ""),
                "all_geo_prices": all_geo_prices,
                "us_price": valid_prices.get("US", {}).get("usd_price"),
                # Raw HTML of cheapest and expensive cards for price QA
                "raw_html_cheap": cheapest.get("raw_html", ""),
                "raw_html_expensive": expensive.get("raw_html", ""),
            })

        # Sort by savings percentage
        best_deals.sort(key=lambda x: x["savings_percent"], reverse=True)

        return best_deals


# Test the scraper
if __name__ == "__main__":
    async def test():
        engine = PriceEngine()
        
        intent = TravelIntent(
            destination_city="Paris",
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 20),
            guests=2,
            rooms=1,
            hotel_tier="mid-range"
        )

        print("Starting search...")

        all_results = {}
        async for update in engine.search_all_geos(intent, geos=["US", "BR", "IN", "VN", "MY", "BG", "RS", "NG", "JP", "FR", "CA"]):
            print(f"Progress: {update['progress']}% - Completed: {update['geos_completed']}")
            if "geo_results" in update:
                all_results[update["geo"]] = update["geo_results"]
        
        print("\n--- Best Deals ---")
        best = engine.calculate_best_deals(all_results)
        for deal in best[:5]:
            print(f"{deal['hotel_name']}: ${deal['usd_price']} via {deal['geo_country_name']} "
                  f"(Save {deal['savings_percent']}% / ${deal['savings_usd']})")
    
    asyncio.run(test())
