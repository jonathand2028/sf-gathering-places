# SF Gathering Places — Demo Script & Architecture Guide

This is your prep doc for presenting to judges. It covers the problem, the real
architecture (grounded in the actual code, not marketing language), a
minute-by-minute demo script, and a Q&A cheat sheet for hard technical
questions. Read section 2 and 4 closely — that's the part that separates "I
vibe-coded this" from "I can defend every design decision."

---

## 1. Project Overview & The Problem

### The problem

Loneliness in cities is a documented public health issue, but the tools built
to fix it assume the wrong starting condition. Meetup apps, event platforms,
and social calendars all require you to already have a plan, a group, or the
social energy to organize one. If you're lonely *right now*, the last thing
that helps is another app asking you to coordinate with other people first.

There's a second, quieter failure mode: decision paralysis. Even someone
willing to go out alone is faced with a search engine, a review site, and a
map with hundreds of pins — and no signal for "is this a place where showing
up alone is normal, or will I feel like the only person in a room built for
groups?"

### The solution

SF Gathering Places is a **low-friction decision engine**, not a directory.
It gives you exactly one real, currently-open place at a time — no swiping
through a list, no comparing ratings, no group to assemble first. The default
filter is literally called "Show up alone," and it's on by default. The
product's job is to remove the decision, not present more options.

Core interaction: pick a neighborhood (or search an address) → see one place
→ optionally get an AI-written "here's what it's actually like to walk in
alone" brief → save it, or say "not for me" and get the next one.

### The dataset

- **Source:** `sf_business`, DataSF's official registered-business dataset,
  loaded into ClickHouse Cloud. **366,032 total records** (this is a live
  `count()`, not a static number baked into the UI — it's queried fresh from
  ClickHouse every time the badge renders).
- Every record has a name, address, zip, neighborhood, self-reported NAICS
  industry code, open/close dates, and (for most rows) a WKT point geometry.
- **Not every business is a gathering place.** Most of the 366k rows are
  landlords, sole proprietors, consultants, and offices with no public-facing
  space. The app applies a real filter pipeline (detailed in §2) before a
  record is ever eligible to be shown — this is why the homepage badge says
  "~7,000 places to gather," not 366,032. That smaller number is *also* a live
  query result, not hand-picked.

---

## 2. The Database Architecture: ClickHouse + Neon Postgres

Two databases, doing two genuinely different jobs. This isn't "we used two
databases because it sounds impressive" — it's a real OLAP/OLTP split, and you
should be able to explain *why* each one is doing what it's doing.

### ClickHouse — the read-heavy analytical engine (OLAP)

ClickHouse is a **columnar** database: built to scan millions of rows and
aggregate/filter/sort them fast, at the cost of being bad at single-row
transactional writes. That's exactly the shape of this app's core query:
"scan up to 366,032 rows, apply a dozen text filters, compute geographic
distance for each one, sort by distance, return the top handful."

Every place lookup runs one of two queries against `sf_business`:

```sql
-- neighborhood mode (open_places)
SELECT uniqueid, dba_name, full_business_address,
       self_reported_naics_code, location_start_date
FROM sf_business
WHERE neighborhoods_analysis_boundaries = {nb:String}
  AND location_end_date = ''
  AND city = 'San Francisco'
  AND <place_filter_sql>
ORDER BY dba_name

-- address/search mode (places_near_anchor) — does real spatial math
SELECT uniqueid, dba_name, full_business_address, self_reported_naics_code,
       location_start_date,
       geoDistance(readWKTPoint(location).1, readWKTPoint(location).2, {lon}, {lat}) AS dist_m,
       neighborhoods_analysis_boundaries AS hood
FROM sf_business
WHERE location_end_date = '' AND city = 'San Francisco'
  AND location != '' AND startsWith(location, 'POINT')
  AND <place_filter_sql>
ORDER BY dist_m ASC
LIMIT 200
```

`geoDistance()` is ClickHouse's built-in great-circle (haversine-style)
distance function — real spatial math computed inside the database, not
approximated in Python. `readWKTPoint(location)` parses the stored
`'POINT (lon lat)'` string into coordinates on the fly.

**Measured performance:** every query is timed in Python around the
`ch().query()` call and shown live in the UI ("⚡ 366,032 records scanned in
Nms via ClickHouse Cloud"). Observed numbers during testing: **136–385ms** for
a full-table scan with the entire filter stack (NAICS OR keyword-match OR
public-place-term match, AND corporate-exclusion, AND-ed together) plus a
live per-row distance computation and sort — **with zero pre-built spatial
index**. That's the honest number to quote: a few hundred milliseconds for a
cold, unindexed, 366k-row geospatial query is a legitimately strong result for
ClickHouse's columnar engine, and you don't need to round it down to sound
impressive.

**The filter pipeline** (this is the "how do you keep offices out" answer):

1. **Inclusion** — a record must match at least one of three signals:
   - `self_reported_naics_code` starts with one of 5 prefixes covering
     restaurants, bars/drinking places, recreation & fitness, and book/media
     retail (as self-reported to the city, not a strict federal taxonomy).
   - Its name contains one of ~29 "alone-friendly" keywords (`community
     center`, `library`, `ymca`, `gym`, `yoga`, `climbing`, `church`,
     `makerspace`, `chess`, `bowling`, `pottery`, etc.) — this is what pulls
     in gathering spots that don't have a "restaurant" NAICS code at all.
   - Its name contains one of ~21 generic public-place terms (`Library`,
     `Center`, `Community`, `Studio`, `Cafe`, `Club`, `Museum`, `Church`,
     `Academy`, ...).
2. **Exclusion** (AND-ed on top of the above) — the record is dropped if:
   - Its name contains a corporate-entity marker: `LLC`, `Inc`, `Corp`,
     `Corporation`, `Holdings`, `Group`, `Consulting`, `Partners`,
     `Management`, `Capital`, `Investments`, `Solutions`, `Technologies`,
     `Logistics`, `Enterprise`, `Services`.
   - Its address contains an office marker: `Fl `, `Floor`, `Ste `, `Suite`.
3. **A second pass in Python** (`is_junk_name`) after the SQL result comes
   back: drops names that start with a digit, names identical to their own
   address (placeholder/junk rows), and any name still containing `LLC`,
   `INC`, `CORP`, or `TRUST` as a whole-word-ish token. This is defense in
   depth — SQL substring filters occasionally let a stray record through, and
   the Python pass catches it before it can ever become the hero card.

### Neon Postgres — the write-heavy transactional layer (OLTP)

Postgres is doing what Postgres is good at: small, frequent, per-user writes
that need to be durable and immediately consistent. Three tables:

```sql
saved_places      (id, place_name, address, neighborhood, visitor_id, created_at)
dismissed_places  (id, place_name, visitor_id, created_at)
shown_places      (id, place_name, neighborhood, shown_at)          -- NOT visitor-scoped
```

- `saved_places` / `dismissed_places` are keyed by `visitor_id` (a UUID
  generated once per browser session — see §4/Q3 for why this matters for
  concurrency).
- `shown_places` is deliberately **global**, not per-visitor — it powers the
  "You've seen 56 places across 14 neighborhoods" footer line, which is a
  cross-session stat about the whole app's usage, not one person's session.
- **Connection pattern:** the app opens and closes a fresh `psycopg2`
  connection on every single query (`pg_conn()` context manager), instead of
  caching one long-lived connection. This was a real bug fix, not a
  stylistic choice — Neon's serverless Postgres auto-suspends idle compute,
  which silently kills a cached connection and throws
  `psycopg2.InterfaceError: connection already closed` on the next query.
  Connect-per-call costs a little latency per write but makes the app
  immune to Neon's suspend/resume cycle. Every `pg_query`/`pg_exec` call is
  also wrapped in try/except, so a Postgres outage degrades to a warning
  toast, never a crash.

### The actual "data synergy loop" (be precise about this one)

The honest version, in case a judge asks to see the query: **there is no
`WHERE place_id NOT IN (...)` pushed into ClickHouse.** The two databases
don't share a query — they share a rerun cycle. Here's exactly what happens:

1. User clicks **"Not for me"** → `INSERT INTO dismissed_places (place_name,
   visitor_id) VALUES (...)` — one OLTP write, keyed by name (there's no
   shared numeric ID between the two systems; correlation is by business
   name string, which is a known simplification, not a bug you need to
   hide — see §4).
2. `st.rerun()` fires. On the new run, `fetch_dismissed_names()` re-reads
   this visitor's dismissed names from Postgres (`SELECT place_name FROM
   dismissed_places WHERE visitor_id = %s`).
3. The candidate list — which ClickHouse already returned and Streamlit
   already has cached for this neighborhood/anchor (`@st.cache_data(ttl=600)`
   means **no new ClickHouse round-trip happens at all** for a dismissal) —
   gets filtered in Python: `candidates = [p for p in rows if p["name"] not
   in dismissed and not is_junk_name(...)]`. The next candidate becomes the
   new hero.
4. **"Restore"** in the Avoided Places drawer does the mirror operation:
   `DELETE FROM dismissed_places WHERE id = %s AND visitor_id = %s`, clear
   the cached Gemini drafts, `st.rerun()`. Because the ClickHouse result is
   still cached, the restored place is instantly eligible again — no
   re-query needed.

This is arguably a *better* story than a live SQL pushdown: it shows you
understand Streamlit's rerun model and cache boundaries, and that you're
deliberately keeping ClickHouse doing bulk analytical work while Postgres
handles cheap per-user state, joined in the application layer instead of at
the database layer. Say it this way, not the "NOT IN" version — a judge who
asks to see the query will find this, and being accurate here builds more
credibility than the shortcut version.

---

## 3. 10-Minute Comprehensive Demo Script

### Minute 0–2: The hook & the problem

> "Every social app assumes you already have someone to go with. If you're
> lonely, that's exactly the thing you don't have. I built a tool that skips
> the coordination step entirely — it just tells you one real place, right
> now, where showing up alone is normal."

Show the landing page. Point out: the subtitle ("Places you can walk into
alone, where you'll end up around people"), the live badge count, and that
"Show up alone" is the *default* filter, not an option you have to find.

### Minute 2–4: Live product walkthrough

- Click a neighborhood chip (e.g. **Mission**) — instant hero card: name,
  address, "open since [year]", a category tag ("Community space" / "Bar" /
  etc.).
- Show the **category filter pills**: Show up alone / Free or low cost /
  Anywhere — toggle one to show the candidate pool changing live.
- Switch to **address search**: type `Valencia` or `Sonia` — a real dynamic
  suggestion dropdown appears ("Matching Bay Area addresses — pick one:"),
  populated from a combination of SF ZIP lookup, neighborhood name matching,
  and live Nominatim geocoding. Pick one, or let the top match auto-resolve.
- Type an out-of-SF address (e.g. `49 Sonia St, Oakland`) to show it **does
  not silently fail or restrict to SF** — it geocodes cleanly and shows
  "7.6 miles away from 49 Sonia St, Oakland — here are the closest places we
  cover." Honesty about distance instead of pretending Oakland is SF.

### Minute 4–6: Deep dive — ClickHouse

- Point at the **⚡ ClickHouse badge** on the hero card: "N,NNN records
  scanned in Nms via ClickHouse Cloud." Explain this is a live full-table
  scan timed in real time, not a cached fake number — refresh a search and
  watch the number change.
- Open the code to `place_filter_sql()` briefly, or just narrate it: three
  inclusion signals (NAICS prefix, keyword match, public-place term) AND-ed
  against a corporate-exclusion blacklist (LLC/Inc/Corp/floor/suite).
- This is the moment to say: "This is why you'll never see 'Anthropic PBC'
  or a law firm suite show up as a recommended hangout spot."

### Minute 6–8: Deep dive — Neon Postgres

- Click **"Not for me"** on the current hero. Watch the card instantly swap
  to the next candidate, and point at the **🐘 Postgres Session** badge
  updating ("0 Saved • 1 Avoided").
- Open the **"Avoided Places"** expander — show the dismissed name sitting
  there with a "Restore" button.
- Click **Restore**. Explain: this deletes the Postgres row, clears cached
  AI drafts, and the place is instantly back in rotation — no new database
  scan needed, because the underlying ClickHouse result is still cached;
  only the Postgres-informed filter changed.
- Mention `visitor_id`: a UUID generated per browser session, used to scope
  every save/dismiss read and write — this is what makes two people using
  the app side-by-side (or two judges on two laptops) fully independent.

### Minute 8–9: The AI layer

- Click **"What's it like to go alone?"** — a Gemini-generated, 3–4 sentence,
  practical brief about walking into *this specific, already-selected* place
  solo (when to go, what to say at the door, reassurance it's normal).
- Click **"Invite someone"** — two casual, non-flyer-sounding text-message
  drafts a real person might actually send.
- Mention the fallback: if the Gemini call fails (bad key, rate limit,
  network), the app **never shows a raw error** — it shows a styled,
  amber-bordered tip pulled from a curated per-category fallback dict
  (different tip for a bar vs. a gym vs. a bookstore), so the feature
  degrades gracefully instead of crashing the demo.

### Minute 9–10: Wrap-up & vision

> "Today this is one city and one dataset, but the whole pipeline — ClickHouse
> filter logic, Postgres session state, the AI layer — is city-agnostic. Swap
> in any municipal open-data business registry with a name, address, and
> category, and the same three-part architecture works in Oakland, Chicago,
> or NYC. The bigger idea is: loneliness tools shouldn't require organizing
> other people first. One honest, real, currently-open recommendation is a
> lower bar to clear than a group chat."

---

## 4. Judge Q&A Cheat Sheet

### Q1: Why two databases instead of just Postgres (or just ClickHouse)?

Because the two workloads have opposite shapes. The place-lookup query scans
up to 366k rows, applies a dozen text predicates, computes distance for every
row, and sorts — that's a columnar analytical scan, ClickHouse's entire
reason to exist, and it does it in a few hundred milliseconds with zero
indexes. Save/dismiss actions are the opposite: single-row inserts/deletes
that need to be immediately durable and correctly isolated per user —
classic OLTP, which is what Postgres is built for and ClickHouse is
deliberately bad at (it's not designed for frequent single-row mutations).
Doing both in Postgres would make the analytical scan slow at 366k rows with
this many text predicates; doing both in ClickHouse would make per-click
writes clunky and give up transactional guarantees we don't need for
analytics but do need for "did this save actually happen."

### Q2: How do you keep corporate offices and private businesses out?

Three layers, detailed in §2: a SQL inclusion whitelist (NAICS prefix OR
keyword match OR public-place term), a SQL exclusion blacklist (corporate
name markers AND office/floor/suite address markers), and a final Python-side
pass that drops digit-prefixed names, name==address placeholder rows, and any
stray LLC/INC/CORP/TRUST token that slipped past the SQL filter. It's
defense in depth, not one clever regex.

### Q3: What happens if two judges open the app at the exact same time?

Nothing collides. Each browser session gets its own `visitor_id` (a UUID
generated once, on first load, into `st.session_state`) — every Postgres
read and write for saves and dismissals is scoped by `WHERE visitor_id =
%s`. Two people can dismiss the same place simultaneously and each will get
their own independent recommendation feed; neither ever sees the other's
saved list or avoided list. The one deliberately shared, non-isolated table
is `shown_places`, which powers a global "you've seen N places across the
app" footer stat — that's intentional, not a bug.

### Q4: Is Gemini making up venues?

No — and structurally, it can't. Gemini is never asked "what's a good place
in the Mission" — it's only ever given a prompt that already contains the
specific venue name, neighborhood, category, and open-since year that
ClickHouse already returned as the hero card. Its only job is to generate
*prose about a place the database already chose* (solo-visit tips, or invite
text). The venue itself is never Gemini's output — swap the API key for an
invalid one and the venue selection, distance math, and filtering all keep
working identically; only the AI-generated commentary falls back to a static,
per-category tip.

### Q5: How fast are the spatial queries, really?

136–385ms observed in testing, scanning the full 366,032-row table live with
the entire filter predicate stack plus a per-row `geoDistance()` computation
and sort — with **no pre-built spatial index**. That's the number to say out
loud; it doesn't need rounding down. The timing itself is measured in Python
around every ClickHouse call and rendered live in the UI badge, so a judge
can watch the number change between two different searches instead of taking
your word for it.

### Q6: Why keyword matching instead of a proper ML classifier for "is this a gathering place"?

Because the ground truth doesn't exist to train one — DataSF's NAICS codes
are self-reported at business registration and inconsistent (a yoga studio
might register as "other services," a community space might register as
nothing recognizable at all). A curated keyword/NAICS hybrid, backed by an
explicit corporate blacklist, is auditable and instantly explainable: you can
point at the exact list of ~30 keywords and 15 corporate-exclusion terms and
know precisely why a given business is or isn't showing up. That's a real
advantage in a live demo — if a judge asks "why is this place here," the
answer is one line of Python, not a black box.

### Q7: What happens if ClickHouse or Postgres goes down mid-demo?

Every database call is wrapped in try/except with logging to stderr. A
ClickHouse outage on the main query surfaces a friendly "we're having
trouble reaching our data" message with an honest degraded footer instead of
a stack trace. A Postgres outage degrades Save/Dismiss to a toast warning —
the recommendation engine itself keeps working, because it never depended on
Postgres to pick a place, only to remember your reaction to one. This was
tested directly during development by pointing the app at invalid
credentials and confirming the failure mode.

### Q8: Why a session UUID instead of a login?

This is a walk-up, zero-friction product — the entire pitch is removing
friction for someone who's already in a low-energy, lonely state. Asking for
an account creates exactly the kind of activation-energy barrier the product
exists to eliminate. The UUID gives real per-session state isolation (saves,
dismissals) without a signup wall; the tradeoff, stated plainly, is that
state doesn't persist across devices or a cleared browser session — a
reasonable trade for a hackathon-stage product, and a login could be layered
on top of the same `visitor_id`-scoped schema later without a rewrite.

### Q9: Does this only work for San Francisco?

The dataset is SF-specific (DataSF's business registry), but nothing else in
the pipeline is. The filter logic, the ClickHouse schema shape, the Postgres
session model, and the Gemini prompts are all written against generic
columns (name, address, category code, coordinates) that any city's open
business-registry export would also have. The search box already reaches
past city limits — a Bay Area viewbox on the Nominatim geocoder resolves
Oakland/Berkeley/Daly City addresses today and shows honest distance instead
of pretending they're San Francisco.

### Q10: What's the actual weak point in this system right now?

Worth having a candid answer ready — judges respect this more than pretending
there isn't one. The dismissed/saved correlation between Postgres and
ClickHouse is done by **business name string**, not a shared numeric ID —
two distinct businesses with an identical `dba_name` would be indistinguishable
to the dismiss/save logic. It's a known simplification, not something to
discover live on stage: mention it if asked, frame it as the obvious next
fix (store ClickHouse's `uniqueid` in the Postgres rows instead of the name).

---

## Quick reference: the numbers to have memorized

| Fact | Number |
|---|---|
| Total DataSF business records in ClickHouse | 366,032 |
| Live "places to gather" after filtering | ~6,990 (live query, changes as data does) |
| SF neighborhoods covered | 41 (`ALL_HOODS`), 8 shown as default chips |
| ClickHouse query latency (full-table scan + filter + distance sort) | 136–385ms observed |
| Corporate name-exclusion terms | 15 |
| Alone-friendly keyword whitelist | 29 |
| SF ZIPs with instant (zero-network) coordinate lookup | 20 |
| Address suggestions gathered per search | up to 5 |
| Out-of-SF distance threshold before showing the honest "X miles away" note | 1.0 mile |
| Gemini model used | `gemini-flash-lite-latest` (direct REST call, no SDK) |
