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
    }
    
    # Hotel tier to star mapping for filtering
    TIER_STARS = {
        "budget": [1, 2],
        "mid-range": [3],
        "luxury": [4],
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
        
        # Debug mode - set to True to see browser
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
    
    def _build_booking_url(self, intent: TravelIntent, currency: str = "USD") -> str:
        """Build Booking.com search URL"""
        base = "https://www.booking.com/searchresults.html"
        
        # Star filter based on tier
        star_filter = ""
        if intent.hotel_tier and intent.hotel_tier in self.TIER_STARS:
            stars = self.TIER_STARS[intent.hotel_tier]
            star_filter = "&".join([f"nflt=class%3D{s}" for s in stars])
        
        params = {
            "ss": intent.destination_city,
            "checkin": intent.check_in.isoformat() if intent.check_in else "",
            "checkout": intent.check_out.isoformat() if intent.check_out else "",
            "group_adults": str(intent.guests),
            "no_rooms": str(intent.rooms),
            "selected_currency": currency,
        }
        
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

        # If no base URL configured, run without proxy (for testing)
        if not self.proxy_base_url:
            print(f"[{geo_code}] No proxy configured, using direct connection")
            return None

        # Option 2: Viprox.net format
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

        # Add proxy if configured
        proxy = self._get_proxy_for_geo(geo_code)
        if proxy:
            context_options["proxy"] = proxy

        context = await browser.new_context(**context_options)
        page = await context.new_page()

        try:
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

            # Navigate to search
            url = self._build_booking_url(intent, geo_info["currency"])
            print(f"[{geo_code}] Scraping: {url}")

            await page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")

            # Wait for results to load
            try:
                await page.wait_for_selector('[data-testid="property-card"]', timeout=20000)
            except PlaywrightTimeout:
                print(f"[{geo_code}] No results found or timeout")
                return []

            # Small delay for dynamic content
            await asyncio.sleep(2)
            
            # Extract hotel data
            hotels = await page.evaluate('''() => {
                const cards = document.querySelectorAll('[data-testid="property-card"]');
                return Array.from(cards).slice(0, 15).map(card => {
                    // Hotel name
                    const nameEl = card.querySelector('[data-testid="title"]');
                    const name = nameEl ? nameEl.innerText.trim() : '';
                    
                    // Price - try multiple selectors
                    let price = '';
                    const priceEl = card.querySelector('[data-testid="price-and-discounted-price"]') 
                        || card.querySelector('.bui-price-display__value')
                        || card.querySelector('[class*="price"]');
                    if (priceEl) {
                        price = priceEl.innerText.trim();
                    }
                    
                    // Stars
                    const starsEl = card.querySelector('[data-testid="rating-stars"]');
                    let stars = 0;
                    if (starsEl) {
                        const starSpans = starsEl.querySelectorAll('span');
                        stars = starSpans.length;
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
                    let hotelId = '';
                    const idMatch = link.match(/hotel\\/([^/]+)\\.html/);
                    if (idMatch) hotelId = idMatch[1];
                    
                    // Free cancellation
                    const freeCancelEl = card.querySelector('[data-testid="free-cancellation-badge"]');
                    const freeCancel = !!freeCancelEl;
                    
                    // Breakfast
                    const breakfastEl = card.textContent.toLowerCase().includes('breakfast included');
                    
                    return {
                        name,
                        price,
                        stars,
                        reviewScore,
                        location,
                        link,
                        hotelId,
                        freeCancel,
                        breakfast: breakfastEl
                    };
                }).filter(h => h.name && h.price);
            }''')
            
            # Parse and normalize results
            for hotel in hotels:
                price_value = self._parse_price(hotel["price"], geo_info["currency"])
                if price_value > 0:
                    results.append({
                        "hotel_name": hotel["name"],
                        "hotel_id": hotel["hotelId"] or hotel["name"].lower().replace(" ", "-"),
                        "stars": hotel["stars"] or None,
                        "location": hotel["location"],
                        "geo_country": geo_code,
                        "geo_price": price_value,
                        "geo_currency": geo_info["currency"],
                        "usd_price": self._to_usd(price_value, geo_info["currency"]),
                        "room_type": None,
                        "includes_breakfast": hotel["breakfast"],
                        "free_cancellation": hotel["freeCancel"],
                        "review_score": hotel["reviewScore"],
                        "booking_url": hotel["link"],
                    })
            
            print(f"[{geo_code}] Found {len(results)} hotels")
            
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
            # Dot only: could be decimal (46.5) or European thousands (46.259)
            parts = cleaned.split('.')
            if len(parts) == 2 and len(parts[-1]) == 3:
                # European thousands separator: 46.259 → 46259
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
        Returns an async generator for real-time updates.
        """
        if geos is None:
            geos = list(self.GEO_COUNTRIES.keys())
        
        total = len(geos)
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
                # Process geos with concurrency limit
                semaphore = asyncio.Semaphore(self.max_concurrent)
                
                async def scrape_with_semaphore(geo: str):
                    async with semaphore:
                        return geo, await self._scrape_geo(browser, intent, geo)
                
                # Create tasks for all geos
                tasks = [scrape_with_semaphore(geo) for geo in geos]
                
                # Process as they complete
                for coro in asyncio.as_completed(tasks):
                    geo, results = await coro
                    completed.append(geo)
                    all_results[geo] = results
                    
                    progress = int((len(completed) / total) * 100)
                    
                    yield {
                        "progress": progress,
                        "geos_completed": completed.copy(),
                        "geo": geo,
                        "geo_results": results,
                    }
                
            finally:
                await browser.close()
    
    def calculate_best_deals(
        self, 
        all_results: Dict[str, List[Dict[str, Any]]],
        baseline_geo: str = "US"
    ) -> List[Dict[str, Any]]:
        """
        Calculate best deals across all geos.
        Compares each hotel's price across geos and finds the best savings.
        """
        # Group hotels by ID/name
        hotels_by_id = {}
        
        for geo, hotels in all_results.items():
            for hotel in hotels:
                hotel_id = hotel["hotel_id"]
                if hotel_id not in hotels_by_id:
                    hotels_by_id[hotel_id] = {
                        "hotel_name": hotel["hotel_name"],
                        "hotel_id": hotel_id,
                        "stars": hotel.get("stars"),
                        "location": hotel.get("location", ""),
                        "review_score": hotel.get("review_score"),
                        "prices": {}
                    }
                hotels_by_id[hotel_id]["prices"][geo] = hotel
        
        # Calculate best deal for each hotel
        best_deals = []
        
        for hotel_id, hotel_data in hotels_by_id.items():
            prices = hotel_data["prices"]
            
            if not prices:
                continue
            
            # Find cheapest geo
            cheapest_geo = min(prices.keys(), key=lambda g: prices[g]["usd_price"])
            cheapest = prices[cheapest_geo]
            
            # Get baseline price (US or first available)
            baseline = prices.get(baseline_geo, prices.get(list(prices.keys())[0]))
            baseline_price = baseline["usd_price"]
            
            # Calculate savings
            savings_usd = baseline_price - cheapest["usd_price"]
            savings_percent = (savings_usd / baseline_price * 100) if baseline_price > 0 else 0
            
            # Only include if there are actual savings
            if savings_percent >= 1:  # At least 1% savings
                best_deals.append({
                    "hotel_name": hotel_data["hotel_name"],
                    "hotel_id": hotel_id,
                    "stars": hotel_data.get("stars"),
                    "location": hotel_data.get("location"),
                    "review_score": hotel_data.get("review_score"),
                    "geo_country": cheapest_geo,
                    "geo_country_name": self.GEO_COUNTRIES[cheapest_geo]["name"],
                    "geo_price": cheapest["geo_price"],
                    "geo_currency": cheapest["geo_currency"],
                    "usd_price": cheapest["usd_price"],
                    "baseline_usd_price": baseline_price,
                    "baseline_geo": baseline_geo,
                    "baseline_geo_name": self.GEO_COUNTRIES.get(baseline_geo, {}).get("name", baseline_geo),
                    "savings_percent": round(savings_percent, 1),
                    "savings_usd": round(savings_usd, 2),
                    "includes_breakfast": cheapest.get("includes_breakfast", False),
                    "free_cancellation": cheapest.get("free_cancellation", False),
                    "booking_url": cheapest["booking_url"],
                    "baseline_url": baseline.get("booking_url", ""),
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
