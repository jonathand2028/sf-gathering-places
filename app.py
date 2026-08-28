import os, re, time
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
def neighborhood_summary(neighborhood):
    t0 = time.time()
    r = ch().query(f"""
        SELECT
            countIf(location_end_date = '' AND ({naics_filter_sql()})) AS open_now,
            countIf(location_start_date != ''
                    AND parseDateTimeBestEffortOrNull(location_start_date) >= toDate('2019-01-01')
                    AND ({naics_filter_sql()})) AS opened_since_2019,
            countIf(location_end_date != ''
                    AND parseDateTimeBestEffortOrNull(location_end_date) >= toDate('2019-01-01')
                    AND ({naics_filter_sql()})) AS closed_since_2019
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
    """, parameters={"nb": neighborhood})
    ms = int((time.time() - t0) * 1000)
    open_now, opened_since, closed_since = r.result_rows[0]
    return {"open_now": open_now, "opened_since_2019": opened_since, "closed_since_2019": closed_since}, ms


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
def closures_per_year(neighborhood):
    t0 = time.time()
    r = ch().query("""
        SELECT toYear(parseDateTimeBestEffortOrNull(location_end_date)) AS yr, count() AS n
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {nb:String}
          AND location_end_date != ''
          AND toYear(parseDateTimeBestEffortOrNull(location_end_date)) BETWEEN 2015 AND 2026
        GROUP BY yr
        ORDER BY yr
    """, parameters={"nb": neighborhood})
    ms = int((time.time() - t0) * 1000)
    df = pd.DataFrame(r.result_rows, columns=["Year", "Businesses closed"]).set_index("Year")
    return df.reindex(range(2015, 2027), fill_value=0), ms


@st.cache_data(ttl=300)
def open_places(neighborhood):
    t0 = time.time()
    r = ch().query(f"""
        SELECT uniqueid, dba_name, full_business_address, location
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
          AND location_end_date = ''
          AND ({naics_filter_sql()})
        ORDER BY dba_name
    """, parameters={"nb": neighborhood})
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, ms


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
    summary, summary_ms = neighborhood_summary(neighborhood)
    timings.append(("counting places here", summary_ms))
    place_count = summary["open_now"]

    all_df, all_ms = citywide_gathering_counts()
    timings.append(("comparing to the rest of the city", all_ms))
    all_df["rank"] = range(1, len(all_df) + 1)
    this_row = all_df[all_df["hood"] == neighborhood]

    with st.container(border=True, key="overview_card"):
        section_label("Overview")
        st.markdown(f"<div class='headline-number'>{place_count} places to gather here</div>", unsafe_allow_html=True)
        if not this_row.empty and len(all_df) >= 3:
            rank = int(this_row.iloc[0]["rank"])
            pct = rank / len(all_df)
            if pct <= 1 / 3:
                phrase, third = "more than most of", "top"
            elif pct <= 2 / 3:
                phrase, third = "about the same as", "middle"
            else:
                phrase, third = "fewer than most of", "bottom"
            st.markdown(
                f"<div class='muted-text'>That's {phrase} San Francisco — you're in the {third} third.</div>",
                unsafe_allow_html=True,
            )

    places_rows, places_ms = open_places(neighborhood)
    timings.append(("finding places here", places_ms))

    map_points = []
    for _, name, address, location in places_rows:
        m = POINT_RE.match(location) if location else None
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
            map_points.append({"lat": lat, "lon": lon})
        if len(map_points) >= 500:
            break

    if map_points:
        with st.container(border=True, key="map_card"):
            section_label("Map")
            st.map(pd.DataFrame(map_points), size=20, color="#0070F3")

    with st.container(border=True, key="stats_card"):
        section_label("At a glance")
        stat_cols = st.columns(3)
        stat_cols[0].metric("Open right now", f"{summary['open_now']:,}")
        stat_cols[1].metric("Opened in the last few years", f"{summary['opened_since_2019']:,}")
        stat_cols[2].metric("Closed in the last few years", f"{summary['closed_since_2019']:,}")

    with st.container(border=True, key="chart_card"):
        section_label("Trends")
        st.markdown(f"<div class='card-title'>Businesses closing each year in {neighborhood}</div>", unsafe_allow_html=True)
        year_df, year_ms = closures_per_year(neighborhood)
        timings.append(("building the chart", year_ms))
        st.bar_chart(year_df)

    with st.container(border=True, key="places_card"):
        section_label("Places nearby")
        st.markdown("<div class='muted-text' style='margin-bottom:12px;'>Restaurants, bars, cafés, gyms, and bookstores that are still open.</div>", unsafe_allow_html=True)
        if places_rows:
            for _, name, address, _location in places_rows:
                st.markdown(f"- **{name}** — {address}")
        else:
            empty_state("We couldn't find any open places to gather in this area yet.")

    with st.container(border=True, key="invite_card"):
        section_label("Invite a friend")

        if places_rows:
            place_labels = [f"{r[1]} — {r[2]}" for r in places_rows]
            place_idx = st.selectbox("Pick a place", range(len(place_labels)), format_func=lambda i: place_labels[i])
            chosen_id, chosen_name, chosen_addr, _chosen_location = places_rows[place_idx]
            evening = st.date_input("Pick an evening")

            if "draft" not in st.session_state:
                st.session_state.draft = ""

            if st.button("Write an invite for me", type="primary"):
                api_key = secret("GEMINI_API_KEY")
                prompt = (
                    f"Write a short, casual, low-pressure text message (2-3 sentences max) inviting a friend "
                    f"to hang out at '{chosen_name}' ({chosen_addr}) in San Francisco on {evening.strftime('%A, %B %d')}. "
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
                st.write(st.session_state.draft)

            if st.button("I'm going", type="primary"):
                night_str = evening.isoformat()
                with pg().cursor() as c:
                    c.execute(
                        "INSERT INTO attendances (place_id, place_name, night) VALUES (%s, %s, %s)",
                        (chosen_id, chosen_name, night_str),
                    )
                    c.execute(
                        "SELECT count(*) FROM attendances WHERE place_id = %s AND night = %s",
                        (chosen_id, night_str),
                    )
                    same_count = c.fetchone()[0]
                success_banner(
                    f"You're going! {same_count} {'person has' if same_count == 1 else 'people have'} also picked "
                    f"{chosen_name} on {evening.strftime('%A, %B %d')}."
                )
        else:
            empty_state("No open places to gather here yet, so there's nothing to invite a friend to.")


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
.headline-number {
    font-size: 56px;
    font-weight: 700;
    color: #0A0A0A;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
    margin-bottom: 4px;
}

/* cards */
.st-key-overview_card, .st-key-map_card, .st-key-stats_card,
.st-key-chart_card, .st-key-places_card, .st-key-invite_card {
    background: #FFFFFF !important;
    border: 1px solid #EAEAEA !important;
    border-radius: 12px !important;
    padding: 24px !important;
    margin-bottom: 20px !important;
    box-shadow: none !important;
}

/* stat metrics */
[data-testid="stMetric"] { padding: 0 !important; background: transparent !important; border: none !important; }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; color: #0A0A0A; }
[data-testid="stMetricLabel"] { color: #666666; }

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

/* neighborhood pills */
.st-key-neighborhood_pills button {
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
.st-key-neighborhood_pills button[aria-selected="true"] {
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
