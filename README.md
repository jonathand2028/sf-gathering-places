# SF Gathering Places

A low-friction decision engine for going out alone: pick a San Francisco
neighborhood, or search any Bay Area address, ZIP, or neighborhood name, and
get one real, currently-open place to gather — a restaurant, bar, café, gym,
bookstore, or community space — filtered so it's actually a public gathering
spot, not a corporate office or a private business. Save it, dismiss it for
the next option, or ask Gemini for a short brief on what it's actually like
to walk in there solo.

See [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for the full architecture writeup, a
minute-by-minute demo walkthrough, and a Q&A cheat sheet covering the harder
technical questions.

## How it works

- **Search:** neighborhood chips (top 8 by live gathering-place count), a
  full 41-neighborhood dropdown, or free-text search with a dynamic
  suggestion dropdown backed by instant SF ZIP lookup, neighborhood-name
  matching, and Nominatim geocoding biased to the Bay Area. Addresses outside
  San Francisco (Oakland, Berkeley, Daly City, ...) resolve cleanly and show
  an honest straight-line distance instead of being blocked.
- **Filtering:** every result is a NAICS-code / keyword / public-place-term
  match, minus a corporate-entity and office/floor/suite exclusion list —
  see `place_filter_sql()` in `app.py`.
- **Category filter:** "Show up alone" (default), "Free or low cost", or
  "Anywhere."
- **Per-visitor state:** each browser session gets a UUID (`visitor_id`)
  used to scope saved and dismissed places, so concurrent users never
  collide. Dismissed places live in an "Avoided Places" drawer and can be
  restored.
- **AI layer:** Google Gemini generates a short solo-visit brief ("What's it
  like to go alone?") and two casual invite-text drafts ("Invite someone")
  for the currently selected place. If the Gemini call fails for any reason,
  the app falls back to a curated, category-specific static tip instead of
  showing an error.

## Data

- **ClickHouse** (`sf_business` table) — DataSF's registered-business
  dataset: name, address, ZIP, neighborhood, open/close dates, a
  self-reported industry code, and a geographic point. This is the
  read-only source for every place lookup, count, and distance
  calculation — including live spatial queries via `geoDistance()`.
- **Postgres / Neon** — three tables, created automatically on first run:
  `saved_places` and `dismissed_places` (both scoped by `visitor_id`), and
  `shown_places` (a global, non-per-visitor count of places surfaced across
  all sessions, used for the "you've seen N places" footer stat).

## Running it

```
pip install -r requirements.txt
streamlit run app.py
```

Needs a `.env` file (not committed) with:

```
CH_HOST=...
CH_PASSWORD=...
DATABASE_URL=...
GEMINI_API_KEY=...
```

On Streamlit Cloud, set the same four values under the app's Settings →
Secrets instead — the app reads `st.secrets` first and falls back to `.env`
locally.
