# SF Gathering Places

Pick a San Francisco neighborhood (or type a zip code or neighborhood name) and see how many places to gather — restaurants, bars, cafés, gyms, and bookstores — are still open there, how that compares to the rest of the city, and where they are on a map. Pick one and get an AI-drafted text to invite a friend, then RSVP and see who else picked the same place and night.

## Data

- **ClickHouse** (`sf_business` table) — every business ever registered in San Francisco: name, address, zip, neighborhood, open/close dates, and a self-reported industry code. This is the read-only source for everything about places: counts, the map, the closures chart, and the list of open spots.
- **Postgres / Neon** (`attendances` table) — created automatically on first run. Stores each RSVP (place, night, timestamp) so the app can show how many other people picked the same plan.

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

On Streamlit Cloud, set the same four values under the app's Settings → Secrets instead — the app reads `st.secrets` first and falls back to `.env` locally.
