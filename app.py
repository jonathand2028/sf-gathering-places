import html
import os, sys, time, traceback
import requests
import streamlit as st
import clickhouse_connect, certifi, psycopg2
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="SF Gathering Places", page_icon="🫂", layout="wide")

GATHERING_NAICS_PREFIXES = ("7225", "7224", "7139", "4512", "4592")

ALL_HOODS = [
    "Bayview Hunters Point", "Bernal Heights", "Castro/Upper Market", "Chinatown",
    "Excelsior", "Financial District/South Beach", "Glen Park", "Golden Gate Park",
    "Haight Ashbury", "Hayes Valley", "Inner Richmond", "Inner Sunset", "Japantown",
    "Lakeshore", "Lincoln Park", "Lone Mountain/USF", "Marina", "McLaren Park",
    "Mission", "Mission Bay", "Nob Hill", "Noe Valley", "North Beach",
    "Oceanview/Merced/Ingleside", "Outer Mission", "Outer Richmond", "Pacific Heights",
    "Portola", "Potrero Hill", "Presidio", "Presidio Heights", "Russian Hill",
    "Seacliff", "South of Market", "Sunset/Parkside", "Tenderloin", "Treasure Island",
    "Twin Peaks", "Visitacion Valley", "West of Twin Peaks", "Western Addition",
]

# Median (lon, lat) per zip, precomputed from sf_business so common SF zips resolve
# instantly with no network call instead of round-tripping through Nominatim.
SF_ZIP_COORDS = {
    "94102": (-122.414895, 37.782387), "94103": (-122.410204751, 37.774332),
    "94107": (-122.395734, 37.777428), "94108": (-122.4061335, 37.790415),
    "94109": (-122.4203715, 37.790390892), "94110": (-122.41791, 37.754244),
    "94112": (-122.4407385, 37.720926), "94114": (-122.433696, 37.760571),
    "94115": (-122.4351855, 37.786725), "94117": (-122.444210859, 37.771461),
    "94118": (-122.4614115, 37.782387), "94121": (-122.4866475, 37.779336),
    "94122": (-122.4804285, 37.761003), "94123": (-122.4347175, 37.798839),
    "94124": (-122.391945, 37.733346), "94127": (-122.463792, 37.737522),
    "94131": (-122.435406, 37.74375), "94132": (-122.47668, 37.720791),
    "94133": (-122.409126, 37.799829), "94134": (-122.4065835, 37.719045),
}


def record_error(context, e):
    tb = traceback.format_exc()
    st.session_state.last_error = {"context": context, "error": f"{type(e).__name__}: {e}", "traceback": tb}
    print(f"[ERROR] {context}\n{type(e).__name__}: {e}\n{tb}", file=sys.stderr)


def secret(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key)


def gemini_generate(prompt):
    api_key = secret("GEMINI_API_KEY")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-flash-lite-latest:generateContent?key={api_key}"
    )
    resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


@st.cache_resource
def ch():
    return clickhouse_connect.get_client(
        host=secret("CH_HOST"), port=8443, username="default",
        password=secret("CH_PASSWORD"), secure=True, ca_cert=certifi.where(),
    )


@contextmanager
def pg_conn():
    conn = psycopg2.connect(secret("DATABASE_URL"), connect_timeout=10)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def pg_query(sql, params=None):
    try:
        with pg_conn() as conn:
            with conn.cursor() as c:
                c.execute(sql, params or ())
                return c.fetchall() if c.description else []
    except Exception as e:
        record_error(sql, e)
        raise


def pg_exec(sql, params=None):
    try:
        with pg_conn() as conn:
            with conn.cursor() as c:
                c.execute(sql, params or ())
    except Exception as e:
        record_error(sql, e)
        raise


@st.cache_resource
def ensure_tables():
    pg_exec("""
        CREATE TABLE IF NOT EXISTS saved_places (
            id SERIAL PRIMARY KEY,
            place_name TEXT,
            address TEXT,
            neighborhood TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    pg_exec("""
        CREATE TABLE IF NOT EXISTS dismissed_places (
            id SERIAL PRIMARY KEY,
            place_name TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    pg_exec("""
        CREATE TABLE IF NOT EXISTS shown_places (
            id SERIAL PRIMARY KEY,
            place_name TEXT,
            neighborhood TEXT,
            shown_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    return True


def record_shown(place_name, neighborhood):
    key = (place_name, neighborhood)
    if st.session_state.get("last_shown") == key:
        return
    st.session_state.last_shown = key
    try:
        pg_exec("INSERT INTO shown_places (place_name, neighborhood) VALUES (%s, %s)", (place_name, neighborhood))
    except Exception:
        pass


def fetch_shown_stats():
    try:
        rows = pg_query("SELECT count(*), count(DISTINCT neighborhood) FROM shown_places")
        return rows[0] if rows else (0, 0)
    except Exception:
        return (0, 0)


def naics_filter_sql():
    return " OR ".join(f"startsWith(self_reported_naics_code, '{p}')" for p in GATHERING_NAICS_PREFIXES)


ALONE_FRIENDLY_KEYWORDS = (
    "community center", "senior center", "library", "ymca", "rec center",
    "recreation center", "gym", "fitness", "yoga", "pilates", "crossfit",
    "martial arts", "jiu jitsu", "boxing", "dance", "climbing", "bouldering",
    "church", "temple", "synagogue", "meditation", "zen", "art center",
    "cultural center", "makerspace", "workshop", "chess", "run club",
    "bowling", "pottery", "ceramics",
)
FREE_KEYWORDS = (
    "community center", "senior center", "library", "ymca", "rec center",
    "recreation center", "church", "temple", "synagogue",
)


def is_alone_friendly(name):
    lower = (name or "").lower()
    return any(kw in lower for kw in ALONE_FRIENDLY_KEYWORDS)


def is_free_or_low_cost(name):
    lower = (name or "").lower()
    return any(kw in lower for kw in FREE_KEYWORDS)


def alone_friendly_filter_sql():
    return " OR ".join(f"positionCaseInsensitive(dba_name, '{kw}') > 0" for kw in ALONE_FRIENDLY_KEYWORDS)


CORPORATE_NAME_EXCLUDE_TERMS = (
    "Enterprise", "LLC", "Inc", "Corp", "Corporation", "Holdings", "Group",
    "Consulting", "Services", "Partners", "Management", "Capital", "Investments",
    "Solutions", "Technologies", "Logistics",
)
CORPORATE_ADDRESS_EXCLUDE_TERMS = ("Fl ", "Floor", "Ste ", "Suite")
PUBLIC_PLACE_TERMS = (
    "Library", "Rec", "Recreation", "Center", "Community", "YMCA", "Gym",
    "Fitness", "Park", "Studio", "Cafe", "Coffee", "Club", "Museum", "Society",
    "Garden", "Church", "Hall", "Book", "Academy", "Guild",
)


def corporate_exclusion_sql():
    name_excl = " AND ".join(f"dba_name NOT ILIKE '%{t}%'" for t in CORPORATE_NAME_EXCLUDE_TERMS)
    addr_excl = " AND ".join(f"full_business_address NOT ILIKE '%{t}%'" for t in CORPORATE_ADDRESS_EXCLUDE_TERMS)
    return f"({name_excl}) AND ({addr_excl})"


def public_place_terms_sql():
    return " OR ".join(f"dba_name ILIKE '%{t}%'" for t in PUBLIC_PLACE_TERMS)


def place_filter_sql():
    inclusion = f"(({naics_filter_sql()}) OR ({alone_friendly_filter_sql()}) OR ({public_place_terms_sql()}))"
    return f"{inclusion} AND {corporate_exclusion_sql()}"


@st.cache_data(ttl=600)
def open_places(neighborhood):
    sql = f"""
        SELECT uniqueid, dba_name, full_business_address,
               self_reported_naics_code, location_start_date
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
          AND location_end_date = ''
          AND city = 'San Francisco'
          AND {place_filter_sql()}
        ORDER BY dba_name
    """
    t0 = time.time()
    try:
        r = ch().query(sql, parameters={"nb": neighborhood})
    except Exception as e:
        record_error(sql, e)
        raise
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, ms


@st.cache_data(ttl=600)
def total_business_count():
    sql = "SELECT count() FROM sf_business"
    t0 = time.time()
    try:
        r = ch().query(sql)
    except Exception as e:
        record_error(sql, e)
        raise
    ms = int((time.time() - t0) * 1000)
    return r.result_rows[0][0], ms


@st.cache_data(ttl=86400, show_spinner=False)
def geocode(query):
    """Returns (lat, lon, display_name) or None."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "SFGatheringPlaces/1.0 (hackathon project)"},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return float(data[0]["lat"]), float(data[0]["lon"]), data[0]["display_name"]
    except Exception:
        return None


def short_display(display_name):
    parts = [p.strip() for p in display_name.split(",")]
    return ", ".join(parts[:4]) if len(parts) > 4 else display_name


def match_neighborhood_name(text):
    q = text.strip().lower()
    for h in ALL_HOODS:
        if h.lower() == q:
            return h
    for h in ALL_HOODS:
        if h.lower().startswith(q):
            return h
    for h in ALL_HOODS:
        if q in h.lower():
            return h
    return None


@st.cache_data(ttl=300)
def fetch_address_suggestions(query):
    sql = """
        SELECT full_business_address,
               any(readWKTPoint(location).1) AS lon,
               any(readWKTPoint(location).2) AS lat
        FROM sf_business
        WHERE full_business_address ILIKE {pattern:String}
          AND city = 'San Francisco'
          AND location != '' AND startsWith(location, 'POINT')
        GROUP BY full_business_address
        LIMIT 5
    """
    try:
        r = ch().query(sql, parameters={"pattern": f"%{query}%"})
    except Exception as e:
        record_error(sql, e)
        return []
    return [(row[0], row[2], row[1]) for row in r.result_rows]


def format_distance(dist_m):
    miles = dist_m / 1609.34
    if miles <= 2:
        walk_min = round((miles / 3) * 60)
        return f"{miles:.1f} miles away · {walk_min} min walk"
    return f"{miles:.1f} miles away"


@st.cache_data(ttl=3600)
def neighborhood_anchor_coords():
    sql = """
        SELECT neighborhoods_analysis_boundaries AS hood,
               medianExact(readWKTPoint(location).1) AS lon,
               medianExact(readWKTPoint(location).2) AS lat
        FROM sf_business
        WHERE location != '' AND startsWith(location, 'POINT')
          AND hood != '' AND hood != 'Multiple'
        GROUP BY hood
    """
    try:
        r = ch().query(sql)
    except Exception as e:
        record_error(sql, e)
        raise
    return {row[0]: (row[1], row[2]) for row in r.result_rows}


@st.cache_data(ttl=600)
def places_near_anchor(anchor_lat, anchor_lon, limit=200):
    sql = f"""
        SELECT uniqueid, dba_name, full_business_address, self_reported_naics_code, location_start_date,
               geoDistance(readWKTPoint(location).1, readWKTPoint(location).2, {{lon:Float64}}, {{lat:Float64}}) AS dist_m,
               neighborhoods_analysis_boundaries AS hood
        FROM sf_business
        WHERE location_end_date = ''
          AND city = 'San Francisco'
          AND location != '' AND startsWith(location, 'POINT')
          AND {place_filter_sql()}
        ORDER BY dist_m ASC
        LIMIT {{lim:UInt32}}
    """
    t0 = time.time()
    try:
        r = ch().query(sql, parameters={"lat": anchor_lat, "lon": anchor_lon, "lim": limit})
    except Exception as e:
        record_error(sql, e)
        raise
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, ms


@st.cache_data(ttl=600)
def top_chip_neighborhoods(n=8):
    sql = f"""
        SELECT neighborhoods_analysis_boundaries AS hood, dba_name, full_business_address
        FROM sf_business
        WHERE hood != '' AND hood != 'Multiple'
          AND location_end_date = ''
          AND city = 'San Francisco'
          AND {place_filter_sql()}
    """
    t0 = time.time()
    try:
        r = ch().query(sql)
    except Exception as e:
        record_error(sql, e)
        raise
    ms = int((time.time() - t0) * 1000)
    counts = {}
    total = 0
    for hood, name, addr in r.result_rows:
        if is_junk_name(name, addr):
            continue
        counts[hood] = counts.get(hood, 0) + 1
        total += 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [hood for hood, _ in ranked[:n]], total, ms


def fetch_dismissed_names():
    try:
        rows = pg_query("SELECT place_name FROM dismissed_places")
    except Exception:
        return set()
    return {row[0] for row in rows}


def fetch_saved_places():
    try:
        return pg_query("SELECT id, place_name, address, neighborhood FROM saved_places ORDER BY created_at DESC")
    except Exception:
        return []


def category_label(naics):
    if naics.startswith("7224"):
        return "Bar"
    if naics.startswith("7225"):
        return "Restaurant or café"
    if naics.startswith("7139"):
        return "Gym or fitness studio"
    if naics.startswith("4512") or naics.startswith("4592"):
        return "Bookstore"
    return "Gathering spot"


def type_tag(name, naics):
    if is_alone_friendly(name):
        return "Community space"
    return category_label(naics)


JUNK_TOKENS = ("LLC", "INC", "CORP", "TRUST")


def is_junk_name(name, address):
    if not name:
        return True
    if name[0].isdigit():
        return True
    if name.strip().lower() == (address or "").strip().lower():
        return True
    upper = name.upper()
    return any(token in upper for token in JUNK_TOKENS)


def section_label(text):
    st.markdown(f"<div class='section-label'>{text}</div>", unsafe_allow_html=True)


def empty_state(text):
    st.markdown(f"<div class='empty-state'>{text}</div>", unsafe_allow_html=True)


def render_generated_text(text):
    st.markdown(f"<div class='generated-text'>{html.escape(text)}</div>", unsafe_allow_html=True)


FILTER_OPTIONS = ["Show up alone", "Free or low cost", "Anywhere"]


OUT_OF_SF_MILES = 1.0


def render_body(neighborhood=None, anchor=None):
    if anchor:
        st.markdown(f"<div class='muted-text' style='margin-bottom:12px;'>Found: {anchor['label']}.</div>", unsafe_allow_html=True)
        raw_rows, fetch_ms = places_near_anchor(anchor["lat"], anchor["lon"])
        rows = [
            {"id": r[0], "name": r[1], "addr": r[2], "naics": r[3], "start": r[4], "dist_m": r[5], "hood": r[6]}
            for r in raw_rows
        ]
        location_label = anchor["label"]
    else:
        raw_rows, fetch_ms = open_places(neighborhood)
        rows = [
            {"id": r[0], "name": r[1], "addr": r[2], "naics": r[3], "start": r[4], "dist_m": None, "hood": neighborhood}
            for r in raw_rows
        ]
        location_label = neighborhood

    dismissed = fetch_dismissed_names()
    candidates = [p for p in rows if p["name"] not in dismissed and not is_junk_name(p["name"], p["addr"])]

    category_filter = st.pills(
        "Filter",
        options=FILTER_OPTIONS,
        selection_mode="single",
        default="Show up alone",
        key="category_filter",
        label_visibility="collapsed",
    ) or "Show up alone"

    if category_filter == "Show up alone":
        candidates = [p for p in candidates if is_alone_friendly(p["name"])]
    elif category_filter == "Free or low cost":
        candidates = [p for p in candidates if is_free_or_low_cost(p["name"])]

    if not candidates:
        if category_filter == "Show up alone":
            empty_state("Nothing for showing up alone found here yet, try Anywhere.")
        elif category_filter == "Free or low cost":
            empty_state("No free or low-cost spots found here yet, try Anywhere.")
        else:
            empty_state("No good matches here yet, try another neighborhood.")
        return None

    hero = candidates[0]
    hero_id, hero_name, hero_addr, hero_naics, hero_start = (
        hero["id"], hero["name"], hero["addr"], hero["naics"], hero["start"],
    )
    hero_hood = hero["hood"] or location_label
    record_shown(hero_name, hero_hood)

    if st.session_state.get("last_hero_id") != hero_id:
        st.session_state.last_hero_id = hero_id
        st.session_state.solo_draft = ""
        st.session_state.invite_drafts = []

    if anchor and hero["dist_m"] is not None and hero["dist_m"] / 1609.34 > OUT_OF_SF_MILES:
        st.markdown(
            f"<div class='muted-text' style='margin-bottom:12px;'>You're about "
            f"{hero['dist_m'] / 1609.34:.1f} miles from the nearest place we cover — other cities coming soon.</div>",
            unsafe_allow_html=True,
        )

    with st.container(border=True, key="hero_card"):
        st.markdown(f"<span class='type-tag'>{type_tag(hero_name, hero_naics)}</span>", unsafe_allow_html=True)
        st.markdown(f"<div class='hero-name'>{hero_name}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='muted-text'>{hero_addr}</div>", unsafe_allow_html=True)
        if hero["dist_m"] is not None:
            st.markdown(f"<div class='muted-text'>{format_distance(hero['dist_m'])}</div>", unsafe_allow_html=True)
        if hero_start:
            st.markdown(f"<div class='muted-text'>open since {hero_start[:4]}</div>", unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        if col1.button("Save this", type="primary", key="save_this"):
            try:
                pg_exec(
                    "INSERT INTO saved_places (place_name, address, neighborhood) VALUES (%s, %s, %s)",
                    (hero_name, hero_addr, hero_hood),
                )
                st.toast("Saved")
            except Exception:
                st.warning("Couldn't save that just now. Please try again.")
        if col2.button("Not for me", key="not_for_me"):
            try:
                pg_exec("INSERT INTO dismissed_places (place_name) VALUES (%s)", (hero_name,))
            except Exception:
                st.warning("Couldn't update that just now. Please try again.")
            st.session_state.solo_draft = ""
            st.session_state.invite_drafts = []
            st.session_state.writing_solo = False
            st.session_state.writing_invite = False
            st.rerun()

        total_count, _total_ms = total_business_count()
        st.markdown(
            f"<div class='ch-badge'>⚡ {total_count:,} records scanned in {fetch_ms}ms via ClickHouse Cloud</div>",
            unsafe_allow_html=True,
        )

    one_of_text = (
        f"One of {len(candidates):,} places near {location_label}."
        if anchor else
        f"One of {len(candidates):,} places still open in {location_label}."
    )
    st.markdown(f"<div class='muted-text' style='margin:-4px 0 4px;'>{one_of_text}</div>", unsafe_allow_html=True)

    kind = type_tag(hero_name, hero_naics).lower()
    open_since_text = f"open since {hero_start[:4]}" if hero_start else None

    with st.container(border=True, key="invite_card"):
        section_label("Going there")
        for key in ("solo_draft", "invite_drafts", "writing_solo", "writing_invite"):
            if key not in st.session_state:
                st.session_state[key] = "" if key == "solo_draft" else ([] if key == "invite_drafts" else False)

        if st.button(
            "What's it like to go alone?", type="primary", key="solo_button",
            disabled=st.session_state.writing_solo,
        ):
            st.session_state.writing_solo = True
            with st.spinner("Thinking it through…"):
                prompt = (
                    f"Someone is thinking about going alone to {hero_name}, a {kind} in {hero_hood}, "
                    f"San Francisco" + (f", {open_since_text}" if open_since_text else "") + ". "
                    f"Write 3 to 4 short, practical sentences about what it's actually like to show up there by "
                    f"yourself: a good time to go, anything to bring, what to say to whoever's at the front desk "
                    f"or counter, and honest reassurance that going alone there is normal. "
                    f"Tone: practical and warm, like a friend explaining it, not a motivational poster. "
                    f"No exclamation marks. Output only those sentences, nothing else."
                )
                try:
                    st.session_state.solo_draft = gemini_generate(prompt)
                except Exception:
                    st.error("Something went wrong putting that together. Please try again.")
            st.session_state.writing_solo = False

        if st.session_state.solo_draft:
            render_generated_text(st.session_state.solo_draft)

        if st.button(
            "Invite someone", key="invite_button",
            disabled=st.session_state.writing_invite,
        ):
            st.session_state.writing_invite = True
            with st.spinner("Writing your invite…"):
                prompt = (
                    f"Write two different short text messages inviting a friend to {hero_name}, a {kind} "
                    f"in {hero_hood}, San Francisco" + (f" ({open_since_text})" if open_since_text else "") + ". "
                    f"Each must sound like an actual text a real person would send a friend, not an invitation "
                    f"or a flyer. Rules: lowercase and casual, 2-3 sentences max, no exclamation marks, no "
                    f"\"hey there\" or similar greetings, no marketing language, include one concrete suggestion "
                    f"of when (like \"this weekend\" or \"friday after work\"). "
                    f"Separate the two messages with a line containing only ---. "
                    f"Output only the two messages and the separator, nothing else."
                )
                try:
                    text = gemini_generate(prompt)
                    parts = [p.strip() for p in text.split("---") if p.strip()]
                    st.session_state.invite_drafts = parts[:2]
                except Exception:
                    st.error("Something went wrong writing your invite. Please try again.")
            st.session_state.writing_invite = False

        for draft in st.session_state.invite_drafts:
            render_generated_text(draft)

    saved = fetch_saved_places()
    if saved:
        section_label("Your list")
        for saved_id, place_name, address, saved_hood in saved:
            with st.container(border=True, key=f"saved_row_{saved_id}"):
                col1, col2 = st.columns([5, 1])
                col1.markdown(f"**{place_name}** — {address} ({saved_hood})")
                if col2.button("Remove", key=f"remove_{saved_id}"):
                    try:
                        pg_exec("DELETE FROM saved_places WHERE id = %s", (saved_id,))
                    except Exception:
                        st.warning("Couldn't remove that just now. Please try again.")
                    st.rerun()

    total_count, _total_ms = total_business_count()
    funnel_middle = f"{len(candidates):,} nearest to {location_label}" if anchor else f"{len(candidates):,} still open in {location_label}"
    st.markdown(
        f"<div class='footer-note'>Searched {total_count:,} records in San Francisco's business registry "
        f"→ {funnel_middle} → 1 picked, in {fetch_ms} ms.</div>",
        unsafe_allow_html=True,
    )

    shown_count, shown_hoods = fetch_shown_stats()
    if shown_count:
        st.markdown(
            f"<div class='footer-note'>You've seen {shown_count:,} {'place' if shown_count == 1 else 'places'} "
            f"across {shown_hoods:,} {'neighborhood' if shown_hoods == 1 else 'neighborhoods'}.</div>",
            unsafe_allow_html=True,
        )


try:
    ensure_tables()
except Exception:
    pass

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

#MainMenu, footer, header,
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], [data-testid="stMainMenuButton"] {
    display: none !important;
}

html, body, [class*="css"] {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
}

.stApp {
    background: #06080F;
    background-image:
        radial-gradient(1100px circle at 50% -15%, rgba(59,130,246,0.28), transparent 55%),
        radial-gradient(800px circle at 15% 30%, rgba(37,99,235,0.12), transparent 60%);
    background-attachment: fixed;
}

.block-container {
    max-width: 880px;
    margin: 0 auto;
    padding: 80px 24px;
}

[data-testid="stVerticalBlock"] { gap: 12px !important; }

h1, h2, h3, h4, p, label, span, div { color: #F1F5F9; }

.hero-badge {
    display: table;
    margin: 0 auto 24px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 999px;
    padding: 7px 18px;
    font-size: 13px;
    color: #94A3B8;
}
.page-title {
    font-size: 58px;
    font-weight: 800;
    letter-spacing: -0.035em;
    line-height: 1.05;
    color: #FFFFFF;
    text-align: center;
    margin-bottom: 0;
}
.page-subtitle {
    font-size: 18px;
    color: #94A3B8;
    max-width: 540px;
    margin: 14px auto 36px;
    text-align: center;
}
.section-label {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 600;
    color: #94A3B8;
    margin-bottom: 8px;
}
.muted-text {
    font-size: 16px;
    color: #94A3B8;
}
.hero-name {
    font-size: 30px;
    font-weight: 700;
    color: #F1F5F9;
    line-height: 1.2;
    margin-bottom: 4px;
}
.footer-note {
    font-size: 13px;
    color: #94A3B8;
    margin-top: 24px;
}

/* cards */
.st-key-hero_card, .st-key-invite_card, [class*="st-key-saved_row_"] {
    background: rgba(255,255,255,0.035) !important;
    border: 1px solid rgba(255,255,255,0.09) !important;
    border-radius: 16px !important;
    padding: 30px !important;
    margin-bottom: 20px !important;
    backdrop-filter: blur(10px);
    box-shadow: 0 20px 60px -30px rgba(0,0,0,0.9) !important;
}

.empty-state {
    text-align: center;
    color: #94A3B8;
    padding: 48px 24px;
    border: 1px dashed rgba(255,255,255,0.09);
    border-radius: 16px;
    background: rgba(255,255,255,0.035);
}

.chip-caption {
    font-size: 13px;
    color: #94A3B8;
    text-align: center;
    margin-bottom: 10px;
}

.type-tag {
    display: inline-block;
    background: rgba(59,130,246,0.12);
    border: 1px solid rgba(59,130,246,0.35);
    color: #93C5FD;
    border-radius: 999px;
    padding: 3px 12px;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 10px;
}

.generated-text {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px;
    padding: 16px 18px;
    color: #F1F5F9;
    font-size: 15px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-wrap: break-word;
    height: auto;
    margin-bottom: 12px;
}

.ch-badge {
    font-size: 12px;
    color: #94A3B8;
    margin-top: 12px;
}

/* ============ unified control system ============ */
/* base rule: everything clickable */
.stButton>button,
.st-key-neighborhood_pills button,
.st-key-category_filter button {
    cursor: pointer;
    transition: all 0.15s ease;
    border-radius: 10px;
    font-weight: 500;
}

/* chips: neighborhoods + category filter */
.st-key-neighborhood_pills [data-testid="stButtonGroup"],
.st-key-category_filter [data-testid="stButtonGroup"] {
    display: flex !important;
    justify-content: center !important;
    flex-wrap: wrap !important;
    gap: 10px !important;
}
.st-key-neighborhood_pills button,
.st-key-category_filter button {
    border-radius: 999px !important;
    padding: 9px 18px !important;
    font-size: 14px !important;
    white-space: nowrap !important;
    width: auto !important;
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    color: #CBD5E1 !important;
}
.st-key-neighborhood_pills button:hover,
.st-key-category_filter button:hover {
    background: rgba(255,255,255,0.11) !important;
    border-color: rgba(255,255,255,0.30) !important;
    color: #F1F5F9 !important;
    transform: translateY(-1px);
}
.st-key-neighborhood_pills button:active,
.st-key-category_filter button:active {
    transform: scale(0.985);
}
.st-key-neighborhood_pills button[aria-pressed="true"],
.st-key-neighborhood_pills button[aria-checked="true"],
.st-key-neighborhood_pills button[aria-selected="true"],
.st-key-category_filter button[aria-pressed="true"],
.st-key-category_filter button[aria-checked="true"],
.st-key-category_filter button[aria-selected="true"] {
    background-color: #3B82F6 !important;
    color: #FFFFFF !important;
    border: none !important;
    box-shadow: 0 0 24px rgba(59,130,246,0.45) !important;
}

/* primary buttons: Save this, Write the invite */
.stButton>button[kind="primary"] {
    background-color: #3B82F6;
    color: #FFFFFF;
    padding: 12px 26px;
    font-weight: 600;
    border: none;
}
.stButton>button[kind="primary"]:hover {
    background-color: #2563EB;
    color: #FFFFFF;
    box-shadow: 0 0 28px rgba(59,130,246,0.5);
    transform: translateY(-1px);
}
.stButton>button[kind="primary"]:active {
    transform: scale(0.985);
}
.stButton>button[kind="primary"]:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
}

/* secondary buttons: Not for me, Show me another, Remove */
.stButton>button[kind="secondary"] {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.14);
    color: #CBD5E1;
    padding: 11px 22px;
}
.stButton>button[kind="secondary"]:hover {
    background: rgba(255,255,255,0.10);
    border-color: rgba(255,255,255,0.28);
    color: #F1F5F9;
    transform: translateY(-1px);
}
.stButton>button[kind="secondary"]:active {
    transform: scale(0.985);
}

/* keyboard focus ring, everything clickable */
.stButton>button:focus-visible,
.st-key-neighborhood_pills button:focus-visible,
.st-key-category_filter button:focus-visible,
[data-testid="stTextInput"] input:focus-visible,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div:focus-within {
    outline: 2px solid #3B82F6 !important;
    outline-offset: 2px !important;
}

/* card hover */
.st-key-hero_card {
    transition: all 0.15s ease;
}
.st-key-hero_card:hover {
    border-color: rgba(255,255,255,0.18) !important;
    transform: translateY(-2px);
}

/* inputs and dropdown */
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background-color: rgba(255,255,255,0.04) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
    color: #F1F5F9 !important;
    transition: all 0.15s ease !important;
}
[data-testid="stTextInput"] input:hover,
[data-testid="stTextInput"] input:focus,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div:hover,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div:focus-within {
    border-color: #3B82F6 !important;
}
</style>
""", unsafe_allow_html=True)

try:
    chip_hoods, gathering_total, _chip_ms = top_chip_neighborhoods()
    badge_text = f"San Francisco · {gathering_total:,} places to gather"
except Exception:
    chip_hoods = []
    badge_text = "San Francisco"

st.markdown(f"<div class='hero-badge'>{badge_text}</div>", unsafe_allow_html=True)
st.markdown("<div class='page-title'>SF Gathering Places</div>", unsafe_allow_html=True)
st.markdown("<div class='page-subtitle'>Places you can walk into alone, where you'll end up around people.</div>", unsafe_allow_html=True)

if "neighborhood" not in st.session_state:
    st.session_state.neighborhood = None
if "anchor" not in st.session_state:
    st.session_state.anchor = None
if "prev_pills" not in st.session_state:
    st.session_state.prev_pills = None
if "prev_dropdown" not in st.session_state:
    st.session_state.prev_dropdown = None
if "prev_search" not in st.session_state:
    st.session_state.prev_search = ""
if "prev_suggestion" not in st.session_state:
    st.session_state.prev_suggestion = None
if "pending_suggestions" not in st.session_state:
    st.session_state.pending_suggestions = []

cur_pills = st.session_state.get("neighborhood_pills")
cur_dropdown = st.session_state.get("neighborhood_dropdown")
cur_search = st.session_state.get("search_box", "")
cur_suggestion = st.session_state.get("address_suggestion")

search_warning = None

if cur_pills != st.session_state.prev_pills and cur_pills is not None:
    st.session_state.neighborhood = cur_pills
    st.session_state.anchor = None
    st.session_state.neighborhood_dropdown = None
    st.session_state.search_box = ""
    st.session_state.pending_suggestions = []
elif cur_suggestion != st.session_state.prev_suggestion and cur_suggestion is not None:
    match = next((s for s in st.session_state.pending_suggestions if s[0] == cur_suggestion), None)
    if match:
        _addr, s_lat, s_lon = match
        st.session_state.anchor = {"lat": s_lat, "lon": s_lon, "label": cur_suggestion}
        st.session_state.neighborhood = None
        st.session_state.neighborhood_pills = None
        st.session_state.neighborhood_dropdown = None
    st.session_state.pending_suggestions = []
elif cur_dropdown != st.session_state.prev_dropdown and cur_dropdown is not None:
    st.session_state.neighborhood = cur_dropdown
    st.session_state.anchor = None
    st.session_state.neighborhood_pills = None
    st.session_state.search_box = ""
    st.session_state.pending_suggestions = []
elif cur_search != st.session_state.prev_search and cur_search:
    st.session_state.pending_suggestions = []
    cleaned = cur_search.strip()
    if cleaned.isdigit() and len(cleaned) == 5:
        if cleaned in SF_ZIP_COORDS:
            lon, lat = SF_ZIP_COORDS[cleaned]
            st.session_state.anchor = {"lat": lat, "lon": lon, "label": f"{cleaned}, San Francisco, CA"}
            st.session_state.neighborhood = None
            st.session_state.neighborhood_pills = None
            st.session_state.neighborhood_dropdown = None
        else:
            result = geocode(f"{cleaned}, CA, USA")
            if result:
                lat, lon, display = result
                st.session_state.anchor = {"lat": lat, "lon": lon, "label": short_display(display)}
                st.session_state.neighborhood = None
                st.session_state.neighborhood_pills = None
                st.session_state.neighborhood_dropdown = None
            else:
                search_warning = "We couldn't find that zip code. Try a different one or pick a neighborhood above."
    else:
        matched_hood = match_neighborhood_name(cleaned)
        if matched_hood:
            try:
                coords = neighborhood_anchor_coords().get(matched_hood)
            except Exception:
                coords = None
            if coords:
                lon, lat = coords
                st.session_state.anchor = {"lat": lat, "lon": lon, "label": matched_hood}
                st.session_state.neighborhood = None
                st.session_state.neighborhood_pills = None
                st.session_state.neighborhood_dropdown = None
            else:
                search_warning = "We're having trouble reaching our data right now. Please try again in a moment."
        else:
            suggestions = fetch_address_suggestions(cleaned) if len(cleaned) >= 3 else []
            if suggestions:
                st.session_state.pending_suggestions = suggestions
            else:
                result = geocode(cleaned)
                if result:
                    lat, lon, display = result
                    st.session_state.anchor = {"lat": lat, "lon": lon, "label": short_display(display)}
                    st.session_state.neighborhood = None
                    st.session_state.neighborhood_pills = None
                    st.session_state.neighborhood_dropdown = None
                else:
                    search_warning = "We couldn't find that address. Try a zip code or pick a neighborhood above."

st.session_state.prev_pills = cur_pills
st.session_state.prev_dropdown = cur_dropdown
st.session_state.prev_search = cur_search
st.session_state.prev_suggestion = cur_suggestion

if chip_hoods:
    st.markdown("<div class='chip-caption'>The neighborhoods with the most places to gather</div>", unsafe_allow_html=True)

st.pills(
    "Pick a neighborhood",
    options=chip_hoods,
    selection_mode="single",
    key="neighborhood_pills",
    label_visibility="collapsed",
)

st.selectbox(
    "Or pick another neighborhood",
    options=sorted(ALL_HOODS),
    index=None,
    placeholder="Or pick another neighborhood",
    key="neighborhood_dropdown",
    label_visibility="collapsed",
)

st.text_input(
    "Your address, neighborhood, or zip", placeholder="Your address, neighborhood, or zip",
    key="search_box", label_visibility="collapsed",
)
if search_warning:
    st.warning(search_warning)

if st.session_state.pending_suggestions:
    suggestion_labels = [addr for addr, _lat, _lon in st.session_state.pending_suggestions]
    st.selectbox(
        "Matching SF addresses", options=suggestion_labels, index=None,
        placeholder="Matching SF addresses — pick one",
        key="address_suggestion",
    )

neighborhood = st.session_state.neighborhood
anchor = st.session_state.anchor
if not neighborhood and not anchor:
    empty_state("Pick a neighborhood above to get started.")
    st.stop()

st.session_state.last_error = None
try:
    render_body(neighborhood=neighborhood, anchor=anchor)
except Exception as e:
    if st.session_state.get("last_error") is None:
        record_error("render (no SQL captured)", e)
    st.error("We're having trouble reaching our data right now. Please try again in a moment.")
    try:
        total_count, _total_ms = total_business_count()
        st.markdown(
            f"<div class='footer-note'>Searched {total_count:,} records in San Francisco's business registry.</div>",
            unsafe_allow_html=True,
        )
    except Exception:
        st.markdown("<div class='footer-note'>Couldn't reach the database just now.</div>", unsafe_allow_html=True)
