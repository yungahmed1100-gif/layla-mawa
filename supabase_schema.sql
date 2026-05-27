-- Run once in Supabase SQL Editor. Idempotent.

create extension if not exists "pgcrypto";

create table if not exists listings (
    mawa_id          text primary key,
    url              text not null,
    title_ar         text,
    title_en         text,
    transaction      text,                  -- 'rent' | 'sale'
    property_type    text,                  -- 'apartment' | 'villa' | 'twin-villa' | ...
    location         text,                  -- slug: 'al-mouj', 'qurum'
    location_ar      text,
    bedrooms         int,
    bathrooms        int,
    area_sqm         numeric,
    price_omr        numeric,
    furnished        text,                  -- 'furnished' | 'unfurnished' | 'semi'
    description_ar   text,
    description_en   text,
    image_urls       jsonb,                 -- array of hotlinked Mawa CDN URLs
    agent_name       text,
    agent_phone      text,
    agent_whatsapp   text,
    raw              jsonb,                 -- everything else, future-proofing
    scraped_at       timestamptz default now(),
    is_active        boolean default true
);

create index if not exists idx_listings_search
    on listings (transaction, location, bedrooms, price_omr)
    where is_active = true;

create index if not exists idx_listings_freshness
    on listings (is_active, scraped_at);

create table if not exists leads (
    id                    uuid primary key default gen_random_uuid(),
    phone_number          text not null,
    twilio_message_sids   text[] default '{}',     -- for dedupe
    name                  text,
    language              text default 'ar',
    transaction           text,
    location              text,
    bedrooms              int,
    max_budget_omr        numeric,
    interested_listing_id text references listings(mawa_id),
    conversation_log      jsonb default '[]'::jsonb,
    status                text default 'new',
    created_at            timestamptz default now(),
    updated_at            timestamptz default now()
);

create unique index if not exists idx_leads_phone on leads(phone_number);
create index if not exists idx_leads_status on leads(status, created_at desc);

-- Auto-update updated_at
create or replace function set_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists trg_leads_updated on leads;
create trigger trg_leads_updated before update on leads
    for each row execute function set_updated_at();
