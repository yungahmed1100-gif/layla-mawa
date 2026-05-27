from __future__ import annotations
"""
Mawa.om listing scraper — card-based, no Playwright needed.
Run: python scrape_mawa.py [--limit N]
"""

import argparse
import asyncio
import json
import logging
import re
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

import os

MAWA_BASE_URL = os.getenv("MAWA_BASE_URL", "https://www.mawa.om").rstrip("/")
SCRAPER_ENABLED = os.getenv("SCRAPER_ENABLED", "true").lower() == "true"
DELAY = float(os.getenv("MAWA_REQUEST_DELAY_SECONDS", "5"))
USER_AGENT = os.getenv("MAWA_USER_AGENT", "BznsflowResearchBot/1.0 (+contact@bznsflow.com)")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("scraper")

_shutdown = False


def handle_sigint(sig, frame):
    global _shutdown
    log.info("Interrupted — shutting down cleanly.")
    _shutdown = True


signal.signal(signal.SIGINT, handle_sigint)


# ── Location / type mappings ──────────────────────────────────────────────────

LOCATION_MAP: dict[str, str] = {
    "الموج": "al-mouj",
    "القرم": "qurum",
    "خليج مسقط": "muscat-bay",
    "مسقط هيلز": "muscat-hills",
    "الخوير": "al-khuwair",
    "العذيبة": "al-athaibah",
    "بوشر": "bausher",
    "الوادي الكبير": "wadi-al-kabeer",
    "الموالح": "mawaleh",
    "الخوض": "al-khoud",
    "غلاء": "ghala",
    "السيب": "seeb",
    "الحيل": "al-hail",
    "الغبرة": "al-ghubra",
    "مدينة السلطان قابوس": "madinat-sultan-qaboos",
    "شاطئ القرم": "shati-al-qurum",
    "العامرات": "al-amerat",
    "قريات": "quriyat",
    "مطرح": "muttrah",
    "صحار": "sohar",
    "صلالة": "salalah",
    "نزوى": "nizwa",
    "روي": "ruwi",
    "الحمرية": "al-hamriya",
    "الرسيل": "al-rusayl",
    "عبري": "ibri",
    "البريمي": "buraimi",
}

TYPE_MAP: dict[str, str] = {
    "شقة": "apartment",
    "فيلا": "villa",
    "توين فيلا": "twin-villa",
    "تاون هاوس": "townhouse",
    "مكاتب بمساحات مفتوحة": "office",
    "مساحات مكتبية بالخدمات": "office",
    "مكتب": "office",
    "محل": "commercial",
    "معرض": "commercial",
    "تجاري": "commercial",
    "فيلا تجارية": "commercial",
    "أرض": "land",
    "مخزن": "commercial",
    "غرفة": "apartment",
    "مبنى سكني": "building",
}


def _map_location(ar: str) -> str | None:
    for key, slug in LOCATION_MAP.items():
        if key in ar:
            return slug
    return None


def _map_type(ar: str) -> str | None:
    for key, norm in TYPE_MAP.items():
        if key in ar:
            return norm
    return ar.strip() or None


def _url_to_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1]
    m = re.search(r"-(\d+)$", slug)
    return m.group(1) if m else slug


def _parse_price(text: str) -> float | None:
    m = re.search(r"[\d,]+", text.replace(",", ""))
    return float(m.group().replace(",", "")) if m else None


def _parse_area(text: str) -> float | None:
    m = re.search(r"([\d.]+)", text)
    return float(m.group(1)) if m else None


def _parse_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


# ── Card parser ───────────────────────────────────────────────────────────────

def parse_card(card, transaction: str) -> dict | None:
    # URL & ID
    link = card.select_one(".prop_img a[href]")
    if not link:
        return None
    url = link["href"]
    mawa_id = _url_to_id(url)

    # Title
    h3 = card.select_one("h3")
    title_ar = h3.get("title", "").strip() if h3 else (h3.get_text(strip=True) if h3 else None)

    # Location
    loc_p = card.select_one(".view_map_flag p")
    location_ar = loc_p.get_text(strip=True) if loc_p else None
    location = _map_location(location_ar) if location_ar else None

    # Price
    price_el = card.select_one(".price p")
    price_omr = _parse_price(price_el.get_text()) if price_el else None

    # Beds / Baths
    bedrooms = None
    bathrooms = None
    for li in card.select(".square ul li p"):
        text = li.get_text(strip=True)
        if "fa-bed" in str(li) or li.find("i", class_="fa-bed"):
            bedrooms = _parse_int(text)
        elif "fa-bath" in str(li) or li.find("i", class_="fa-bath"):
            bathrooms = _parse_int(text)
    # Icon classes live on <i> inside <p>
    for p in card.select(".square ul li p"):
        i = p.find("i")
        if not i:
            continue
        classes = " ".join(i.get("class", []))
        val = _parse_int(p.get_text(strip=True))
        if "fa-bed" in classes:
            bedrooms = val
        elif "fa-bath" in classes:
            bathrooms = val

    # Area & type
    area_sqm = None
    property_type = None
    for p in card.select(".square > p"):
        i = p.find("i")
        if not i:
            continue
        classes = " ".join(i.get("class", []))
        text = p.get_text(strip=True)
        if "fa-area-chart" in classes:
            area_sqm = _parse_area(text)
        elif "fa-building-o" in classes:
            property_type = _map_type(text)

    # Image
    img = card.select_one(".prop_img img[src]")
    image_url = img["src"] if img else None

    # Phone
    phone_a = card.select_one('a[href^="tel:"]')
    agent_phone = phone_a["href"].replace("tel:", "") if phone_a else None

    # WhatsApp from onclick
    wa_a = card.select_one(".whats_app a[onclick]")
    agent_whatsapp = None
    if wa_a:
        m = re.search(r"phone=([+\d]+)", wa_a.get("onclick", ""))
        if m:
            agent_whatsapp = m.group(1)

    return {
        "mawa_id": mawa_id,
        "url": url,
        "title_ar": title_ar,
        "title_en": None,
        "transaction": transaction,
        "property_type": property_type,
        "location_ar": location_ar,
        "location": location,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "area_sqm": area_sqm,
        "price_omr": price_omr,
        "furnished": None,
        "description_ar": None,
        "description_en": None,
        "image_urls": [image_url] if image_url else [],
        "agent_name": None,
        "agent_phone": agent_phone,
        "agent_whatsapp": agent_whatsapp,
    }


# ── Audit DB ──────────────────────────────────────────────────────────────────

def audit_db() -> sqlite3.Connection:
    conn = sqlite3.connect("scrape_audit.db")
    conn.execute(
        """create table if not exists scrape_log (
            id        integer primary key autoincrement,
            url       text,
            ts        text,
            status    text,
            bytes     integer
        )"""
    )
    conn.commit()
    return conn


def audit_write(conn: sqlite3.Connection, url: str, status: str, size: int):
    conn.execute(
        "insert into scrape_log (url, ts, status, bytes) values (?,?,?,?)",
        (url, datetime.now(timezone.utc).isoformat(), status, size),
    )
    conn.commit()


# ── Supabase upsert ───────────────────────────────────────────────────────────

def supabase_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=SUPABASE_URL,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=30,
    )


async def upsert_listing(sb: httpx.AsyncClient, listing: dict):
    payload = {k: v for k, v in listing.items() if v is not None}
    payload["scraped_at"] = datetime.now(timezone.utc).isoformat()
    payload["is_active"] = True

    if "image_urls" in payload and isinstance(payload["image_urls"], list):
        payload["image_urls"] = json.dumps(payload["image_urls"])

    r = await sb.post(
        "/rest/v1/listings",
        json=payload,
        headers={
            "Prefer": "resolution=merge-duplicates,return=minimal",
            "Content-Type": "application/json",
        },
    )
    if r.status_code not in (200, 201):
        log.error("Supabase upsert failed %s: %s %s", listing.get("mawa_id"), r.status_code, r.text[:200])
    else:
        log.debug("Upserted %s", listing.get("mawa_id"))


async def mark_stale(sb: httpx.AsyncClient, seen_ids: list[str]):
    """Mark scraped listings not seen in this run as inactive.
    CSV-imported rows (mawa_id starting with 'csv-') are never touched.
    """
    if not seen_ids:
        return
    ids_csv = ",".join(seen_ids)
    # Two filters via repeated query params; PostgREST ANDs them.
    # not.like.csv-% excludes CSV-imported seed rows from being marked stale.
    r = await sb.patch(
        f"/rest/v1/listings?mawa_id=not.in.({ids_csv})&mawa_id=not.like.csv-%25",
        json={"is_active": False},
        headers={"Content-Type": "application/json"},
    )
    if r.status_code not in (200, 204):
        log.warning("mark_stale failed: %s %s", r.status_code, r.text[:200])
    else:
        log.info("Marked stale scraped listings inactive (kept %d active)", len(seen_ids))


# ── Scrape one section (rent or sale) ─────────────────────────────────────────

async def scrape_section(
    client: httpx.AsyncClient,
    sb: httpx.AsyncClient,
    audit: sqlite3.Connection,
    section: str,
    prop_for: str,
    transaction: str,
    limit: int | None,
) -> list[str]:
    seen_ids: list[str] = []

    # Get CSRF token + session cookie
    try:
        r = await client.get(f"{MAWA_BASE_URL}{section}")
        soup = BeautifulSoup(r.text, "html.parser")
        token_tag = soup.find("meta", {"name": "csrf-token"})
        csrf = token_tag["content"] if token_tag else ""
    except Exception as e:
        log.error("Could not load %s: %s", section, e)
        return seen_ids

    log.info("Scraping %s (prop_for=%s)", section, prop_for)
    page = 1

    while True:
        if _shutdown:
            break
        if limit and len(seen_ids) >= limit:
            break

        try:
            r = await client.post(
                f"{MAWA_BASE_URL}/ar/PropertyListing",
                data={"property_for": prop_for, "page": str(page), "_token": csrf},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{MAWA_BASE_URL}{section}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except Exception as e:
            log.warning("Request error page %d: %s", page, e)
            break

        if r.status_code != 200:
            log.warning("Non-200 on page %d: %s", page, r.status_code)
            break

        text = r.text.strip()
        if not text or "feature_inner" not in text:
            log.info("No more listings on page %d", page)
            break

        soup = BeautifulSoup(text, "html.parser")
        cards = soup.select(".feature_inner")
        if not cards:
            break

        for card in cards:
            if _shutdown:
                break
            if limit and len(seen_ids) >= limit:
                break

            listing = parse_card(card, transaction)
            if not listing or not listing.get("mawa_id"):
                continue

            await upsert_listing(sb, listing)
            seen_ids.append(listing["mawa_id"])
            audit_write(audit, listing["url"], "ok", len(text))
            log.info("[%s p%d] %s — %s OMR", transaction, page, listing.get("title_ar", "")[:40], listing.get("price_omr"))

        log.info("%s page %d — %d listings so far", section, page, len(seen_ids))
        page += 1

        if not _shutdown:
            await asyncio.sleep(DELAY)

    return seen_ids


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_once(limit: int | None, audit: sqlite3.Connection) -> int:
    """Run one full scrape cycle. Returns number of listings upserted."""
    seen_ids: list[str] = []

    async with supabase_client() as sb:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30,
        ) as client:
            for section, prop_for, transaction in [
                ("/ar/rent", "3", "rent"),
                ("/ar/sale", "4", "sale"),
            ]:
                if _shutdown:
                    break
                ids = await scrape_section(client, sb, audit, section, prop_for, transaction, limit)
                seen_ids.extend(ids)
                log.info("Section %s done — %d listings", section, len(ids))

        if seen_ids and not _shutdown:
            await mark_stale(sb, seen_ids)

    log.info("Scrape cycle complete. %d listings total.", len(seen_ids))
    return len(seen_ids)


async def main(limit: int | None, interval: int | None):
    if not SCRAPER_ENABLED:
        log.info("SCRAPER_ENABLED=false — exiting.")
        return

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        sys.exit(1)

    audit = audit_db()
    run = 0

    while True:
        run += 1
        log.info("=== Scrape run #%d started ===", run)
        await run_once(limit, audit)

        if _shutdown or interval is None:
            break

        log.info("Next run in %d minutes. Ctrl+C to stop.", interval)
        for _ in range(interval * 60):
            if _shutdown:
                break
            await asyncio.sleep(1)

    audit.close()
    log.info("Scraper stopped after %d run(s).", run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mawa.om listing scraper")
    parser.add_argument("--limit", type=int, default=None, help="Max listings per run")
    parser.add_argument(
        "--interval", type=int, default=None,
        help="Re-scrape interval in minutes (omit to run once)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.interval))
