"""
GeoPrice Deal Scanner
=====================
Background service that pre-scans hotel deals across all 32 geo proxies
for multiple popular cities.

Scan schedule: every 3-day check-in interval over the next 3 months.
Stay durations: 2-night and 7-night stays.
Hotel tiers: budget, mid-range, luxury.
Results stored in SQLite, served via /api/featured-deals.

Remote scanner mode: set REPORT_URL + REPORT_TOKEN to have this scanner
POST deals back to a central GeoPrice server instead of writing locally.

Usage:
  python deal_scanner.py              # Continuous daemon (loops every 6 hours)
  python deal_scanner.py scan-once    # Single pass then exit
  python deal_scanner.py serve        # FastAPI server on port 8767
  python deal_scanner.py serve-bg     # Serve + start scanner daemon in background

Deploy on any machine that has access to the geo proxies:
  REPORT_URL=https://hotels.chatleg.ai REPORT_TOKEN=<token> python deal_scanner.py

Popular cities mode (on the central server):
  SCAN_CITIES="Paris:France,London:UK,Tokyo:Japan" python deal_scanner.py
"""

import asyncio
import json
import itertools
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("deal_scanner")

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DEAL_SCANNER_DB", "/tmp/geoprice_deals.db")

# Single-city backward-compat (overridden by SCAN_CITIES)
SCAN_CITY = os.getenv("SCAN_CITY", "Paris")
SCAN_COUNTRY = os.getenv("SCAN_COUNTRY", "France")

# Multi-city: "Paris:France,London:United Kingdom,Tokyo:Japan"
# Each entry is "City:Country" — use the full country name Booking.com recognises.
_DEFAULT_CITIES = (
    "Paris:France,"
    "London:United Kingdom,"
    "New York:United States,"
    "Tokyo:Japan,"
    "Barcelona:Spain,"
    "Rome:Italy,"
    "Amsterdam:Netherlands,"
    "Bangkok:Thailand,"
    "Dubai:United Arab Emirates,"
    "Singapore:Singapore,"
    "Prague:Czech Republic,"
    "Lisbon:Portugal,"
    "Vienna:Austria,"
    "Berlin:Germany,"
    "Bali:Indonesia"
)
_cities_raw = os.getenv("SCAN_CITIES", _DEFAULT_CITIES)
SCAN_CITIES: List[Tuple[str, str]] = []
for _entry in _cities_raw.split(","):
    _entry = _entry.strip()
    if ":" in _entry:
        _c, _co = _entry.split(":", 1)
        SCAN_CITIES.append((_c.strip(), _co.strip()))
if not SCAN_CITIES:
    SCAN_CITIES = [(SCAN_CITY, SCAN_COUNTRY)]

DATE_INTERVAL_DAYS = int(os.getenv("DATE_INTERVAL_DAYS", "3"))
SCAN_MONTHS = int(os.getenv("SCAN_MONTHS", "3"))
STAY_DURATIONS = [2, 7]           # nights
HOTEL_TIERS = ["budget", "mid-range", "luxury"]
MIN_SAVINGS_PCT = float(os.getenv("MIN_SAVINGS_PCT", "10.0"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
MAX_PAGES = int(os.getenv("MAX_PAGES_PER_GEO", "5"))  # 5 pages × 25 hotels = 125 per geo
RESCAN_HOURS = float(os.getenv("RESCAN_HOURS", "12"))
LOOP_PAUSE_HOURS = float(os.getenv("LOOP_PAUSE_HOURS", "6"))

# Remote reporting (optional) — set on remote scanners to POST deals back
REPORT_URL = os.getenv("REPORT_URL", "").rstrip("/")   # e.g. https://hotels.chatleg.ai
REPORT_TOKEN = os.getenv("REPORT_TOKEN", "")           # bearer token


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS deals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            city                TEXT    NOT NULL DEFAULT 'Paris',
            check_in            TEXT    NOT NULL,
            check_out           TEXT    NOT NULL,
            nights              INTEGER NOT NULL,
            tier                TEXT    NOT NULL,
            hotel_id            TEXT,
            hotel_name          TEXT,
            stars               INTEGER,
            cheapest_geo        TEXT,
            cheapest_geo_name   TEXT,
            cheapest_usd        REAL,
            baseline_geo        TEXT    DEFAULT 'US',
            baseline_usd        REAL,
            savings_pct         REAL,
            savings_usd         REAL,
            booking_url         TEXT,
            baseline_url        TEXT,
            scanned_at          TEXT    NOT NULL,
            raw_json            TEXT,
            is_verified         INTEGER DEFAULT 0,
            verified_at         TEXT,
            verify_fails        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS scan_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            city        TEXT    NOT NULL DEFAULT 'Paris',
            check_in    TEXT    NOT NULL,
            check_out   TEXT    NOT NULL,
            tier        TEXT    NOT NULL,
            scanned_at  TEXT    NOT NULL,
            deals_found INTEGER DEFAULT 0,
            duration_s  REAL    DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_deals_savings    ON deals(savings_pct DESC);
        CREATE INDEX IF NOT EXISTS idx_deals_checkin    ON deals(check_in);
        CREATE INDEX IF NOT EXISTS idx_deals_hotel      ON deals(hotel_id);
        CREATE INDEX IF NOT EXISTS idx_deals_city       ON deals(city);
        CREATE INDEX IF NOT EXISTS idx_log_combo        ON scan_log(check_in, check_out, tier);
    """)
    # Migration: add verification columns to existing DBs (ALTER TABLE is idempotent via try/except)
    for col, defn in [
        ("is_verified",  "INTEGER DEFAULT 0"),
        ("verified_at",  "TEXT"),
        ("verify_fails", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE deals ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Create the verified index after migration ensures the column exists
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_verified ON deals(is_verified)")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _combo_last_scanned(db_path: str, city: str, check_in: date, check_out: date, tier: str) -> Optional[str]:
    conn = sqlite3.connect(db_path)
    cutoff = (datetime.utcnow() - timedelta(hours=RESCAN_HOURS)).isoformat()
    row = conn.execute(
        "SELECT scanned_at FROM scan_log "
        "WHERE city=? AND check_in=? AND check_out=? AND tier=? AND scanned_at > ? "
        "ORDER BY scanned_at DESC LIMIT 1",
        (city, check_in.isoformat(), check_out.isoformat(), tier, cutoff),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def save_deals(db_path: str, city: str, check_in: date, check_out: date,
               tier: str, deals: list, duration_s: float):
    nights = (check_out - check_in).days
    conn = sqlite3.connect(db_path)
    now = datetime.utcnow().isoformat()

    # Replace stale results for this combo
    conn.execute(
        "DELETE FROM deals WHERE city=? AND check_in=? AND check_out=? AND tier=?",
        (city, check_in.isoformat(), check_out.isoformat(), tier),
    )

    for d in deals:
        conn.execute(
            """INSERT INTO deals
               (city, check_in, check_out, nights, tier,
                hotel_id, hotel_name, stars,
                cheapest_geo, cheapest_geo_name, cheapest_usd,
                baseline_geo, baseline_usd, savings_pct, savings_usd,
                booking_url, baseline_url, scanned_at, raw_json,
                is_verified, verified_at, verify_fails)
               VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?)""",
            (
                city, check_in.isoformat(), check_out.isoformat(), nights, tier,
                d.get("hotel_id"), d.get("hotel_name"), d.get("stars"),
                d.get("geo_country"), d.get("geo_country_name"), d.get("usd_price"),
                d.get("baseline_geo", "US"), d.get("baseline_usd_price"),
                d.get("savings_percent"), d.get("savings_usd"),
                d.get("booking_url"), d.get("baseline_url"),
                now, json.dumps(d),
                0, None, 0,
            ),
        )

    conn.execute(
        "INSERT INTO scan_log (city, check_in, check_out, tier, scanned_at, deals_found, duration_s) "
        "VALUES (?,?,?,?,?,?,?)",
        (city, check_in.isoformat(), check_out.isoformat(), tier, now, len(deals), round(duration_s, 1)),
    )
    conn.commit()
    conn.close()


# ── Deal Verification ─────────────────────────────────────────────────────────

VERIFY_MIN_AGE_MINUTES = int(os.getenv("VERIFY_MIN_AGE_MINUTES", "60"))
VERIFY_SAVINGS_RATIO   = float(os.getenv("VERIFY_SAVINGS_RATIO", "0.7"))  # deal valid if savings still >= original * 0.7
VERIFY_MAX_FAILS       = int(os.getenv("VERIFY_MAX_FAILS", "2"))
VERIFY_CONCURRENCY     = int(os.getenv("VERIFY_CONCURRENCY", "2"))


def get_unverified_deals(db_path: str, min_age_minutes: int = VERIFY_MIN_AGE_MINUTES,
                         limit: int = 30) -> list:
    """Return unverified deals older than min_age_minutes with fewer than MAX_FAILS failures."""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    cutoff = (datetime.utcnow() - timedelta(minutes=min_age_minutes)).isoformat()
    rows = conn.execute(
        """SELECT id, city, check_in, check_out, tier,
                  cheapest_geo, baseline_geo,
                  cheapest_usd, baseline_usd,
                  hotel_id, hotel_name, verify_fails, savings_pct
           FROM deals
           WHERE is_verified = 0
             AND verify_fails < ?
             AND scanned_at <= ?
             AND check_in >= ?
           ORDER BY savings_pct DESC
           LIMIT ?""",
        (VERIFY_MAX_FAILS, cutoff, date.today().isoformat(), limit),
    ).fetchall()
    conn.close()
    return rows


def _mark_deal_verified(db_path: str, deal_id: int, refreshed: dict):
    """Update deal with fresh prices and mark it verified."""
    conn = sqlite3.connect(db_path)
    now = datetime.utcnow().isoformat()
    conn.execute(
        """UPDATE deals SET
               is_verified   = 1,
               verified_at   = ?,
               verify_fails  = 0,
               cheapest_usd  = ?,
               baseline_usd  = ?,
               savings_pct   = ?,
               savings_usd   = ?,
               booking_url   = ?,
               baseline_url  = ?,
               raw_json      = ?
           WHERE id = ?""",
        (
            now,
            refreshed.get("usd_price"),
            refreshed.get("baseline_usd_price"),
            refreshed.get("savings_percent"),
            refreshed.get("savings_usd"),
            refreshed.get("booking_url"),
            refreshed.get("baseline_url"),
            json.dumps(refreshed),
            deal_id,
        ),
    )
    conn.commit()
    conn.close()


def _fail_deal(db_path: str, deal_id: int):
    """Increment verify_fails; delete the deal once it hits VERIFY_MAX_FAILS."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE deals SET verify_fails = verify_fails + 1 WHERE id = ?",
        (deal_id,),
    )
    conn.execute(
        "DELETE FROM deals WHERE id = ? AND verify_fails >= ?",
        (deal_id, VERIFY_MAX_FAILS),
    )
    conn.commit()
    conn.close()


async def verify_one_deal(deal_row: tuple, db_path: str) -> bool:
    """Re-scrape both geos for a deal and confirm the price difference still holds.

    Returns True if verified, False if the deal failed/was removed.
    """
    from price_engine import PriceEngine

    try:
        from chat_service import TravelIntent
    except ImportError:
        from price_engine import TravelIntent

    (deal_id, city, check_in_str, check_out_str, tier,
     cheap_geo, exp_geo, cheap_usd, exp_usd,
     hotel_id, hotel_name, verify_fails, orig_savings_pct) = deal_row

    check_in  = date.fromisoformat(check_in_str)
    check_out = date.fromisoformat(check_out_str)

    # Expired deal — remove it
    if check_in < date.today():
        _fail_deal(db_path, deal_id)
        log.info(f"  ✗ Removed expired deal: {hotel_name} (check-in {check_in_str})")
        return False

    engine = PriceEngine()
    engine.max_pages = 2  # 2 pages (50 hotels) is enough to find the hotel

    intent = TravelIntent(
        destination_city=city,
        check_in=check_in,
        check_out=check_out,
        guests=2,
        rooms=1,
        hotel_tier=tier,
    )

    all_results: dict = {}
    try:
        async for update in engine.search_all_geos(intent, geos=[cheap_geo, exp_geo]):
            if "geo_results" in update:
                all_results[update["geo"]] = update["geo_results"]
    except Exception as e:
        log.warning(f"  ✗ Verify scrape error ({hotel_name}): {e}")
        _fail_deal(db_path, deal_id)
        return False

    # Find the specific hotel in the re-scraped results
    deals = engine.calculate_best_deals(all_results)
    match = next((d for d in deals if d.get("hotel_id") == hotel_id), None)

    if not match:
        log.info(f"  ✗ Verify: {hotel_name} no longer found in both geos — removing")
        _fail_deal(db_path, deal_id)
        return False

    new_savings = match.get("savings_percent", 0)
    threshold   = orig_savings_pct * VERIFY_SAVINGS_RATIO

    if new_savings >= threshold:
        log.info(
            f"  ✓ Verified: {hotel_name}  "
            f"{match['geo_country']}/${match['usd_price']:.0f} vs "
            f"{match['baseline_geo']}/${match['baseline_usd_price']:.0f}  "
            f"({new_savings:.1f}%)"
        )
        _mark_deal_verified(db_path, deal_id, match)
        return True
    else:
        log.info(
            f"  ✗ Verify: {hotel_name} savings dropped {orig_savings_pct:.1f}% → {new_savings:.1f}% — removing"
        )
        _fail_deal(db_path, deal_id)
        return False


async def run_verification_pass(db_path: str):
    """Verify all unverified deals older than VERIFY_MIN_AGE_MINUTES."""
    unverified = get_unverified_deals(db_path)
    if not unverified:
        log.info("Verification pass: nothing to verify yet")
        return

    log.info(f"Verification pass: {len(unverified)} deals to check")
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def bounded(row):
        async with sem:
            return await verify_one_deal(row, db_path)

    results = await asyncio.gather(*[bounded(r) for r in unverified], return_exceptions=True)
    ok   = sum(1 for r in results if r is True)
    fail = sum(1 for r in results if r is False)
    log.info(f"Verification pass complete: {ok} verified ✓  {fail} removed ✗")


async def report_deals_remote(city: str, check_in: date, check_out: date,
                               tier: str, deals: list):
    """POST deals to the central server (only used on remote scanners)."""
    if not REPORT_URL or not REPORT_TOKEN:
        return
    try:
        import aiohttp
        payload = {
            "city": city,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "tier": tier,
            "deals": deals,
        }
        headers = {"Authorization": f"Bearer {REPORT_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{REPORT_URL}/api/scanner/ingest",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Ingest HTTP {resp.status} for {city}/{check_in}/{tier}")
                else:
                    log.info(f"  → reported {len(deals)} deals to central server")
    except Exception as e:
        log.warning(f"Remote report failed: {e}")


# Geos supported by the Chrome extension (must match geo-proxy/main.go PORT_MAP)
EXTENSION_GEOS = {
    'US','IN','GB','VN','MY','SG','JP','HK','CA','FR','PL','MX','ZA','BD','PK','HU','KZ','CL','KR',
    'AE','AT','AU','CH','DE','ES','IE','IT','KE','NL','NZ',
}

def get_featured_deals(db_path: str = DB_PATH, limit: int = 20,
                       min_savings: float = MIN_SAVINGS_PCT,
                       city: Optional[str] = None,
                       verified_only: bool = False) -> List[dict]:
    """Return top deals sorted by savings % — only for future check-ins.

    Verified deals are always listed first.  Pass verified_only=True to
    exclude unverified deals.
    """
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    base = (
        "SELECT raw_json, check_in, check_out, tier, nights, city, is_verified, verified_at "
        "FROM deals "
        "WHERE savings_pct >= ? AND check_in >= ?"
    )
    params: list = [min_savings, date.today().isoformat()]

    if city:
        base += " AND city=?"
        params.append(city)
    if verified_only:
        base += " AND is_verified=1"

    # Verified deals first, then by savings %
    base += " ORDER BY is_verified DESC, savings_pct DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(base, params).fetchall()
    conn.close()
    return [d for row in rows for d in [_row_to_deal(row)] if d]


def _row_to_deal(row) -> Optional[dict]:
    """Convert a DB row to a deal dict."""
    raw, ci, co, tier, nights, city_name, is_verified, verified_at = row
    try:
        d = json.loads(raw)
        if d.get("geo_country", "").upper() not in EXTENSION_GEOS:
            return None
        d["check_in"]    = ci
        d["check_out"]   = co
        d["tier"]        = tier
        d["nights"]      = nights
        d["city"]        = city_name
        d["is_verified"] = bool(is_verified)
        d["verified_at"] = verified_at
        return d
    except Exception:
        return None


def get_scanner_stats(db_path: str = DB_PATH) -> dict:
    if not Path(db_path).exists():
        return {"total_deals": 0, "scans_24h": 0, "cities": [c for c, _ in SCAN_CITIES]}
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    scans_24h = conn.execute(
        "SELECT COUNT(*) FROM scan_log WHERE scanned_at > datetime('now','-24 hours')"
    ).fetchone()[0]
    top = conn.execute(
        "SELECT MAX(savings_pct) FROM deals WHERE check_in >= ?",
        (date.today().isoformat(),),
    ).fetchone()[0]
    city_counts = conn.execute(
        "SELECT city, COUNT(*) FROM deals WHERE check_in >= ? GROUP BY city ORDER BY COUNT(*) DESC",
        (date.today().isoformat(),),
    ).fetchall()
    conn.close()
    return {
        "cities": [c for c, _ in SCAN_CITIES],
        "total_deals": total,
        "scans_24h": scans_24h,
        "top_savings_pct": round(top, 1) if top else None,
        "deals_by_city": {row[0]: row[1] for row in city_counts},
    }


# ── Scan logic ────────────────────────────────────────────────────────────────

def build_date_combos(city: str, country: str) -> list:
    """Generate (city, country, check_in, check_out, tier) for the next SCAN_MONTHS months."""
    combos = []
    today = date.today()
    start = today + timedelta(days=7)
    end = today + timedelta(days=SCAN_MONTHS * 30)
    d = start
    while d <= end:
        for nights in STAY_DURATIONS:
            check_out = d + timedelta(days=nights)
            for tier in HOTEL_TIERS:
                combos.append((city, country, d, check_out, tier))
        d += timedelta(days=DATE_INTERVAL_DAYS)
    return combos


def _build_raw_matrix(all_results: dict) -> dict:
    """Build {hotel_id: {geo: {usd, block}}} from all_results for debug logging."""
    matrix = {}
    for geo, hotels in all_results.items():
        for h in hotels:
            hid = h.get("hotel_id", "")
            if not hid:
                continue
            if hid not in matrix:
                matrix[hid] = {"name": h.get("hotel_name", ""), "prices": {}}
            matrix[hid]["prices"][geo] = {
                "usd": h.get("usd_price"),
                "block": h.get("room_type"),
            }
    return matrix


async def scan_one_combo(city: str, country: str, check_in: date, check_out: date,
                         tier: str, db_path: str):
    """Run a full geo-scan for one city/date/tier combo and persist deals."""
    from price_engine import PriceEngine

    try:
        from chat_service import TravelIntent
    except ImportError:
        from price_engine import TravelIntent  # fallback

    engine = PriceEngine()
    engine.max_pages = MAX_PAGES
    intent = TravelIntent(
        destination_city=city,
        destination_country=country,
        check_in=check_in,
        check_out=check_out,
        guests=2,
        rooms=1,
        hotel_tier=tier,
    )

    t0 = datetime.utcnow()
    all_results: dict = {}
    try:
        async for update in engine.search_all_geos(intent):
            if "geo_results" in update:
                all_results[update["geo"]] = update["geo_results"]
    except Exception as e:
        log.warning(f"Scan error {city}/{check_in}/{tier}: {e}")
        return

    duration_s = (datetime.utcnow() - t0).total_seconds()
    deals = engine.calculate_best_deals(all_results, baseline_geo="US")
    good_deals = [d for d in deals if (d.get("savings_percent") or 0) >= MIN_SAVINGS_PCT]

    # ── Debug: save full price matrix for every hotel in this scan ──────────
    debug_path = os.getenv("SCAN_DEBUG_LOG", "/tmp/geoprice_scan_debug.jsonl")
    try:
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "city": city,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "tier": tier,
            "geos_scraped": list(all_results.keys()),
            "hotels_per_geo": {g: len(v) for g, v in all_results.items()},
            "deals_found": len(good_deals),
            "deals": [
                {
                    "hotel": d.get("hotel_name"),
                    "hotel_id": d.get("hotel_id"),
                    "cheap_geo": d.get("geo_country"),
                    "cheap_usd": d.get("usd_price"),
                    "exp_geo": d.get("baseline_geo"),
                    "exp_usd": d.get("baseline_usd_price"),
                    "us_price": d.get("us_price"),
                    "savings_pct": d.get("savings_percent"),
                    "all_prices": d.get("all_geo_prices", {}),
                }
                for d in good_deals
            ],
            "raw_matrix": _build_raw_matrix(all_results),
        }
        with open(debug_path, "a") as dbf:
            dbf.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"Debug log write failed: {e}")

    if REPORT_URL and REPORT_TOKEN:
        # Remote scanner: report to central server, don't write locally
        await report_deals_remote(city, check_in, check_out, tier, good_deals)
    else:
        save_deals(db_path, city, check_in, check_out, tier, good_deals, duration_s)

    log.info(
        f"  {city:15s} {check_in} +{(check_out-check_in).days}n {tier:12s} → "
        f"{len(good_deals)} deals  ({duration_s:.0f}s)"
    )


async def run_scan_pass(db_path: str):
    """Single pass: scan all city/date/tier combos not refreshed recently."""
    # Build per-city combo lists then interleave round-robin so every city
    # gets its first combo scanned before any city gets its second, etc.
    per_city = [build_date_combos(city, country) for city, country in SCAN_CITIES]
    all_combos = [
        combo
        for round_combos in itertools.zip_longest(*per_city)
        for combo in round_combos
        if combo is not None
    ]

    log.info(f"Starting scan pass: {len(all_combos)} combos across {len(SCAN_CITIES)} cities")

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def bounded(city, country, ci, co, tier):
        if _combo_last_scanned(db_path, city, ci, co, tier):
            return  # still fresh
        async with sem:
            await scan_one_combo(city, country, ci, co, tier, db_path)

    await asyncio.gather(
        *[bounded(city, country, ci, co, t) for city, country, ci, co, t in all_combos],
        return_exceptions=True,
    )
    log.info("Scan pass complete.")


async def run_daemon(db_path: str):
    """Continuous daemon: scan → verify every 30 min during sleep → repeat."""
    init_db(db_path)
    city_list = ", ".join(c for c, _ in SCAN_CITIES)
    log.info(f"Deal scanner daemon started. Cities: {city_list}  DB={db_path}")
    if REPORT_URL:
        log.info(f"Remote reporting enabled → {REPORT_URL}")
    while True:
        # Run scan pass, then immediately verify any deals already in DB
        await run_scan_pass(db_path)
        await run_verification_pass(db_path)

        # During the inter-pass sleep, keep verifying new deals every 30 min
        log.info(f"Sleeping {LOOP_PAUSE_HOURS}h before next scan pass…")
        sleep_secs   = int(LOOP_PAUSE_HOURS * 3600)
        verify_every = 1800  # 30 minutes
        elapsed      = 0
        while elapsed < sleep_secs:
            chunk    = min(verify_every, sleep_secs - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk
            if elapsed < sleep_secs:
                await run_verification_pass(db_path)


# ── FastAPI app ───────────────────────────────────────────────────────────────

def create_scanner_app(db_path: str = DB_PATH, start_daemon: bool = True):
    from fastapi import FastAPI, Query
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="GeoPrice Deal Scanner", version="2.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/featured-deals")
    async def featured_deals(
        limit: int = Query(20, ge=1, le=100),
        min_savings: float = Query(MIN_SAVINGS_PCT),
        city: Optional[str] = Query(None),
    ):
        deals = get_featured_deals(db_path, limit, min_savings, city)
        return {"deals": deals, "cities": [c for c, _ in SCAN_CITIES]}

    @app.get("/api/scanner-stats")
    async def scanner_stats():
        return get_scanner_stats(db_path)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "cities": [c for c, _ in SCAN_CITIES], "db": db_path}

    if start_daemon:
        @app.on_event("startup")
        async def _start_daemon():
            init_db(db_path)
            asyncio.create_task(run_daemon(db_path))

    return app


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daemon"

    if mode == "serve":
        import uvicorn
        app = create_scanner_app(DB_PATH, start_daemon=False)
        uvicorn.run(app, host="0.0.0.0", port=8767)

    elif mode == "serve-bg":
        import uvicorn
        app = create_scanner_app(DB_PATH, start_daemon=True)
        uvicorn.run(app, host="0.0.0.0", port=8767)

    elif mode == "scan-once":
        init_db(DB_PATH)
        asyncio.run(run_scan_pass(DB_PATH))

    else:  # daemon
        asyncio.run(run_daemon(DB_PATH))
