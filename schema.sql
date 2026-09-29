-- Germany streaming tracker tables. Run once in the Supabase SQL editor.
-- Lives alongside the Poland tracker's tables; nothing here touches those.

create table if not exists de_releases (
  series_slug           text not null,
  provider_slug         text not null,
  season_label          text not null,
  title                 text,
  original_title        text,
  countries             text,      -- as listed on the page, e.g. "USA/GB"
  countries_tmdb        text,      -- filled from TMDb only when the page lists none
  start_year            int,
  end_year              int,
  genres                text,
  provider_name         text,
  channel_via           text,      -- "Prime Video Zusatz-Kanäle" / "MagentaTV" for add-on channels
  is_premiere           boolean,
  date_text             text,      -- raw wording, e.g. "ab 14.10."
  expected_date         date,      -- announced start date (latest seen)
  available_date        date,      -- confirmed from the "Neu verfügbar" list
  first_seen            date,
  first_seen_upcoming   date,
  last_seen_upcoming    date,
  last_seen_available   date,
  imdb_url              text,
  matched_original_name text,
  primary key (series_slug, provider_slug, season_label)
);

create table if not exists de_date_changes (
  id            bigserial primary key,
  series_slug   text not null,
  provider_slug text not null,
  season_label  text not null,
  old_date      date,
  new_date      date,
  seen_on       date not null
);

create table if not exists de_scrape_log (
  run_date       date primary key,
  upcoming_rows  int,
  available_rows int,
  kept_rows      int,
  date_changes   int,
  scraped_at     timestamptz default now()
);

create table if not exists de_title_links (
  series_slug           text primary key,
  tmdb_id               bigint,
  tmdb_type             text,
  imdb_id               text,
  imdb_url              text,
  matched_original_name text,
  origin_iso            text
);

-- Public read-only (the dashboard uses the anon key); writes need the service key.
alter table de_releases     enable row level security;
alter table de_date_changes enable row level security;
alter table de_scrape_log   enable row level security;
alter table de_title_links  enable row level security;

create policy "public read" on de_releases     for select using (true);
create policy "public read" on de_date_changes for select using (true);
create policy "public read" on de_scrape_log   for select using (true);
create policy "public read" on de_title_links  for select using (true);
