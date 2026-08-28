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


@st.cache_data(ttl=600)
def open_places(neighborhood):
    sql = f"""
        SELECT uniqueid, dba_name, full_business_address,
               self_reported_naics_code, location_start_date
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
          AND location_end_date = ''
          AND city = 'San Francisco'
          AND ({naics_filter_sql()})
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


@st.cache_data(ttl=600)
def zip_to_neighborhood(zip_code):
    sql = """
        SELECT neighborhoods_analysis_boundaries AS hood, count() AS n
        FROM sf_business
        WHERE business_zip = {zip:String} AND hood != '' AND hood != 'Multiple'
          AND city = 'San Francisco'
        GROUP BY hood
        ORDER BY n DESC
        LIMIT 1
    """
    t0 = time.time()
    try:
        r = ch().query(sql, parameters={"zip": zip_code})
    except Exception as e:
        record_error(sql, e)
        raise
    ms = int((time.time() - t0) * 1000)
    return (r.result_rows[0][0] if r.result_rows else None), ms


@st.cache_data(ttl=600)
def top_chip_neighborhoods(n=8):
    sql = f"""
        SELECT neighborhoods_analysis_boundaries AS hood, dba_name, full_business_address
        FROM sf_business
        WHERE hood != '' AND hood != 'Multiple'
          AND location_end_date = ''
          AND city = 'San Francisco'
          AND ({naics_filter_sql()})
    """
    t0 = time.time()
    try:
        r = ch().query(sql)
    except Exception as e:
        record_error(sql, e)
        raise
    ms = int((time.time() - t0) * 1000)
    counts = {}
    for hood, name, addr in r.result_rows:
        if is_junk_name(name, addr):
            continue
        counts[hood] = counts.get(hood, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [hood for hood, _ in ranked[:n]], ms


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


def render_neighborhood(neighborhood):
    places_rows, places_ms = open_places(neighborhood)
    dismissed = fetch_dismissed_names()
    candidates = [p for p in places_rows if p[1] not in dismissed and not is_junk_name(p[1], p[2])]

    if not candidates:
        empty_state("No good matches here yet, try another neighborhood.")
        return None

    hero_id, hero_name, hero_addr, hero_naics, hero_start = candidates[0]
    record_shown(hero_name, neighborhood)

    with st.container(border=True, key="hero_card"):
        st.markdown(f"<div class='hero-name'>{hero_name}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='muted-text'>{hero_addr}</div>", unsafe_allow_html=True)
        meta_bits = [category_label(hero_naics)]
        if hero_start:
            meta_bits.append(f"open since {hero_start[:4]}")
        st.markdown(f"<div class='muted-text'>{' · '.join(meta_bits)}</div>", unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        if col1.button("Save this", type="primary", key="save_this"):
            try:
                pg_exec(
                    "INSERT INTO saved_places (place_name, address, neighborhood) VALUES (%s, %s, %s)",
                    (hero_name, hero_addr, neighborhood),
                )
                st.toast("Saved")
            except Exception:
                st.warning("Couldn't save that just now. Please try again.")
        if col2.button("Not for me", key="not_for_me"):
            try:
                pg_exec("INSERT INTO dismissed_places (place_name) VALUES (%s)", (hero_name,))
            except Exception:
                st.warning("Couldn't update that just now. Please try again.")
            st.rerun()

    st.markdown(
        f"<div class='muted-text' style='margin:-4px 0 4px;'>One of {len(candidates):,} places still open in {neighborhood}.</div>",
        unsafe_allow_html=True,
    )

    with st.container(border=True, key="invite_card"):
        section_label("Send this to a friend")
        if "draft" not in st.session_state:
            st.session_state.draft = ""
        if "writing_invite" not in st.session_state:
            st.session_state.writing_invite = False

        if st.button("Write the invite", type="primary", key="write_invite", disabled=st.session_state.writing_invite):
            st.session_state.writing_invite = True
            with st.spinner("Writing your invite…"):
                api_key = secret("GEMINI_API_KEY")
                prompt = (
                    f"Write a short, casual, low-pressure text message (2-3 sentences max) inviting a friend "
                    f"to hang out at '{hero_name}' ({hero_addr}) in San Francisco. "
                    f"Keep it warm and easygoing, no pressure to say yes, no exclamation-point overload. "
                    f"Just output the text message itself, nothing else."
                )
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"gemini-flash-lite-latest:generateContent?key={api_key}"
                )
                try:
                    resp = requests.post(
                        url,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    st.session_state.draft = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                except Exception:
                    st.error("Something went wrong writing your invite. Please try again.")
            st.session_state.writing_invite = False

        if st.session_state.draft:
            st.code(st.session_state.draft, language=None)

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
    st.markdown(
        f"<div class='footer-note'>Searched {total_count:,} records in San Francisco's business registry "
        f"→ {len(candidates):,} still open in {neighborhood} → 1 picked, in {places_ms} ms.</div>",
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

/* pills (neighborhood chips) */
.st-key-neighborhood_pills [data-testid="stButtonGroup"] {
    display: flex !important;
    justify-content: center !important;
    flex-wrap: wrap !important;
    gap: 10px !important;
}
.st-key-neighborhood_pills button {
    border-radius: 999px !important;
    padding: 9px 18px !important;
    font-size: 14px !important;
    white-space: nowrap !important;
    width: auto !important;
    border: 1px solid rgba(255,255,255,0.10) !important;
    background-color: rgba(255,255,255,0.05) !important;
    color: #CBD5E1 !important;
    transition: all 0.15s ease !important;
    cursor: pointer !important;
}
.st-key-neighborhood_pills button:hover {
    transform: translateY(-1px);
    border-color: rgba(255,255,255,0.22) !important;
    background-color: rgba(255,255,255,0.09) !important;
}
.st-key-neighborhood_pills button:active {
    transform: scale(0.985);
}
.st-key-neighborhood_pills button[aria-pressed="true"],
.st-key-neighborhood_pills button[aria-checked="true"],
.st-key-neighborhood_pills button[aria-selected="true"] {
    background-color: #3B82F6 !important;
    color: #FFFFFF !important;
    border: none !important;
    box-shadow: 0 0 24px rgba(59,130,246,0.45) !important;
}

/* buttons */
.stButton>button {
    border-radius: 10px;
    transition: all 0.15s ease;
    cursor: pointer;
}
.stButton>button:active {
    transform: scale(0.985);
}
.stButton>button[kind="primary"] {
    background-color: #3B82F6;
    color: #FFFFFF;
    border-radius: 10px;
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
.stButton>button[kind="secondary"] {
    background-color: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.14);
    color: #CBD5E1;
    border-radius: 10px;
    padding: 11px 22px;
}
.stButton>button[kind="secondary"]:hover {
    background-color: rgba(255,255,255,0.10);
    border-color: rgba(255,255,255,0.28);
    color: #F1F5F9;
    transform: translateY(-1px);
}

/* keyboard focus */
.stButton>button:focus-visible,
.st-key-neighborhood_pills button:focus-visible,
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
    border-color: rgba(255,255,255,0.16) !important;
    transform: translateY(-1px);
}

/* inputs and dropdown */
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background-color: rgba(255,255,255,0.04) !important;
    border: 1px solid rgba(255,255,255,0.10) !important;
    border-radius: 10px !important;
    color: #F1F5F9 !important;
}
</style>
""", unsafe_allow_html=True)

try:
    badge_count, _badge_ms = total_business_count()
    badge_text = f"San Francisco · {badge_count:,} places"
except Exception:
    badge_text = "San Francisco"

st.markdown(f"<div class='hero-badge'>{badge_text}</div>", unsafe_allow_html=True)
st.markdown("<div class='page-title'>SF Gathering Places</div>", unsafe_allow_html=True)
st.markdown("<div class='page-subtitle'>Find one place near you, and someone to go with.</div>", unsafe_allow_html=True)

if "neighborhood" not in st.session_state:
    st.session_state.neighborhood = None
if "prev_pills" not in st.session_state:
    st.session_state.prev_pills = None
if "prev_dropdown" not in st.session_state:
    st.session_state.prev_dropdown = None
if "prev_zip" not in st.session_state:
    st.session_state.prev_zip = ""

cur_pills = st.session_state.get("neighborhood_pills")
cur_dropdown = st.session_state.get("neighborhood_dropdown")
cur_zip = st.session_state.get("zip_input", "")

zip_warning = None

if cur_pills != st.session_state.prev_pills and cur_pills is not None:
    st.session_state.neighborhood = cur_pills
    st.session_state.neighborhood_dropdown = None
    st.session_state.zip_input = ""
elif cur_dropdown != st.session_state.prev_dropdown and cur_dropdown is not None:
    st.session_state.neighborhood = cur_dropdown
    st.session_state.neighborhood_pills = None
    st.session_state.zip_input = ""
elif cur_zip != st.session_state.prev_zip and cur_zip:
    cleaned = cur_zip.strip()
    if cleaned.isdigit() and len(cleaned) == 5:
        zip_int = int(cleaned)
        if not (94102 <= zip_int <= 94134 or zip_int == 94158):
            zip_warning = "That's not a San Francisco zip code — this only covers SF for now."
        else:
            match, _zip_ms = zip_to_neighborhood(cleaned)
            if match:
                st.session_state.neighborhood = match
                st.session_state.neighborhood_pills = None
                st.session_state.neighborhood_dropdown = None
            else:
                zip_warning = "We don't have data for that zip code yet."
    else:
        zip_warning = "Enter a 5-digit San Francisco zip code."

st.session_state.prev_pills = cur_pills
st.session_state.prev_dropdown = cur_dropdown
st.session_state.prev_zip = cur_zip

try:
    chip_hoods, _chip_ms = top_chip_neighborhoods()
except Exception:
    chip_hoods = []

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

st.text_input("Or enter a zip code", placeholder="94110", key="zip_input", label_visibility="collapsed")
if zip_warning:
    st.warning(zip_warning)

neighborhood = st.session_state.neighborhood
if not neighborhood:
    empty_state("Pick a neighborhood above to get started.")
    st.stop()

st.session_state.last_error = None
try:
    render_neighborhood(neighborhood)
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
