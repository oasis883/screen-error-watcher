import streamlit as st

st.set_page_config(page_title="Screen Error Watcher — Demo", page_icon="🔍")

st.title("🔍 Screen Error Watcher")
st.markdown(
    "A lightweight Windows tool with an always-on-top overlay that watches "
    "your screen and shows an **AI-suggested fix** the moment an error appears — "
    "powered by the Claude vision API."
)

st.header("Live demo recording")
st.video("demo.mp4")

st.header("How it works")
st.image("flow.png")

st.markdown("---")
st.markdown(
    "**Source code:** [github.com/oasis883/screen-error-watcher]"
    "(https://github.com/oasis883/screen-error-watcher)"
)