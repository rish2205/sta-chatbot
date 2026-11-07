"""Streamlit UI for the STA chatbot: a floating chat widget.

Flow: round launcher button (bottom-right of the page) -> welcome screen -> ask the
shareholder's name -> chat with topic buttons, follow-up question buttons and a text box.

Run:  streamlit run app.py
"""
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

# Load the key from Streamlit secrets (preferred) or the environment.
try:
    if "OPENAI_API_KEY" in st.secrets:
        os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
    for _k in ("SCOPE_MODEL", "ANSWER_MODEL"):
        if _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass

import sta_bot  # noqa: E402  (imported after env is set)

BOT_NAME = "Nia"  # placeholder assistant name; change to whatever you want
TAGLINE = "Your shareholder e-assistant"
FOOTER = "Nia provides general guidance only. For details about your holdings or KYC status, contact GNSA's STA team."

TOPICS = sta_bot.load_topics()
TOPIC_BY_ID = {t["id"]: t for t in TOPICS}
TOPIC_CHIPS = [{"label": t["label"], "kind": "topic", "id": t["id"]} for t in TOPICS]
WELCOME_LIST = [t["label"] for t in TOPICS if t["id"] != "isin"]

st.set_page_config(page_title="STA Assistant", page_icon="💬", layout="wide")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap');
html, body, [class*="st-"], .stApp { font-family: 'Manrope', sans-serif; }
#MainMenu, header[data-testid="stHeader"], footer { visibility: hidden; height: 0; }
.stApp {
  background:
    radial-gradient(900px 600px at 0% 0%, #fde8e4 0%, transparent 60%),
    radial-gradient(900px 600px at 100% 100%, #e6ebf8 0%, transparent 60%),
    #fbf8f4;
}
.block-container { padding-top: 2rem; }

/* ---------- launcher ---------- */
.st-key-launcher { position: fixed; right: 24px; bottom: 24px; width: auto !important; z-index: 1000000; }
.st-key-launcher button {
  width: 62px; height: 62px; border-radius: 50%; padding: 0;
  background: #d11f26; border: none; color: #fff;
  box-shadow: 0 8px 24px rgba(209,31,38,.45);
}
.st-key-launcher button:hover { background: #b8191f; color: #fff; }
.st-key-launcher button p { font-size: 26px; line-height: 1; color: #fff; }

/* ---------- panel ---------- */
.st-key-chatpanel {
  position: fixed; right: 24px; bottom: 100px; z-index: 999990;
  width: 410px; max-width: calc(100vw - 32px);
  height: min(660px, calc(100vh - 124px));
  background: #fff; border-radius: 24px; overflow: hidden;
  box-shadow: 0 24px 70px rgba(16,32,63,.28);
  display: flex; flex-direction: column; gap: 0 !important; padding: 0 !important;
}
.st-key-chatpanel > div { width: 100% !important; }
.sia-head {
  display: flex; align-items: center; gap: 14px; padding: 20px 22px;
  background: linear-gradient(135deg, #10203f 0%, #1b2f5e 55%, #7d2530 100%); color: #fff;
}
.sia-avatar {
  width: 56px; height: 56px; border-radius: 50%; background: #d11f26; color: #fff;
  display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 22px;
  border: 3px solid rgba(255,255,255,.25);
}
.sia-title { font-weight: 800; font-size: 22px; line-height: 1.1; }
.sia-sub { font-size: 13px; opacity: .85; display: flex; align-items: center; gap: 6px; margin-top: 3px; }
.sia-sub i { width: 9px; height: 9px; border-radius: 50%; background: #d9d9d9; display: inline-block; }
.st-key-btn_restart, .st-key-btn_close { position: absolute; top: 24px; z-index: 5; width: auto !important; }
.st-key-btn_restart { right: 64px; } .st-key-btn_close { right: 20px; }
.st-key-btn_restart button, .st-key-btn_close button {
  background: transparent; border: none; color: #fff; padding: 0 6px; min-height: 0; font-size: 20px;
}
.st-key-btn_restart button p, .st-key-btn_close button p { color: #fff; font-size: 20px; }

/* ---------- welcome screen ---------- */
.st-key-welcome_body { padding: 22px; gap: 14px; }
.sia-card { background: #f8f4ef; border-radius: 20px; padding: 24px; }
.sia-card h3 { margin: 0 0 10px; font-size: 22px; font-weight: 800; color: #0b1226; }
.sia-card p { margin: 0 0 12px; color: #667085; font-size: 15px; }
.sia-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.sia-grid span { font-size: 15px; color: #101828; font-weight: 500; }
.sia-grid span::before { content: "\\203A"; color: #d11f26; font-weight: 800; margin-right: 12px; }
.sia-label { font-weight: 600; font-size: 15px; color: #0b1226; margin: 6px 0 -6px; }
.st-key-nameform { border: none !important; padding: 0 !important; }
.st-key-nameform input {
  background: #faf8f5; border-radius: 16px; height: 54px; border: 1px solid #e6e1d9; padding: 0 18px;
}
.st-key-nameform button, .st-key-ask button { border: none; }
.st-key-nameform button {
  width: 100%; height: 56px; border-radius: 16px; background: #d11f26; color: #fff; font-weight: 700;
  margin-top: 6px;
}
.st-key-nameform button p { color: #fff; font-weight: 700; font-size: 17px; }
.sia-error { color: #b42318; font-size: 13px; margin-top: -4px; }

/* ---------- messages ---------- */
.st-key-msgs { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 18px 20px; gap: 6px; }
[class*="st-key-bot_"] {
  position: relative; margin-left: 46px; width: fit-content !important; max-width: calc(100% - 46px);
  background: #f8f4ef; border-radius: 20px; padding: 14px 18px; gap: 6px;
}
[class*="st-key-bot_"]::before {
  content: "__INITIAL__"; position: absolute; left: -46px; bottom: 0; width: 36px; height: 36px;
  border-radius: 50%; background: #d11f26; color: #fff; font-weight: 800; font-size: 14px;
  display: flex; align-items: center; justify-content: center;
}
[class*="st-key-bot_"] p, [class*="st-key-bot_"] li { font-size: 15px; line-height: 1.5; color: #101828; }
[class*="st-key-bot_"] ul, [class*="st-key-bot_"] ol { padding-left: 20px; margin: 4px 0; }
[class*="st-key-user_"] {
  margin-left: auto; width: fit-content !important; max-width: 82%;
  background: #10203f; border-radius: 20px; padding: 12px 20px;
}
[class*="st-key-user_"] p { color: #fff; font-size: 15px; margin: 0; }
.ts { font-size: 12px; color: #667085; margin: 2px 0 6px; }
.ts-bot { margin-left: 46px; } .ts-user { text-align: right; }
.formlink {
  display: inline-block; margin: 6px 6px 0 0; padding: 8px 14px; border-radius: 12px;
  background: #fff; border: 1px solid #d11f26; color: #a3141b !important; font-weight: 600;
  font-size: 13px; text-decoration: none;
}
.formlink:hover { background: #fdeceb; }
.formnote { font-size: 12px; color: #667085; margin-top: 6px; }
.srcs { font-size: 12px; color: #667085; margin-top: 8px; }
.srcs summary { cursor: pointer; }
.srcs a { color: #1b2f5e; }

/* ---------- chips ---------- */
[class*="st-key-chips_"] { margin-left: 46px; flex-direction: row !important; flex-wrap: wrap; gap: 8px; padding-bottom: 6px; }
[class*="st-key-chips_"] > div { width: auto !important; flex: 0 0 auto; }
[class*="st-key-chips_"] button {
  background: #fdeceb; border: 1px solid #f3b9b6; color: #a3141b; border-radius: 999px;
  padding: 4px 14px; min-height: 0; font-weight: 500;
}
[class*="st-key-chips_"] button p { color: #a3141b; font-size: 14px; }
[class*="st-key-chips_"] button:hover { background: #f9d6d3; border-color: #e99a95; }
.chip-static {
  display: inline-block; background: #fdeceb; border: 1px solid #f3b9b6; color: #a3141b;
  border-radius: 999px; padding: 5px 14px; font-size: 14px; font-weight: 500; opacity: .6;
}
.chips-static { margin: 0 0 6px 46px; display: flex; flex-wrap: wrap; gap: 8px; }

/* ---------- input ---------- */
.st-key-ask { padding: 10px 18px 0; border: none !important; border-top: 1px solid #eee !important; position: relative; }
.st-key-ask input { background: #faf8f5; border-radius: 18px; height: 54px; padding: 0 64px 0 18px; border: 1px solid #e6e1d9; }
.st-key-ask [data-testid="stFormSubmitButton"] { position: absolute; right: 28px; top: 20px; width: auto !important; }
.st-key-ask button {
  width: 36px; height: 36px; border-radius: 50%; background: #d11f26; color: #fff; padding: 0; min-height: 0;
}
.st-key-ask button p { color: #fff; font-size: 18px; line-height: 1; }
.sia-foot { font-size: 12px; color: #667085; text-align: center; padding: 6px 18px 12px; }

/* page placeholder */
.page-hero { max-width: 640px; margin-top: 12vh; }
.page-hero h1 { font-weight: 800; color: #10203f; }
.page-hero p { color: #667085; }
</style>
""".replace("__INITIAL__", BOT_NAME[0].upper())
st.markdown(CSS, unsafe_allow_html=True)

# ------------------------------------------------------------------ state
ss = st.session_state
ss.setdefault("open", False)
ss.setdefault("stage", "welcome")  # welcome | chat
ss.setdefault("name", "")
ss.setdefault("messages", [])
ss.setdefault("pending", None)
ss.setdefault("name_error", False)


def now() -> str:
    return datetime.now().strftime("%I:%M %p")


def add_msg(role: str, content: str, **extra) -> None:
    ss.messages.append({"role": role, "content": content, "ts": now(), **extra})


def clean_name(raw: str) -> str:
    return re.sub(r"[^\w .'-]", "", raw or "").strip()[:40]


def start_conversation() -> None:
    add_msg(
        "assistant",
        f"Welcome **{ss.name}** to **{BOT_NAME}**, your e-assistant. "
        "Get started by selecting a query below or typing your own.",
        chips=TOPIC_CHIPS,
    )


# ---------------------------------------------------------------- callbacks
def toggle_open() -> None:
    ss.open = not ss.open


def on_start() -> None:
    name = clean_name(ss.get("name_text"))
    if not name:
        ss.name_error = True
        return
    ss.name, ss.name_error, ss.stage = name, False, "chat"
    ss.messages = []
    start_conversation()


def on_restart() -> None:
    ss.messages, ss.pending = [], None
    start_conversation()


def on_question(text: str) -> None:
    add_msg("user", text)
    ss.pending = text


def on_send() -> None:
    text = (ss.get("ask_text") or "").strip()[:500]
    if text:
        on_question(text)


def on_chip(chip: dict) -> None:
    kind = chip["kind"]
    if kind == "question":
        on_question(chip["label"])
    elif kind == "menu":
        add_msg("user", chip["label"])
        add_msg("assistant", "What else can I help you with?", chips=TOPIC_CHIPS)
    else:  # topic
        topic = TOPIC_BY_ID[chip["id"]]
        add_msg("user", topic["label"])
        qs = [{"label": q["q"], "kind": "question"} for q in topic["questions"]]
        if topic.get("special") == "isin":
            # TODO: connect the company database here (search company -> return ISIN).
            add_msg(
                "assistant",
                "Looking up an ISIN by company name will be available soon. "
                "Meanwhile, I can explain how ISIN and CIN work.",
                chips=qs,
            )
        else:
            add_msg("assistant", f"Please select a **{topic['label']}** option", chips=qs)


# ------------------------------------------------------------------ render
def extras_html(msg: dict) -> str:
    parts = []
    for f in msg.get("forms", []):
        label = html.escape(f"{f['code']}: {f['name']}")
        if f.get("url"):
            parts.append(
                f'<a class="formlink" href="{html.escape(f["url"])}" target="_blank" '
                f'rel="noopener">&#11015; {label}</a>'
            )
        else:
            parts.append(f'<div class="formnote">{label}. {html.escape(f.get("note", ""))}</div>')
    if msg.get("sources"):
        items = "".join(
            f'<li><a href="{html.escape(s["url"])}" target="_blank" rel="noopener">'
            f'{html.escape(s["title"])}</a></li>'
            for s in msg["sources"]
        )
        parts.append(f'<details class="srcs"><summary>Sources</summary><ul>{items}</ul></details>')
    if msg.get("kind") == "answer":
        parts.append('<div class="formnote">Informational only. Check the latest SEBI circular '
                     "or your RTA before acting.</div>")
    return "".join(parts)


def render_message(i: int, msg: dict, is_last: bool) -> None:
    if msg["role"] == "user":
        with st.container(key=f"user_{i}"):
            st.markdown(html.escape(msg["content"]))
        st.markdown(f'<div class="ts ts-user">seen &middot; {msg["ts"]}</div>', unsafe_allow_html=True)
        return
    with st.container(key=f"bot_{i}"):
        st.markdown(msg["content"])
        extra = extras_html(msg)
        if extra:
            st.markdown(extra, unsafe_allow_html=True)
    st.markdown(f'<div class="ts ts-bot">{msg["ts"]}</div>', unsafe_allow_html=True)
    chips = msg.get("chips")
    if not chips:
        return
    if is_last and not ss.pending:
        with st.container(key=f"chips_{i}"):
            for j, chip in enumerate(chips):
                st.button(chip["label"], key=f"chip_{i}_{j}", on_click=on_chip, args=(chip,))
    else:
        pills = "".join(f'<span class="chip-static">{html.escape(c["label"])}</span>' for c in chips)
        st.markdown(f'<div class="chips-static">{pills}</div>', unsafe_allow_html=True)


def render_header() -> None:
    st.markdown(
        f'<div class="sia-head"><div class="sia-avatar">{html.escape(BOT_NAME[0].upper())}</div>'
        f'<div><div class="sia-title">{html.escape(BOT_NAME)}</div>'
        f'<div class="sia-sub"><i></i>{html.escape(TAGLINE)}</div></div></div>',
        unsafe_allow_html=True,
    )
    if ss.stage == "chat":
        st.button("↻", key="btn_restart", on_click=on_restart, help="Restart chat")
    st.button("✕", key="btn_close", on_click=toggle_open, help="Close")


def render_welcome() -> None:
    with st.container(key="welcome_body"):
        items = "".join(f"<span>{html.escape(x)}</span>" for x in WELCOME_LIST)
        st.markdown(
            f'<div class="sia-card"><h3>Hi there! I\'m {html.escape(BOT_NAME)} 👋</h3>'
            f"<p>I can help you with your queries regarding</p>"
            f'<div class="sia-grid">{items}</div></div>'
            '<div class="sia-label">Your name</div>',
            unsafe_allow_html=True,
        )
        with st.form("nameform", border=False):
            st.text_input("Your name", key="name_text", placeholder="Enter your name to start",
                          label_visibility="collapsed", max_chars=40)
            st.form_submit_button("Start chat", on_click=on_start)
        if ss.name_error:
            st.markdown('<div class="sia-error">Please enter your name to start.</div>',
                        unsafe_allow_html=True)


def render_chat() -> None:
    with st.container(key="msgs"):
        last = len(ss.messages) - 1
        for i, msg in enumerate(ss.messages):
            render_message(i, msg, i == last)
        if ss.pending:
            question, ss.pending = ss.pending, None
            history = [{"role": m["role"], "content": m["content"]} for m in ss.messages[:-1]]
            with st.spinner(f"{BOT_NAME} is checking official sources..."):
                reply = sta_bot.answer(question, history, user_name=ss.name)
            add_msg(
                "assistant", reply.text, kind=reply.kind, sources=reply.sources, forms=reply.forms,
                chips=[{"label": "Main menu", "kind": "menu"}],
            )
            st.rerun()
    with st.form("ask", clear_on_submit=True, border=False):
        st.text_input("Message", key="ask_text", placeholder="Type your query...",
                      label_visibility="collapsed", max_chars=500)
        st.form_submit_button("➤", on_click=on_send)


# -------------------------------------------------------------------- page
st.markdown(
    '<div class="page-hero"><h1>Shareholder services</h1>'
    "<p>Your website content goes here. The chat button is at the bottom right.</p></div>",
    unsafe_allow_html=True,
)

if ss.open:
    with st.container(key="chatpanel"):
        render_header()
        if ss.stage == "welcome":
            render_welcome()
        else:
            render_chat()
    if ss.stage == "chat":
        # Static script (no user content): keep the message list scrolled to the newest message.
        scroll_js = (
            "<script>const e=window.parent.document.querySelector('.st-key-msgs');"
            "if(e){e.scrollTop=e.scrollHeight;setTimeout(()=>{e.scrollTop=e.scrollHeight},200);}</script>"
        )
        if hasattr(st, "iframe"):  # newer Streamlit replaces components.v1.html
            st.iframe(scroll_js, height=1)
        else:
            components.html(scroll_js, height=0)

with st.container(key="launcher"):
    st.button("✕" if ss.open else "💬", key="launch", on_click=toggle_open)