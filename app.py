import os, re, time, datetime
import requests
import streamlit as st
import pandas as pd
import clickhouse_connect, certifi, psycopg2
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="SF Gathering Places", page_icon="🫂", layout="wide")

GATHERING_NAICS_PREFIXES = ("7225", "7224", "7139", "4512", "4592")
NEIGHBORHOOD_CHIPS = [
    ("Mission", "Mission"),
    ("Chinatown", "Chinatown"),
    ("Japantown", "Japantown"),
    ("SoMa", "South of Market"),
    ("Sunset", "Sunset/Parkside"),
    ("Bayview", "Bayview Hunters Point"),
    ("Marina", "Marina"),
    ("Castro", "Castro/Upper Market"),
]
CHIP_LABELS = [label for label, _ in NEIGHBORHOOD_CHIPS]
HOOD_BY_LABEL = dict(NEIGHBORHOOD_CHIPS)
LABEL_BY_HOOD = {hood: label for label, hood in NEIGHBORHOOD_CHIPS}
POINT_RE = re.compile(r"POINT \(([-\d.]+) ([-\d.]+)\)")


def secret(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key)


@st.cache_resource
def ch():
    return clickhouse_connect.get_client(
        host=secret("CH_HOST"), port=8443, username="default",
        password=secret("CH_PASSWORD"), secure=True, ca_cert=certifi.where(),
    )


@st.cache_resource
def pg():
    conn = psycopg2.connect(secret("DATABASE_URL"))
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS attendances (
                id SERIAL PRIMARY KEY,
                place_id TEXT,
                place_name TEXT,
                night TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
    return conn


def naics_filter_sql():
    return " OR ".join(f"startsWith(self_reported_naics_code, '{p}')" for p in GATHERING_NAICS_PREFIXES)


@st.cache_data(ttl=300)
def all_neighborhood_names():
    t0 = time.time()
    r = ch().query("""
        SELECT DISTINCT neighborhoods_analysis_boundaries
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries != '' AND neighborhoods_analysis_boundaries != 'Multiple'
    """)
    ms = int((time.time() - t0) * 1000)
    return [row[0] for row in r.result_rows], ms


@st.cache_data(ttl=300)
def zip_to_neighborhood(zip_code):
    t0 = time.time()
    r = ch().query("""
        SELECT neighborhoods_analysis_boundaries AS hood, count() AS n
        FROM sf_business
        WHERE business_zip = {zip:String} AND hood != '' AND hood != 'Multiple'
        GROUP BY hood
        ORDER BY n DESC
        LIMIT 1
    """, parameters={"zip": zip_code})
    ms = int((time.time() - t0) * 1000)
    return (r.result_rows[0][0] if r.result_rows else None), ms


@st.cache_data(ttl=300)
def open_place_count(neighborhood):
    t0 = time.time()
    r = ch().query(f"""
        SELECT countIf(location_end_date = '' AND ({naics_filter_sql()})) AS open_now
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
    """, parameters={"nb": neighborhood})
    ms = int((time.time() - t0) * 1000)
    return r.result_rows[0][0], ms


@st.cache_data(ttl=300)
def citywide_gathering_counts():
    t0 = time.time()
    r = ch().query(f"""
        SELECT neighborhoods_analysis_boundaries AS hood,
               countIf(location_end_date = '' AND ({naics_filter_sql()})) AS n
        FROM sf_business
        WHERE hood != '' AND hood != 'Multiple'
        GROUP BY hood
        ORDER BY n DESC
    """)
    ms = int((time.time() - t0) * 1000)
    return pd.DataFrame(r.result_rows, columns=["hood", "n"]).reset_index(drop=True), ms


@st.cache_data(ttl=300)
def open_places(neighborhood):
    t0 = time.time()
    r = ch().query(f"""
        SELECT uniqueid, dba_name, full_business_address, location,
               self_reported_naics_code, location_start_date
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
          AND location_end_date = ''
          AND ({naics_filter_sql()})
        ORDER BY dba_name
    """, parameters={"nb": neighborhood})
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, ms


def fetch_rsvp_counts():
    with pg().cursor() as c:
        c.execute("SELECT place_id, count(*) FROM attendances GROUP BY place_id")
        return dict(c.fetchall())


def build_place_order(places_rows, rsvp_counts):
    with_rsvp = [i for i, p in enumerate(places_rows) if rsvp_counts.get(p[0], 0) > 0]
    with_rsvp.sort(key=lambda i: -rsvp_counts.get(places_rows[i][0], 0))
    without_rsvp = [i for i in range(len(places_rows)) if i not in with_rsvp]
    return with_rsvp + without_rsvp


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


UPCOMING_DAYS = ["Tue", "Wed", "Thu", "Fri", "Sat"]
WEEKDAY_INDEX = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def upcoming_date(day_abbr):
    today = datetime.date.today()
    delta = (WEEKDAY_INDEX[day_abbr] - today.weekday()) % 7
    return today + datetime.timedelta(days=delta)


def fuzzy_match_neighborhood(text, names):
    q = text.strip().lower()
    for n in names:
        if n.lower() == q:
            return n
    for n in names:
        if n.lower().startswith(q):
            return n
    for n in names:
        if q in n.lower():
            return n
    return None


def section_label(text):
    st.markdown(f"<div class='section-label'>{text}</div>", unsafe_allow_html=True)


def empty_state(text):
    st.markdown(f"<div class='empty-state'>{text}</div>", unsafe_allow_html=True)


def success_banner(text):
    st.markdown(f"<div class='success-banner'>{text}</div>", unsafe_allow_html=True)


def render_neighborhood(neighborhood, timings):
    places_rows, places_ms = open_places(neighborhood)
    timings.append(("finding places here", places_ms))

    if not places_rows:
        empty_state("No open places to gather here yet. Try another neighborhood.")
        return

    rsvp_counts = fetch_rsvp_counts()
    order = build_place_order(places_rows, rsvp_counts)

    if "hero_offset" not in st.session_state:
        st.session_state.hero_offset = {}
    offset = st.session_state.hero_offset.get(neighborhood, 0)
    hero_idx = order[offset % len(order)]
    hero_place = places_rows[hero_idx]
    hero_id, hero_name, hero_addr, _hero_location, hero_naics, hero_start = hero_place

    with st.container(border=True, key="hero_card"):
        section_label("Tonight's pick")
        st.markdown(f"<div class='hero-name'>{hero_name}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='muted-text'>{hero_addr}</div>", unsafe_allow_html=True)
        meta_bits = [category_label(hero_naics)]
        if hero_start:
            meta_bits.append(f"open since {hero_start[:4]}")
        st.markdown(f"<div class='muted-text'>{' · '.join(meta_bits)}</div>", unsafe_allow_html=True)
        if st.button("Show me another", key="show_another"):
            st.session_state.hero_offset[neighborhood] = offset + 1
            st.rerun()

    with st.container(border=True, key="going_card"):
        section_label("Who else is going")
        day_dates = {abbr: upcoming_date(abbr) for abbr in UPCOMING_DAYS}
        chosen_day = st.pills(
            "Pick a night",
            options=UPCOMING_DAYS,
            selection_mode="single",
            key="night_pills",
            label_visibility="collapsed",
        )
        if chosen_day:
            night_str = day_dates[chosen_day].isoformat()
            with pg().cursor() as c:
                c.execute(
                    "SELECT count(*) FROM attendances WHERE place_id = %s AND night = %s",
                    (hero_id, night_str),
                )
                going_count = c.fetchone()[0]
            st.markdown(
                f"<div class='going-count'>{going_count} {'person' if going_count == 1 else 'people'} said "
                f"they're going {chosen_day}.</div>",
                unsafe_allow_html=True,
            )
            if st.button("I'll be there", type="primary", key="im_going"):
                with pg().cursor() as c:
                    c.execute(
                        "INSERT INTO attendances (place_id, place_name, night) VALUES (%s, %s, %s)",
                        (hero_id, hero_name, night_str),
                    )
                st.rerun()
        else:
            st.markdown("<div class='muted-text'>Pick a night to see who's in.</div>", unsafe_allow_html=True)

    with st.container(border=True, key="invite_card"):
        section_label("Send this to a friend")
        if not chosen_day:
            st.markdown("<div class='muted-text'>Pick a night above first.</div>", unsafe_allow_html=True)
        else:
            if "draft" not in st.session_state:
                st.session_state.draft = ""

            if st.button("Write the invite", type="primary", key="write_invite"):
                api_key = secret("GEMINI_API_KEY")
                prompt = (
                    f"Write a short, casual, low-pressure text message (2-3 sentences max) inviting a friend "
                    f"to hang out at '{hero_name}' ({hero_addr}) in San Francisco this {chosen_day} "
                    f"({day_dates[chosen_day].strftime('%B %d')}). "
                    f"Keep it warm and easygoing, no pressure to say yes, no exclamation-point overload. "
                    f"Just output the text message itself, nothing else."
                )
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"gemini-flash-lite-latest:generateContent?key={api_key}"
                )
                t0 = time.time()
                try:
                    resp = requests.post(
                        url,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                        timeout=30,
                    )
                    gemini_ms = int((time.time() - t0) * 1000)
                    resp.raise_for_status()
                    data = resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    st.session_state.draft = text
                    timings.append(("writing your invite", gemini_ms))
                except Exception:
                    st.error("Something went wrong writing your invite. Please try again.")

            if st.session_state.draft:
                st.code(st.session_state.draft, language=None)

    place_count, count_ms = open_place_count(neighborhood)
    timings.append(("counting places here", count_ms))
    all_df, all_ms = citywide_gathering_counts()
    timings.append(("comparing to the rest of the city", all_ms))
    all_df["rank"] = range(1, len(all_df) + 1)
    this_row = all_df[all_df["hood"] == neighborhood]

    comparison = ""
    if not this_row.empty and len(all_df) >= 3:
        rank = int(this_row.iloc[0]["rank"])
        pct = rank / len(all_df)
        if pct <= 1 / 3:
            comparison = "more than most of San Francisco"
        elif pct <= 2 / 3:
            comparison = "about the same as most of San Francisco"
        else:
            comparison = "fewer than most of San Francisco"
    st.markdown(
        f"<div class='muted-text' style='margin:16px 0;'>{place_count} places like this near you"
        + (f", {comparison}." if comparison else ".") + "</div>",
        unsafe_allow_html=True,
    )

    map_points = []
    for _, name, address, location, _naics, _start in places_rows:
        m = POINT_RE.match(location) if location else None
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
            map_points.append({"lat": lat, "lon": lon})
        if len(map_points) >= 500:
            break

    if map_points:
        with st.container(border=True, key="map_card"):
            st.map(pd.DataFrame(map_points), size=20, color="#0070F3")


st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

#MainMenu, footer, header,
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], [data-testid="stMainMenuButton"] {
    display: none !important;
}

html, body, [class*="css"] {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
}

.stApp { background-color: #FAFAFA; }

.block-container {
    max-width: 1080px;
    margin: 0 auto;
    padding: 48px 24px;
}

h1, h2, h3, h4, p, label, span, div { color: #0A0A0A; }

.page-title {
    font-size: 40px;
    font-weight: 700;
    letter-spacing: -0.03em;
    color: #0A0A0A;
    margin-bottom: 4px;
}
.page-subtitle {
    font-size: 16px;
    color: #666666;
    margin-bottom: 32px;
}
.section-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 600;
    color: #666666;
    margin-bottom: 12px;
}
.card-title {
    font-size: 16px;
    font-weight: 600;
    color: #0A0A0A;
    margin-bottom: 12px;
}
.muted-text {
    font-size: 16px;
    color: #666666;
}
.hero-name {
    font-size: 32px;
    font-weight: 700;
    color: #0A0A0A;
    line-height: 1.2;
    margin-bottom: 4px;
}
.going-count {
    font-size: 20px;
    font-weight: 600;
    color: #0F9D58;
    margin: 8px 0;
}

/* cards */
.st-key-hero_card, .st-key-going_card, .st-key-invite_card, .st-key-map_card {
    background: #FFFFFF !important;
    border: 1px solid #EAEAEA !important;
    border-radius: 12px !important;
    padding: 24px !important;
    margin-bottom: 20px !important;
    box-shadow: none !important;
}

.empty-state {
    text-align: center;
    color: #666666;
    padding: 48px 24px;
    border: 1px dashed #EAEAEA;
    border-radius: 12px;
    background: #FFFFFF;
}
.success-banner {
    background: #FFFFFF;
    border: 1px solid #EAEAEA;
    border-left: 3px solid #0F9D58;
    border-radius: 12px;
    padding: 16px 20px;
    color: #0A0A0A;
}

/* pills (neighborhood chips + night picker) */
.st-key-neighborhood_pills button, .st-key-night_pills button {
    border-radius: 8px !important;
    padding: 8px 16px !important;
    font-size: 14px !important;
    white-space: nowrap !important;
    border: 1px solid #EAEAEA !important;
    background-color: #FFFFFF !important;
    color: #0A0A0A !important;
}
.st-key-neighborhood_pills button[aria-pressed="true"],
.st-key-neighborhood_pills button[aria-checked="true"],
.st-key-neighborhood_pills button[aria-selected="true"],
.st-key-night_pills button[aria-pressed="true"],
.st-key-night_pills button[aria-checked="true"],
.st-key-night_pills button[aria-selected="true"] {
    background-color: #0070F3 !important;
    color: #FFFFFF !important;
    border: none !important;
}

/* buttons */
.stButton>button {
    border-radius: 8px;
}
.stButton>button[kind="primary"] {
    background-color: #0070F3;
    color: #FFFFFF;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: 500;
    border: none;
}
.stButton>button[kind="secondary"] {
    background-color: #FFFFFF;
    border: 1px solid #EAEAEA;
    color: #0A0A0A;
}
</style>
""", unsafe_allow_html=True)

timings = []

st.markdown("<div class='page-title'>SF Gathering Places</div>", unsafe_allow_html=True)
st.markdown("<div class='page-subtitle'>Find a spot near you and invite a friend to meet up.</div>", unsafe_allow_html=True)

if "neighborhood" not in st.session_state:
    st.session_state.neighborhood = None
if "last_search" not in st.session_state:
    st.session_state.last_search = ""

section_label("Pick a neighborhood")
pills_slot = st.empty()

search_text = st.text_input("Or type a zip code or neighborhood", placeholder="94110 or Mission")
if search_text and search_text != st.session_state.last_search:
    st.session_state.last_search = search_text
    try:
        names, names_ms = all_neighborhood_names()
        timings.append(("looking up neighborhoods", names_ms))
        cleaned = search_text.strip()
        match = None
        if cleaned.isdigit() and len(cleaned) == 5:
            zip_int = int(cleaned)
            if not (94102 <= zip_int <= 94134 or zip_int == 94158):
                st.warning("That's not a San Francisco zip code — this only covers SF for now. Try a neighborhood above.")
            else:
                match, zip_ms = zip_to_neighborhood(cleaned)
                timings.append(("matching your zip code", zip_ms))
                if not match:
                    st.warning("We don't have data for that zip code yet. Try a neighborhood above.")
        else:
            match = fuzzy_match_neighborhood(cleaned, names)
            if not match:
                st.warning(f"We couldn't match \"{search_text}\" to a San Francisco neighborhood.")
        if match:
            st.session_state.neighborhood = match
            st.session_state["neighborhood_pills"] = LABEL_BY_HOOD.get(match)
    except Exception:
        st.error("We're having trouble reaching our data right now. Please try again in a moment.")

selected_chip_label = pills_slot.pills(
    "Pick a neighborhood",
    options=CHIP_LABELS,
    selection_mode="single",
    key="neighborhood_pills",
    label_visibility="collapsed",
)
if selected_chip_label:
    st.session_state.neighborhood = HOOD_BY_LABEL[selected_chip_label]

neighborhood = st.session_state.neighborhood
if not neighborhood:
    empty_state("Pick a neighborhood above, or type a zip code or neighborhood name, to get started.")
    st.stop()

st.markdown(f"<div class='card-title' style='font-size:20px;margin:8px 0 20px;'>You're looking at {neighborhood}.</div>", unsafe_allow_html=True)

try:
    render_neighborhood(neighborhood, timings)
except Exception:
    st.error("We're having trouble reaching our data right now. Please try again in a moment.")

st.caption(" · ".join(f"{label}: {ms} ms" for label, ms in timings))

health_parts = []
try:
    t0 = time.time()
    ch().query("SELECT 1")
    ch_ms = int((time.time() - t0) * 1000)
    health_parts.append(f"ClickHouse: connected ({ch_ms} ms)")
except Exception as e:
    health_parts.append(f"ClickHouse: unreachable — {e}")

try:
    with pg().cursor() as c:
        c.execute("SELECT count(*) FROM attendances")
        rsvp_count = c.fetchone()[0]
    health_parts.append(f"Postgres: connected — {rsvp_count} saved RSVPs")
except Exception as e:
    health_parts.append(f"Postgres: unreachable — {e}")

st.caption(" · ".join(health_parts))
