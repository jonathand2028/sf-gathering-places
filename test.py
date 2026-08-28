import streamlit as st, clickhouse_connect, certifi, os
from dotenv import load_dotenv
load_dotenv()

c = clickhouse_connect.get_client(
    host=os.getenv('CH_HOST'), port=8443, username='default',
    password=os.getenv('CH_PASSWORD'), secure=True, ca_cert=certifi.where()
)
st.write(c.query('SELECT version()').result_rows)