import os, time
import requests
import streamlit as st
import pandas as pd
import clickhouse_connect, certifi, psycopg2
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="SF Gathering Places", layout="centered")

GATHERING_NAICS_PREFIXES = ("7225", "7224", "7139", "4512", "4592")


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


def q(sql, params=None):
    t0 = time.time()
    r = ch().query(sql, parameters=params or {})
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, r.column_names, ms


def naics_filter_sql():
    return " OR ".join(f"startsWith(self_reported_naics_code, '{p}')" for p in GATHERING_NAICS_PREFIXES)


timings = []

st.title("SF Gathering Places")
st.write("Find a spot near you and invite a friend to meet up.")

zip_code = st.text_input("Your zip code", placeholder="94115").strip()

if not zip_code:
    st.info("Enter a San Francisco zip code above to get started.")
    st.stop()

if not (zip_code.isdigit() and len(zip_code) == 5):
    st.warning("That doesn't look like a zip code. Try one like 94115.")
    st.stop()

hood_rows, _, hood_ms = q("""
    SELECT neighborhoods_analysis_boundaries AS hood, count() AS n
    FROM sf_business
    WHERE business_zip = {zip:String} AND hood != '' AND hood != 'Multiple'
    GROUP BY hood
    ORDER BY n DESC
    LIMIT 1
""", {"zip": zip_code})
timings.append(("looking up your area", hood_ms))

if not hood_rows:
    st.warning("We couldn't find that zip code. Try one like 94115.")
    st.stop()

neighborhood = hood_rows[0][0]

st.header(f"You're in {neighborhood}.")

count_rows, _, count_ms = q(f"""
    SELECT count() FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {{nb:String}}
      AND location_end_date = ''
      AND ({naics_filter_sql()})
""", {"nb": neighborhood})
timings.append(("counting places near you", count_ms))
place_count = count_rows[0][0]

all_rows, _, all_ms = q(f"""
    SELECT neighborhoods_analysis_boundaries AS hood,
           countIf(location_end_date = '' AND ({naics_filter_sql()})) AS n
    FROM sf_business
    WHERE hood != '' AND hood != 'Multiple'
    GROUP BY hood
    ORDER BY n DESC
""")
timings.append(("comparing to the rest of the city", all_ms))
all_df = pd.DataFrame(all_rows, columns=["hood", "n"]).reset_index(drop=True)
all_df["rank"] = range(1, len(all_df) + 1)
this_row = all_df[all_df["hood"] == neighborhood]

st.markdown(f"## {place_count} places to gather within your zip code")

if not this_row.empty and len(all_df) >= 3:
    rank = int(this_row.iloc[0]["rank"])
    pct = rank / len(all_df)
    if pct <= 1 / 3:
        phrase, third = "more than most of", "top"
    elif pct <= 2 / 3:
        phrase, third = "about the same as", "middle"
    else:
        phrase, third = "fewer than most of", "bottom"
    st.write(f"That's {phrase} San Francisco — you're in the {third} third.")

st.divider()

st.subheader(f"Businesses closing each year in {neighborhood}")
year_rows, _, year_ms = q("""
    SELECT toYear(parseDateTimeBestEffortOrNull(location_end_date)) AS yr, count() AS n
    FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {nb:String}
      AND location_end_date != ''
      AND toYear(parseDateTimeBestEffortOrNull(location_end_date)) BETWEEN 2015 AND 2026
    GROUP BY yr
    ORDER BY yr
""", {"nb": neighborhood})
timings.append(("building the chart", year_ms))
year_df = pd.DataFrame(year_rows, columns=["Year", "Businesses closed"]).set_index("Year")
year_df = year_df.reindex(range(2015, 2027), fill_value=0)
st.bar_chart(year_df)

st.divider()

st.subheader("Places near you")
st.caption("Restaurants, bars, cafés, gyms, and bookstores that are still open.")

places_rows, _, places_ms = q(f"""
    SELECT uniqueid, dba_name, full_business_address
    FROM sf_business
    WHERE neighborhoods_analysis_boundaries = {{nb:String}}
      AND location_end_date = ''
      AND ({naics_filter_sql()})
    ORDER BY dba_name
""", {"nb": neighborhood})
timings.append(("finding places near you", places_ms))

if places_rows:
    for _, name, address in places_rows:
        st.markdown(f"- **{name}** — {address}")
else:
    st.write("We couldn't find any open places to gather in this area yet.")

st.divider()

st.subheader("Invite a friend")

if places_rows:
    place_labels = [f"{r[1]} — {r[2]}" for r in places_rows]
    place_idx = st.selectbox("Pick a place", range(len(place_labels)), format_func=lambda i: place_labels[i])
    chosen_id, chosen_name, chosen_addr = places_rows[place_idx]
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
