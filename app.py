import os, time
import requests
import streamlit as st
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
            CREATE TABLE IF NOT EXISTS saved_places (
                id SERIAL PRIMARY KEY,
                place_name TEXT,
                address TEXT,
                neighborhood TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS dismissed_places (
                id SERIAL PRIMARY KEY,
                place_name TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
    return conn


def naics_filter_sql():
    return " OR ".join(f"startsWith(self_reported_naics_code, '{p}')" for p in GATHERING_NAICS_PREFIXES)


@st.cache_data(ttl=300)
def open_places(neighborhood):
    t0 = time.time()
    r = ch().query(f"""
        SELECT uniqueid, dba_name, full_business_address,
               self_reported_naics_code, location_start_date
        FROM sf_business
        WHERE neighborhoods_analysis_boundaries = {{nb:String}}
          AND location_end_date = ''
          AND ({naics_filter_sql()})
        ORDER BY dba_name
    """, parameters={"nb": neighborhood})
    ms = int((time.time() - t0) * 1000)
    return r.result_rows, ms


@st.cache_data(ttl=300)
def total_business_count():
    t0 = time.time()
    r = ch().query("SELECT count() FROM sf_business")
    ms = int((time.time() - t0) * 1000)
    return r.result_rows[0][0], ms


def fetch_dismissed_names():
    with pg().cursor() as c:
        c.execute("SELECT place_name FROM dismissed_places")
        return {row[0] for row in c.fetchall()}


def fetch_saved_places():
    with pg().cursor() as c:
        c.execute("SELECT id, place_name, address, neighborhood FROM saved_places ORDER BY created_at DESC")
        return c.fetchall()


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


def section_label(text):
    st.markdown(f"<div class='section-label'>{text}</div>", unsafe_allow_html=True)


def empty_state(text):
    st.markdown(f"<div class='empty-state'>{text}</div>", unsafe_allow_html=True)


def render_neighborhood(neighborhood):
    places_rows, _places_ms = open_places(neighborhood)
    dismissed = fetch_dismissed_names()
    candidates = [p for p in places_rows if p[1] not in dismissed]

    if not candidates:
        empty_state("No open places to gather here yet. Try another neighborhood.")
        return

    hero_id, hero_name, hero_addr, hero_naics, hero_start = candidates[0]

    with st.container(border=True, key="hero_card"):
        st.markdown(f"<div class='hero-name'>{hero_name}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='muted-text'>{hero_addr}</div>", unsafe_allow_html=True)
        meta_bits = [category_label(hero_naics)]
        if hero_start:
            meta_bits.append(f"open since {hero_start[:4]}")
        st.markdown(f"<div class='muted-text'>{' · '.join(meta_bits)}</div>", unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        if col1.button("Save this", type="primary", key="save_this"):
            with pg().cursor() as c:
                c.execute(
                    "INSERT INTO saved_places (place_name, address, neighborhood) VALUES (%s, %s, %s)",
                    (hero_name, hero_addr, neighborhood),
                )
        if col2.button("Not for me", key="not_for_me"):
            with pg().cursor() as c:
                c.execute("INSERT INTO dismissed_places (place_name) VALUES (%s)", (hero_name,))
            st.rerun()

    with st.container(border=True, key="invite_card"):
        section_label("Send this to a friend")
        if "draft" not in st.session_state:
            st.session_state.draft = ""

        if st.button("Write the invite", type="primary", key="write_invite"):
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

        if st.session_state.draft:
            st.code(st.session_state.draft, language=None)


def render_saved_places():
    saved = fetch_saved_places()
    if not saved:
        return
    section_label("Your list")
    for saved_id, place_name, address, neighborhood in saved:
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            col1.markdown(f"**{place_name}** — {address} ({neighborhood})")
            if col2.button("Remove", key=f"remove_{saved_id}"):
                with pg().cursor() as c:
                    c.execute("DELETE FROM saved_places WHERE id = %s", (saved_id,))
                st.rerun()


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

.stApp { background-color: #0E1116; }

.block-container {
    max-width: 1080px;
    margin: 0 auto;
    padding-top: 2rem;
    padding-left: 24px;
    padding-right: 24px;
    padding-bottom: 48px;
}

[data-testid="stVerticalBlock"] { gap: 12px !important; }

h1, h2, h3, h4, p, label, span, div { color: #F2F4F7; }

.page-title {
    font-size: 40px;
    font-weight: 700;
    letter-spacing: -0.03em;
    color: #F2F4F7;
    margin-bottom: 4px;
}
.page-subtitle {
    font-size: 16px;
    color: #8B95A5;
    margin-bottom: 12px;
}
.section-label {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 600;
    color: #8B95A5;
    margin-bottom: 8px;
}
.muted-text {
    font-size: 16px;
    color: #8B95A5;
}
.hero-name {
    font-size: 32px;
    font-weight: 700;
    color: #F2F4F7;
    line-height: 1.2;
    margin-bottom: 4px;
}
.footer-note {
    font-size: 13px;
    color: #8B95A5;
    margin-top: 24px;
}

/* cards */
.st-key-hero_card, .st-key-invite_card {
    background: #171B22 !important;
    border: 1px solid #242A33 !important;
    border-radius: 14px !important;
    padding: 28px !important;
    margin-bottom: 12px !important;
    box-shadow: none !important;
}
.st-key-hero_card {
    border-left: 2px solid #FF6B4A !important;
}

.empty-state {
    text-align: center;
    color: #8B95A5;
    padding: 48px 24px;
    border: 1px dashed #242A33;
    border-radius: 14px;
    background: #171B22;
}

/* pills (neighborhood chips) */
.st-key-neighborhood_pills button {
    border-radius: 8px !important;
    padding: 8px 16px !important;
    font-size: 14px !important;
    white-space: nowrap !important;
    border: 1px solid #242A33 !important;
    background-color: #171B22 !important;
    color: #F2F4F7 !important;
}
.st-key-neighborhood_pills button[aria-pressed="true"],
.st-key-neighborhood_pills button[aria-checked="true"],
.st-key-neighborhood_pills button[aria-selected="true"] {
    background-color: #FF6B4A !important;
    color: #0E1116 !important;
    border: none !important;
}

/* buttons */
.stButton>button {
    border-radius: 8px;
}
.stButton>button[kind="primary"] {
    background-color: #FF6B4A;
    color: #0E1116;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: 500;
    border: none;
}
.stButton>button[kind="secondary"] {
    background-color: #171B22;
    border: 1px solid #242A33;
    color: #F2F4F7;
}
</style>
""", unsafe_allow_html=True)

st.markdown("<div class='page-title'>SF Gathering Places</div>", unsafe_allow_html=True)
st.markdown("<div class='page-subtitle'>Find one place near you, and someone to go with.</div>", unsafe_allow_html=True)

if "neighborhood" not in st.session_state:
    st.session_state.neighborhood = None

selected_chip_label = st.pills(
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
    empty_state("Pick a neighborhood above to get started.")
    st.stop()

try:
    render_neighborhood(neighborhood)
    render_saved_places()
except Exception:
    st.error("We're having trouble reaching our data right now. Please try again in a moment.")

total_count, total_ms = total_business_count()
st.markdown(
    f"<div class='footer-note'>Chosen from {total_count:,} San Francisco business records in {total_ms} ms.</div>",
    unsafe_allow_html=True,
)
