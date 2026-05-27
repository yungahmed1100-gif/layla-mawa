"""
Import Mawa CSV exports into Supabase listings table.
Run: python import_csv.py
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
from datetime import datetime, timezone
from hashlib import md5
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_csv")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

RENT_CSV = "properties-list_properties-for-rent_captured-list_2026-05-25_17-08-51_019e5f40-247f-7411-92a6-9ec42884d7ad.csv"
LISTINGS_CSV = "properties-list_real-estate-listings_captured-list_2026-05-25_17-08-51_019e5f40-247f-7411-92a6-9ec42884d7ad.csv"

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
    "الانصب": "al-ansab",
    "البريمي": "buraimi",
}

TYPE_MAP: dict[str, str] = {
    "شقة": "apartment",
    "فيلا": "villa",
    "توين فيلا": "twin-villa",
    "تاون هاوس": "townhouse",
    "مكاتب": "office",
    "مساحات مكتبية": "office",
    "مكتب": "office",
    "محل": "commercial",
    "معرض": "commercial",
    "تجاري": "commercial",
    "فيلا تجارية": "commercial",
    "أرض": "land",
    "مخزن": "commercial",
    "غرفة": "apartment",
    "مبنى": "building",
}


def map_location(ar: str) -> str | None:
    for key, slug in LOCATION_MAP.items():
        if key in ar:
            return slug
    return None


def map_type(ar: str) -> str | None:
    for key, norm in TYPE_MAP.items():
        if key in ar:
            return norm
    return ar.strip() or None


def parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_beds_baths(text: str) -> tuple[int | None, int | None]:
    nums = re.findall(r"\d+", text or "")
    beds = int(nums[0]) if len(nums) > 0 else None
    baths = int(nums[1]) if len(nums) > 1 else None
    return beds, baths


def url_to_id(url: str) -> str:
    if not url:
        return ""
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1]
    m = re.search(r"-(\d+)$", slug)
    return m.group(1) if m else slug


def infer_transaction(url: str) -> str:
    if "/rent" in url.lower():
        return "rent"
    if "/sale" in url.lower() or "/buy" in url.lower():
        return "sale"
    return "sale"


def read_listings_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("Property URL") or "").strip()
            mawa_id = url_to_id(url) if url else md5(json.dumps(row).encode()).hexdigest()[:12]
            if not mawa_id:
                continue

            location_ar = (row.get("Location") or "").strip()
            price_text = row.get("Price") or ""
            beds, baths = parse_beds_baths(row.get("Bedrooms and Bathrooms") or "")
            prop_type_ar = (row.get("Property Type") or "").strip()
            image = (row.get("Property Image") or "").strip()
            phone_raw = (row.get("Contact Number") or "").strip()
            phone = phone_raw.replace("tel:", "").strip() if phone_raw else None
            title_en = (row.get("Property Title") or "").strip() or None

            rows.append({
                "mawa_id": mawa_id,
                "url": url or None,
                "title_en": title_en,
                "title_ar": None,
                "transaction": infer_transaction(url),
                "property_type": map_type(prop_type_ar),
                "location_ar": location_ar or None,
                "location": map_location(location_ar) if location_ar else None,
                "bedrooms": beds,
                "bathrooms": baths,
                "price_omr": parse_price(price_text),
                "furnished": None,
                "image_urls": json.dumps([image]) if image else json.dumps([]),
                "agent_phone": phone,
                "agent_whatsapp": phone,
            })
    return rows


def read_rent_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            location_ar = (row.get("Location-2") or "").strip()
            # Price is in "Property Type-2" column due to CSV column shift
            price_text = (row.get("Property Type-2") or row.get("Price-2") or "").strip()
            image = (row.get("Company Logo-2") or "").strip()

            price = parse_price(price_text)
            if not price:
                continue

            mawa_id = f"csv-rent-{i+1}"
            rows.append({
                "mawa_id": mawa_id,
                "url": f"https://www.mawa.om/ar/rent#{mawa_id}",
                "title_en": None,
                "title_ar": None,
                "transaction": "rent",
                "property_type": None,
                "location_ar": location_ar or None,
                "location": map_location(location_ar) if location_ar else None,
                "bedrooms": None,
                "bathrooms": None,
                "price_omr": price,
                "furnished": None,
                "image_urls": json.dumps([image]) if image else json.dumps([]),
                "agent_phone": None,
                "agent_whatsapp": None,
            })
    return rows


def supabase_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=SUPABASE_URL,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=30,
    )


async def upsert_batch(sb: httpx.AsyncClient, rows: list[dict]):
    for row in rows:
        payload = {k: v for k, v in row.items() if v is not None}
        payload["scraped_at"] = datetime.now(timezone.utc).isoformat()
        payload["is_active"] = True

        r = await sb.post(
            "/rest/v1/listings",
            json=payload,
            headers={
                "Prefer": "resolution=merge-duplicates,return=minimal",
                "Content-Type": "application/json",
            },
        )
        if r.status_code not in (200, 201):
            log.error("Upsert failed %s: %s %s", row.get("mawa_id"), r.status_code, r.text[:200])
        else:
            log.info("Upserted %s — %s (%s OMR)", row["mawa_id"], row.get("location"), row.get("price_omr"))


async def main():
    listings = read_listings_csv(LISTINGS_CSV)
    rent = read_rent_csv(RENT_CSV)

    # Deduplicate by mawa_id (listings CSV takes priority)
    seen: set[str] = set()
    all_rows: list[dict] = []
    for row in listings + rent:
        mid = row["mawa_id"]
        if mid not in seen:
            seen.add(mid)
            all_rows.append(row)

    log.info("Total rows to upsert: %d (%d listings + %d rent, %d dupes dropped)",
             len(all_rows), len(listings), len(rent), len(listings) + len(rent) - len(all_rows))

    async with supabase_client() as sb:
        await upsert_batch(sb, all_rows)

    log.info("Import complete.")


if __name__ == "__main__":
    asyncio.run(main())
