import logging
import os
import time

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# The Snowflake connector logs verbosely at INFO (connection handshake
# details on every query) -- quiet it down so our own application logs
# (questions, generated SQL, latency, guardrail verdicts) aren't buried.
logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from agent import guardrails
from agent.agent_loop import run_agent_turn, trim_history

st.set_page_config(page_title="US Census Chat Agent", page_icon="📊")

# Cost/abuse protection: bounds how much one browser session can spend on
# Anthropic + Snowflake calls, independent of the Snowflake-side resource
# monitor (which caps total account spend but wouldn't stop a single
# session from burning through it quickly). Neither layer alone is
# sufficient -- this is the per-session throttle, the resource monitor is
# the account-wide hard stop.
MAX_MESSAGES_PER_SESSION = 30
MIN_SECONDS_BETWEEN_MESSAGES = 3
# A single huge pasted-in message would burn a disproportionate number of
# tokens (cost) in one shot; real census questions are short.
MAX_INPUT_LENGTH = 500


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
if "message_count" not in st.session_state:
    st.session_state.message_count = 0
if "last_message_time" not in st.session_state:
    st.session_state.last_message_time = 0.0

for msg in st.session_state.display_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if st.session_state.message_count >= MAX_MESSAGES_PER_SESSION:
    st.info(
        f"This session has reached its limit of {MAX_MESSAGES_PER_SESSION} questions "
        "(a safeguard against runaway usage costs). Please refresh the page to start "
        "a new session."
    )
    st.stop()

user_input = st.chat_input("Ask a question about US Census data...")

if user_input:
    if len(user_input) > MAX_INPUT_LENGTH:
        logger.warning("Rejected over-length input: %d chars", len(user_input))
        st.warning(
            f"That question is too long ({len(user_input)} characters, max "
            f"{MAX_INPUT_LENGTH}). Please shorten it."
        )
        st.stop()

    now = time.monotonic()
    if now - st.session_state.last_message_time < MIN_SECONDS_BETWEEN_MESSAGES:
        st.warning("Please wait a few seconds between questions.")
        st.stop()
    st.session_state.last_message_time = now
    st.session_state.message_count += 1

    logger.info(
        "Question received (session msg #%d): %s",
        st.session_state.message_count,
        user_input[:300],
    )

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
            logger.exception("Guardrail classification failed, failing open")
            verdict = {"verdict": "on_topic", "reason": "guardrail call failed, failing open"}

        logger.info("Guardrail verdict=%s reason=%s", verdict["verdict"], verdict.get("reason"))

        if verdict["verdict"] in guardrails.REFUSAL_MESSAGES:
            answer = guardrails.REFUSAL_MESSAGES[verdict["verdict"]]
            placeholder.markdown(answer)
        else:
            placeholder.markdown("_Looking up Census data..._")
            try:
                st.session_state.anthropic_messages.append(
                    {"role": "user", "content": user_input}
                )
                st.session_state.anthropic_messages, did_trim = trim_history(
                    st.session_state.anthropic_messages
                )
                if did_trim and not st.session_state.get("warned_about_trim"):
                    st.session_state.warned_about_trim = True
                    st.caption(
                        "ℹ️ This conversation has gotten long, so I've dropped the "
                        "earliest part of it to keep things fast -- ask again if you "
                        "need something from much earlier in the chat."
                    )
                answer = run_agent_turn(
                    st.session_state.anthropic_messages,
                    on_progress=lambda msg: placeholder.markdown(f"_{msg}_"),
                )
                logger.info("Answered successfully in %.1fs", time.monotonic() - start)
            except Exception as e:
                logger.exception("Agent turn failed after %.1fs", time.monotonic() - start)
                answer = (
                    "I'm having trouble connecting to the Census data right now "
                    f"({type(e).__name__}). Please try again in a moment, and let "
                    "the maintainer know if this keeps happening."
                )
            elapsed = time.monotonic() - start
            if elapsed > 55:
                logger.warning("Response exceeded 55s: %.1fs", elapsed)
                answer += "\n\n_(That took longer than expected -- try a narrower question.)_"
            placeholder.markdown(answer)

    st.session_state.display_messages.append({"role": "assistant", "content": answer})
