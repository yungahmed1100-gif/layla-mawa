from __future__ import annotations
"""
Mawa Concierge — WhatsApp bot.
Run: uvicorn bot:app --host 0.0.0.0 --port ${APP_PORT:-8000}
"""

import json
import logging
import os
import re
import unicodedata
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, Response
from groq import AsyncGroq
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from prompts import SYSTEM_PROMPT_AR, SYSTEM_PROMPT_EN, TOOLS

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("bot")

# ── Config ────────────────────────────────────────────────────────────────────

APP_ENV = os.getenv("APP_ENV", "development")
BOT_ENABLED = os.getenv("BOT_ENABLED", "true").lower() == "true"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_WEBHOOK_VALIDATE = os.getenv("TWILIO_WEBHOOK_VALIDATE", "true").lower() == "true"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.2"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

LEAD_NOTIFICATION_WHATSAPP = os.getenv("LEAD_NOTIFICATION_WHATSAPP", "")

GROQ_TIMEOUT = 15  # seconds


# ── Clients ───────────────────────────────────────────────────────────────────

groq_client: AsyncGroq | None = None
twilio_client: TwilioClient | None = None
sb_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global groq_client, twilio_client, sb_client
    if GROQ_API_KEY:
        groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        sb_client = httpx.AsyncClient(
            base_url=SUPABASE_URL,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=10,
        )
    log.info("Bot started (env=%s, validate=%s)", APP_ENV, TWILIO_WEBHOOK_VALIDATE)
    yield
    if sb_client:
        await sb_client.aclose()


app = FastAPI(title="Mawa Concierge", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook/twilio")
async def webhook_twilio(
    request: Request,
    background_tasks: BackgroundTasks,
    MessageSid: str = Form(default=""),
    From: str = Form(default=""),
    Body: str = Form(default=""),
):
    if not BOT_ENABLED:
        return Response(content="<Response/>", media_type="text/xml")

    # Validate Twilio signature
    if TWILIO_WEBHOOK_VALIDATE and TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        url = str(request.url)
        form_data = dict(await request.form())
        sig = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(url, form_data, sig):
            log.warning("Invalid Twilio signature from %s", From)
            raise HTTPException(status_code=403, detail="Invalid signature")

    background_tasks.add_task(process_message, MessageSid, From, Body)
    return Response(content="<Response/>", media_type="text/xml")


# ── Core processing ───────────────────────────────────────────────────────────

async def process_message(message_sid: str, phone: str, body: str):
    log.info("Processing message from %s sid=%s", phone, message_sid)
    try:
        lead = await load_or_create_lead(phone)
        if not lead:
            log.error("Could not load/create lead for %s", phone)
            return

        # Dedupe
        sids = lead.get("twilio_message_sids") or []
        if message_sid in sids:
            log.info("Duplicate message_sid %s — skipping", message_sid)
            return

        # Detect language from first user message
        lang = lead.get("language") or detect_language(body)

        # Append incoming message to conversation log
        conv_log = lead.get("conversation_log") or []
        conv_log.append({"role": "user", "content": body})

        # Update lead with new sid and message
        await update_lead(lead["id"], {
            "twilio_message_sids": sids + [message_sid],
            "conversation_log": conv_log,
            "language": lang,
        })

        # Run LLM
        try:
            reply, image_url = await run_llm(lead, conv_log, lang)
        except TimeoutError:
            reply = (
                "عذراً، حدث تأخير. سيتواصل معك أحد وكلائنا قريباً."
                if lang == "ar"
                else "Sorry for the delay — one of our agents will reach out to you shortly."
            )
            image_url = None
            await capture_lead_data(lead["id"], {})

        # Append assistant reply to log
        conv_log.append({"role": "assistant", "content": reply})
        await update_lead(lead["id"], {"conversation_log": conv_log})

        # Send reply via Twilio
        await send_whatsapp(phone, reply, image_url)

    except Exception as e:
        log.exception("Unhandled error processing message from %s: %s", phone, e)


async def run_llm(lead: dict, conv_log: list, lang: str) -> tuple[str, str | None]:
    system_prompt = SYSTEM_PROMPT_AR if lang == "ar" else SYSTEM_PROMPT_EN
    messages = [{"role": "system", "content": system_prompt}] + conv_log

    last_image_url: str | None = None
    max_iterations = 3

    for iteration in range(max_iterations):
        try:
            response = await groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=GROQ_TEMPERATURE,
                timeout=GROQ_TIMEOUT,
            )
        except Exception as e:
            if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                raise TimeoutError("Groq timeout") from e
            raise

        choice = response.choices[0]
        msg = choice.message

        # No tool call — final text reply
        if not msg.tool_calls:
            return msg.content or "", last_image_url

        # Process tool calls
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]})

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            if fn_name == "search_listings":
                result, image_url = await tool_search_listings(fn_args, lang)
                if image_url:
                    last_image_url = image_url
            elif fn_name == "capture_lead":
                result = await tool_capture_lead(lead, fn_args)
            else:
                result = {"error": f"Unknown tool: {fn_name}"}

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # Exceeded max iterations — ask LLM for final answer without tools
    messages.append({"role": "user", "content": "Please give your final answer now."})
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=GROQ_TEMPERATURE,
            timeout=GROQ_TIMEOUT,
        )
        return response.choices[0].message.content or "", last_image_url
    except Exception:
        raise TimeoutError("LLM loop exhausted")


# ── Tool implementations ──────────────────────────────────────────────────────

async def _run_search(filters: list[str]) -> list[dict]:
    select_cols = "mawa_id,url,title_ar,title_en,transaction,property_type,location,location_ar,bedrooms,bathrooms,area_sqm,price_omr,furnished,image_urls,agent_name,agent_phone,agent_whatsapp"
    query = "&".join(filters)
    r = await sb_client.get(
        f"/rest/v1/listings?{query}&select={select_cols}&order=price_omr.asc&limit=3"
    )
    if r.status_code != 200:
        log.error("Supabase search error: %s %s", r.status_code, r.text[:200])
        return []
    return r.json()


async def tool_search_listings(args: dict, lang: str) -> tuple[dict, str | None]:
    transaction = args.get("transaction")
    location = args.get("location", "").lower().strip()
    bedrooms = args.get("bedrooms")
    max_budget = args.get("max_budget_omr")
    property_type = args.get("property_type", "").lower().strip()

    base: list[str] = ["is_active=eq.true"]
    if transaction:
        base.append(f"transaction=eq.{transaction}")
    if property_type:
        base.append(f"property_type=ilike.*{property_type}*")

    # Build fallback ladder: exact → drop budget → drop bedrooms → location only → transaction only
    attempts = []
    exact = list(base)
    if bedrooms is not None:
        exact.append(f"bedrooms=eq.{bedrooms}")
    if max_budget is not None:
        exact.append(f"price_omr=lte.{max_budget}")
    if location:
        exact.append(f"location=ilike.*{location}*")
    attempts.append(("exact", exact))

    if max_budget is not None:
        no_budget = [f for f in exact if not f.startswith("price_omr")]
        attempts.append(("no_budget", no_budget))

    if bedrooms is not None:
        no_beds = [f for f in exact if not f.startswith("bedrooms") and not f.startswith("price_omr")]
        attempts.append(("no_beds_no_budget", no_beds))

    if location:
        location_only = list(base) + [f"location=ilike.*{location}*"]
        attempts.append(("location_only", location_only))

    attempts.append(("transaction_only", list(base)))

    listings: list[dict] = []
    matched_attempt = "exact"
    try:
        for label, filters in attempts:
            listings = await _run_search(filters)
            matched_attempt = label
            if listings:
                break
    except Exception as e:
        log.error("search_listings DB error: %s", e)
        return {"listings": [], "count": 0, "error": str(e)}, None

    if not listings:
        msg = "لم أجد عقارات متاحة حالياً." if lang == "ar" else "No listings available at the moment."
        return {"listings": [], "count": 0, "message": msg}, None

    # Tell the LLM which criteria were relaxed so it can mention it naturally
    relaxation_note = ""
    if matched_attempt == "no_budget":
        relaxation_note = "تجاوزت الميزانية المطلوبة" if lang == "ar" else "budget exceeded requested limit"
    elif matched_attempt == "no_beds_no_budget":
        relaxation_note = "عدد الغرف أو الميزانية مختلف" if lang == "ar" else "bedroom count or budget differs"
    elif matched_attempt == "location_only":
        relaxation_note = "نوع العقار أو عدد الغرف مختلف" if lang == "ar" else "property type or bedrooms differ"
    elif matched_attempt == "transaction_only":
        relaxation_note = "الموقع مختلف" if lang == "ar" else "different location"

    # Format listings for LLM
    formatted = []
    for lst in listings[:3]:
        title = (
            lst.get("title_ar") or lst.get("title_en")
            if lang == "ar"
            else (lst.get("title_en") or lst.get("title_ar"))
        )
        card = {
            "id": lst["mawa_id"],
            "title": title,
            "transaction": lst.get("transaction"),
            "type": lst.get("property_type"),
            "location_ar": lst.get("location_ar"),
            "location_slug": lst.get("location"),
            "bedrooms": lst.get("bedrooms"),
            "bathrooms": lst.get("bathrooms"),
            "area_sqm": lst.get("area_sqm"),
            "price_omr": lst.get("price_omr"),
            "furnished": lst.get("furnished"),
            "url": lst.get("url"),
            "agent_phone": lst.get("agent_phone"),
        }
        formatted.append(card)

    first_image: str | None = None
    for lst in listings[:3]:
        imgs = lst.get("image_urls") or []
        if isinstance(imgs, str):
            try:
                imgs = json.loads(imgs)
            except Exception:
                imgs = []
        if imgs:
            first_image = imgs[0]
            break

    result: dict = {"listings": formatted, "count": len(formatted)}
    if relaxation_note:
        result["note"] = relaxation_note
    return result, first_image


async def tool_capture_lead(lead: dict, args: dict) -> dict:
    name = args.get("name", "").strip()
    listing_id = args.get("interested_listing_id")

    updates: dict = {"status": "qualified"}
    if name:
        updates["name"] = name
    if listing_id:
        updates["interested_listing_id"] = listing_id

    await update_lead(lead["id"], updates)

    # Send notification to lead owner
    if LEAD_NOTIFICATION_WHATSAPP and twilio_client:
        notif = (
            f"🏠 New Mawa Lead\n"
            f"Name: {name or 'Unknown'}\n"
            f"Phone: {lead.get('phone_number', '')}\n"
            f"Listing: {listing_id or 'N/A'}\n"
            f"Language: {lead.get('language', 'ar')}"
        )
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=LEAD_NOTIFICATION_WHATSAPP,
                body=notif,
            )
            log.info("Lead notification sent for %s", lead.get("phone_number"))
        except Exception as e:
            log.error("Failed to send lead notification: %s", e)

    return {"captured": True, "name": name, "listing_id": listing_id}


# ── Supabase helpers ──────────────────────────────────────────────────────────

async def load_or_create_lead(phone: str) -> dict | None:
    r = await sb_client.get(
        f"/rest/v1/leads?phone_number=eq.{quote(phone, safe='')}&limit=1"
    )
    if r.status_code != 200:
        log.error("Error fetching lead: %s %s", r.status_code, r.text[:200])
        return None

    rows = r.json()
    if rows:
        return rows[0]

    # Create new lead
    r = await sb_client.post(
        "/rest/v1/leads",
        json={"phone_number": phone},
        headers={"Prefer": "return=representation", "Content-Type": "application/json"},
    )
    if r.status_code not in (200, 201):
        log.error("Error creating lead: %s %s", r.status_code, r.text[:200])
        return None
    rows = r.json()
    return rows[0] if rows else None


async def update_lead(lead_id: str, updates: dict):
    if not updates:
        return
    r = await sb_client.patch(
        f"/rest/v1/leads?id=eq.{lead_id}",
        json=updates,
        headers={"Content-Type": "application/json"},
    )
    if r.status_code not in (200, 204):
        log.error("Error updating lead %s: %s %s", lead_id, r.status_code, r.text[:200])


async def capture_lead_data(lead_id: str, args: dict):
    await update_lead(lead_id, {"status": "timeout_fallback"})


# ── Twilio send ───────────────────────────────────────────────────────────────

async def send_whatsapp(to: str, body: str, media_url: str | None = None):
    if not twilio_client:
        log.warning("Twilio not configured — would send to %s: %s", to, body[:80])
        return
    try:
        params = {
            "from_": TWILIO_WHATSAPP_FROM,
            "to": to,
            "body": body,
        }
        if media_url:
            params["media_url"] = [media_url]
        twilio_client.messages.create(**params)
        log.info("Sent reply to %s", to)
    except Exception as e:
        log.error("Failed to send WhatsApp to %s: %s", to, e)


# ── Language detection ────────────────────────────────────────────────────────

def detect_language(text: str) -> str:
    for ch in text:
        cat = unicodedata.category(ch)
        name = unicodedata.name(ch, "")
        if "ARABIC" in name:
            return "ar"
    return "en"
