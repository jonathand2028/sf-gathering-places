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


def stat_card(label, value):
    return f"""
    <div style="background:white;border:1px solid #E3E7ED;border-radius:12px;
                padding:20px;text-align:center;">
      <div style="font-size:28px;font-weight:700;color:#101722;">{value}</div>
      <div style="font-size:14px;color:#5A6472;margin-top:4px;">{label}</div>
    </div>
    """


st.markdown("""
<style>
#MainMenu, header, footer {visibility: hidden;}
.stDeployButton {display: none;}
html, body, [class*="css"] { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.stApp { background-color: #F7F8FA; }
.block-container { max-width: 900px; margin: 0 auto; padding-top: 2rem; }
h1, h2, h3, p, label, span, div { color: #101722; }
.stButton>button {
    border-radius: 999px;
    padding: 0.4rem 1.2rem;
    border: 1px solid #E3E7ED;
    background-color: white;
    color: #101722;
}
.stButton>button[kind="primary"] {
    background-color: #2a78d6;
    border-color: #2a78d6;
    color: white;
}
</style>
""", unsafe_allow_html=True)

timings = []

st.title("SF Gathering Places")
st.write("Find a spot near you and invite a friend to meet up.")

if "neighborhood" not in st.session_state:
    st.session_state.neighborhood = None
if "last_search" not in st.session_state:
    st.session_state.last_search = ""

st.write("**Pick a neighborhood**")
chip_cols = st.columns(len(NEIGHBORHOOD_CHIPS))
for col, (label, hood) in zip(chip_cols, NEIGHBORHOOD_CHIPS):
    is_selected = st.session_state.neighborhood == hood
    if col.button(label, key=f"chip_{hood}", type="primary" if is_selected else "secondary"):
        st.session_state.neighborhood = hood

search_text = st.text_input("Or type a zip code or neighborhood", placeholder="94110 or Mission")
if search_text and search_text != st.session_state.last_search:
    st.session_state.last_search = search_text
    names, names_ms = all_neighborhood_names()
    timings.append(("looking up neighborhoods", names_ms))
    cleaned = search_text.strip()
    if cleaned.isdigit() and len(cleaned) == 5:
        match, zip_ms = zip_to_neighborhood(cleaned)
        timings.append(("matching your zip code", zip_ms))
    else:
        match = fuzzy_match_neighborhood(cleaned, names)
    if match:
        st.session_state.neighborhood = match
    else:
        st.warning(f"We couldn't match \"{search_text}\" to a San Francisco neighborhood or zip code.")

neighborhood = st.session_state.neighborhood
if not neighborhood:
    st.info("Pick a neighborhood above, or type a zip code or neighborhood name, to get started.")
    st.stop()

st.header(f"You're looking at {neighborhood}.")

summary, summary_ms = neighborhood_summary(neighborhood)
timings.append(("counting places here", summary_ms))
place_count = summary["open_now"]

all_df, all_ms = citywide_gathering_counts()
timings.append(("comparing to the rest of the city", all_ms))
all_df["rank"] = range(1, len(all_df) + 1)
this_row = all_df[all_df["hood"] == neighborhood]

st.markdown(
    f"<div style='font-size:56px;font-weight:700;color:#101722;line-height:1.1;'>"
    f"{place_count} places to gather here</div>",
    unsafe_allow_html=True,
)

context_sentence = ""
if not this_row.empty and len(all_df) >= 3:
    rank = int(this_row.iloc[0]["rank"])
    pct = rank / len(all_df)
    if pct <= 1 / 3:
        phrase, third = "more than most of", "top"
    elif pct <= 2 / 3:
        phrase, third = "about the same as", "middle"
    else:
        phrase, third = "fewer than most of", "bottom"
    context_sentence = f"That's {phrase} San Francisco — you're in the {third} third."
    st.markdown(
        f"<div style='font-size:16px;color:#5A6472;margin-top:-8px;'>{context_sentence}</div>",
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
    st.map(pd.DataFrame(map_points), size=20, color="#2a78d6")

st.write("")
card_cols = st.columns(3)
card_cols[0].markdown(stat_card("Open right now", f"{summary['open_now']:,}"), unsafe_allow_html=True)
card_cols[1].markdown(stat_card("Opened in the last few years", f"{summary['opened_since_2019']:,}"), unsafe_allow_html=True)
card_cols[2].markdown(stat_card("Closed in the last few years", f"{summary['closed_since_2019']:,}"), unsafe_allow_html=True)

st.write("")
st.subheader(f"Businesses closing each year in {neighborhood}")
year_df, year_ms = closures_per_year(neighborhood)
timings.append(("building the chart", year_ms))
st.bar_chart(year_df)

st.divider()

st.subheader("Places near you")
st.caption("Restaurants, bars, cafés, gyms, and bookstores that are still open.")

if places_rows:
    for _, name, address, _location in places_rows:
        st.markdown(f"- **{name}** — {address}")
else:
    st.write("We couldn't find any open places to gather in this area yet.")

st.divider()

st.subheader("Invite a friend")

if places_rows:
    place_labels = [f"{r[1]} — {r[2]}" for r in places_rows]
    place_idx = st.selectbox("Pick a place", range(len(place_labels)), format_func=lambda i: place_labels[i])
    chosen_id, chosen_name, chosen_addr, _chosen_location = places_rows[place_idx]
    evening = st.date_input("Pick an evening")

    if "draft" not in st.session_state:
        st.session_state.draft = ""

    if st.button("Write an invite for me"):
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

    if st.button("I'm going"):
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
        st.success(f"You're going! {same_count} {'person has' if same_count == 1 else 'people have'} also picked "
                   f"{chosen_name} on {evening.strftime('%A, %B %d')}.")
else:
    st.info("No open places to gather here yet, so there's nothing to invite a friend to.")

st.caption(" · ".join(f"{label}: {ms} ms" for label, ms in timings))
