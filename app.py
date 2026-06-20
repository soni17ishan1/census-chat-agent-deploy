import os
import time

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from agent import guardrails
from agent.agent_loop import run_agent_turn

st.set_page_config(page_title="US Census Chat Agent", page_icon="📊")


def _get_secret(key: str) -> str | None:
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key)


def require_login():
    app_password = _get_secret("APP_PASSWORD")
    if not app_password:
        return  # no password configured -> open access
    if st.session_state.get("authenticated"):
        return
    st.title("📊 US Census Chat Agent")
    pwd = st.text_input("Password", type="password")
    if st.button("Enter") or pwd:
        if pwd == app_password:
            st.session_state.authenticated = True
            st.rerun()
        elif pwd:
            st.error("Incorrect password.")
    st.stop()


require_login()

st.title("📊 US Census Chat Agent")
st.caption(
    "Ask about US population, age, race, income, housing, and more, from the "
    "2019-2020 ACS Census Block Group dataset. State and county level only."
)

if "anthropic_messages" not in st.session_state:
    st.session_state.anthropic_messages = []
if "display_messages" not in st.session_state:
    st.session_state.display_messages = []

for msg in st.session_state.display_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("Ask a question about US Census data...")

if user_input:
    st.session_state.display_messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Checking your question..._")
        start = time.monotonic()
        try:
            verdict = guardrails.classify(user_input, st.session_state.display_messages[:-1])
        except Exception:
            verdict = {"verdict": "on_topic", "reason": "guardrail call failed, failing open"}

        if verdict["verdict"] in guardrails.REFUSAL_MESSAGES:
            answer = guardrails.REFUSAL_MESSAGES[verdict["verdict"]]
            placeholder.markdown(answer)
        else:
            placeholder.markdown("_Looking up Census data..._")
            try:
                st.session_state.anthropic_messages.append(
                    {"role": "user", "content": user_input}
                )
                answer = run_agent_turn(st.session_state.anthropic_messages)
            except Exception as e:
                answer = (
                    "I'm having trouble connecting to the Census data right now "
                    f"({type(e).__name__}). Please try again in a moment, and let "
                    "the maintainer know if this keeps happening."
                )
            elapsed = time.monotonic() - start
            if elapsed > 55:
                answer += "\n\n_(That took longer than expected -- try a narrower question.)_"
            placeholder.markdown(answer)

    st.session_state.display_messages.append({"role": "assistant", "content": answer})
