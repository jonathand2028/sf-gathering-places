import os, time, json
import requests
import streamlit as st
import pandas as pd
import clickhouse_connect, certifi, psycopg2
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="SF Gathering Places", layout="wide")

GATHERING_NAICS_PREFIXES = ("7225", "7224", "7139", "4512", "4592")
GATHERING_LABEL = "restaurants, bars, cafes, gyms, and bookstores"


@st.cache_resource
def ch():
    return clickhouse_connect.get_client(
        host=os.getenv("CH_HOST"), port=8443, username="default",
        password=os.getenv("CH_PASSWORD"), secure=True, ca_cert=certifi.where(),
    )


@st.cache_resource
def pg():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_neighborhoods (
                id SERIAL PRIMARY KEY,
                hood TEXT,
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
    return conn


def q(sql, params=None):
    t0 = time.time()
    r = ch().query(sql, parameters=params or {})
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, r.column_names, ms


def naics_filter_sql():
    return " OR ".join(f"startsWith(self_reported_naics_code, '{p}')" for p in GATHERING_NAICS_PREFIXES)


st.title("SF Gathering Places")
st.caption("Find a still-open neighborhood spot and invite a friend.")

# ---------- sidebar: saved neighborhoods + table counts ----------
with pg().cursor() as c:
    c.execute("SELECT count(*) FROM attendances")
    attendance_count = c.fetchone()[0]
    c.execute("SELECT count(*) FROM saved_neighborhoods")
    saved_count = c.fetchone()[0]
    c.execute("SELECT hood, note, created_at FROM saved_neighborhoods ORDER BY created_at DESC")
    saved_rows = c.fetchall()

with st.sidebar:
    st.header("Saved neighborhoods")
    st.caption(f"{saved_count} saved · {attendance_count} RSVPs")
    if saved_rows:
        for hood, note, created_at in saved_rows:
            st.markdown(f"**{hood}**")
            st.caption(note if note else "_no note_")
    else:
        st.caption("Nothing saved yet.")

# ---------- neighborhood dropdown ----------
nb_rows, _, nb_ms = q("""
    SELECT DISTINCT neighborhoods_analysis_boundaries
    FROM sf_business
    WHERE neighborhoods_analysis_boundaries != ''
    ORDER BY 1
""")
neighborhoods = [r[0] for r in nb_rows]
st.caption(f"loaded {len(neighborhoods)} neighborhoods in {nb_ms} ms")

neighborhood = st.selectbox("Pick a neighborhood", neighborhoods)

save_col1, save_col2 = st.columns([3, 1])
note = save_col1.text_input("One-line note (optional)", key="note_input", label_visibility="collapsed",
                             placeholder="One-line note about this neighborhood (optional)")
if save_col2.button(f"Save '{neighborhood}'"):
    with pg().cursor() as c:
        c.execute("INSERT INTO saved_neighborhoods (hood, note) VALUES (%s, %s)", (neighborhood, note))
    st.rerun()

st.divider()

# ---------- section 1: open / closed / rank / closures-per-year ----------
st.header("1. Neighborhood health")

open_rows, _, open_ms = q("""
    SELECT count() FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {nb:String} AND location_end_date = ''
""", {"nb": neighborhood})
open_count = open_rows[0][0]

closed_since_rows, _, closed_since_ms = q("""
    SELECT count() FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {nb:String}
      AND location_end_date != ''
      AND parseDateTimeBestEffortOrNull(location_end_date) >= toDate('2019-01-01')
""", {"nb": neighborhood})
closed_since_count = closed_since_rows[0][0]

rank_rows, _, rank_ms = q("""
    SELECT neighborhoods_analysis_boundaries,
           countIf(location_end_date != '') AS closed,
           count() AS total,
           closed / total AS pct_lost
    FROM sf_business
    WHERE neighborhoods_analysis_boundaries != ''
    GROUP BY neighborhoods_analysis_boundaries
    HAVING total >= 20
    ORDER BY pct_lost DESC
""")
rank_df = pd.DataFrame(rank_rows, columns=["neighborhood", "closed", "total", "pct_lost"])
rank_df["rank"] = range(1, len(rank_df) + 1)
this_rank_row = rank_df[rank_df["neighborhood"] == neighborhood]

c1, c2, c3 = st.columns(3)
c1.metric("Still open", f"{open_count:,}", help=f"{open_ms} ms")
c2.metric("Closed since 2019", f"{closed_since_count:,}", help=f"{closed_since_ms} ms")
if not this_rank_row.empty:
    rnk = int(this_rank_row.iloc[0]["rank"])
    pct = this_rank_row.iloc[0]["pct_lost"] * 100
    c3.metric(
        "Rank by % lost",
        f"#{rnk} of {len(rank_df)}",
        help=f"{pct:.1f}% of all businesses ever registered here have closed ({rank_ms} ms)",
    )
else:
    c3.metric("Rank by % lost", "n/a (small sample)", help=f"{rank_ms} ms")

year_rows, _, year_ms = q("""
    SELECT toYear(parseDateTimeBestEffortOrNull(location_end_date)) AS yr, count() AS n
    FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {nb:String}
      AND location_end_date != ''
      AND toYear(parseDateTimeBestEffortOrNull(location_end_date)) BETWEEN 2015 AND 2026
    GROUP BY yr
    ORDER BY yr
""", {"nb": neighborhood})
year_df = pd.DataFrame(year_rows, columns=["year", "closures"]).set_index("year")
year_df = year_df.reindex(range(2015, 2027), fill_value=0)
st.caption(f"closures per year query: {year_ms} ms")
st.bar_chart(year_df)

st.divider()

# ---------- section 2: still-open gathering places ----------
st.header("2. Still-open gathering places")
st.caption(f"Filtered to {GATHERING_LABEL} via self-reported NAICS code.")

places_rows, _, places_ms = q(f"""
    SELECT uniqueid, dba_name, full_business_address, self_reported_naics_code
    FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {{nb:String}}
      AND location_end_date = ''
      AND ({naics_filter_sql()})
    ORDER BY dba_name
""", {"nb": neighborhood})
st.caption(f"found {len(places_rows)} places in {places_ms} ms")

places_df = pd.DataFrame(places_rows, columns=["id", "name", "address", "naics"])
st.dataframe(places_df[["name", "address"]], width="stretch", hide_index=True)

st.divider()

# ---------- section 3: invite a friend ----------
st.header("3. Invite a friend")

if places_rows:
    place_labels = [f"{r[1]} — {r[2]}" for r in places_rows]
    place_idx = st.selectbox("Pick a place", range(len(place_labels)), format_func=lambda i: place_labels[i])
    chosen_id, chosen_name, chosen_addr, _ = places_rows[place_idx]
    evening = st.date_input("Pick an evening")

    if "draft" not in st.session_state:
        st.session_state.draft = ""

    if st.button("Draft invite with Gemini"):
        api_key = os.getenv("GEMINI_API_KEY")
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
            st.caption(f"Gemini responded in {gemini_ms} ms")
        except Exception as e:
            st.error(f"Gemini request failed: {e}")

    if st.session_state.draft:
        st.text_area("Draft invite", st.session_state.draft, height=100)

    if st.button("RSVP: I'm going"):
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
        st.success(f"You're in! {same_count} {'person has' if same_count == 1 else 'people have'} picked "
                   f"{chosen_name} on {evening.strftime('%A, %B %d')}.")
else:
    st.info("No open gathering places found in this neighborhood.")
