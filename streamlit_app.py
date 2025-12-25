import io
import time
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_advanced_audio import audix

st.set_page_config(page_title="Bed Proofing Logger", layout="wide")

LABELS = ["Breath", "Pop", "Noise", "Click", "Plosive", "Mouth", "Other"]

def _fmt_time(seconds: float) -> str:
    if seconds is None:
        return ""
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60.0)
    h, m = divmod(m, 60.0)
    if h >= 1:
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    return f"{int(m):02d}:{s:06.3f}"

st.title("Proofing Logger (tap-to-mark)")

st.session_state.setdefault("events", [])
st.session_state.setdefault("audio_name", None)
st.session_state.setdefault("audio_path", None)
st.session_state.setdefault("last_time", 0.0)

uploaded = st.file_uploader(
    "Upload audio (MP3/WAV).",
    type=["mp3", "wav", "m4a", "ogg"],
    accept_multiple_files=False,
)

if uploaded is not None:
    # Save to a temp file so audix can read it as a path.
    suffix = Path(uploaded.name).suffix or ".mp3"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded.getbuffer())
    tmp.close()

    st.session_state["audio_name"] = uploaded.name
    st.session_state["audio_path"] = tmp.name

audio_path = st.session_state.get("audio_path")
audio_name = st.session_state.get("audio_name")

if audio_path:
    st.subheader("Player")

    # Call audix before buttons so we capture the latest currentTime
    # on the same rerun that handles a button press.
    result = audix(audio_path)

    if isinstance(result, dict) and "currentTime" in result:
        st.session_state["last_time"] = float(result["currentTime"])

    st.caption(
        f"Last reported time: {_fmt_time(st.session_state['last_time'])} "
        f"({st.session_state['last_time']:.3f}s)"
    )

    st.subheader("Log an issue")
    note = st.text_input("Optional note", value="", placeholder="e.g., ‘hard T’, ‘long pause’, ‘rustle’")

    cols = st.columns(len(LABELS))
    clicked_label = None
    for i, label in enumerate(LABELS):
        if cols[i].button(label, use_container_width=True):
            clicked_label = label

    if clicked_label:
        st.session_state["events"].append(
            {
                "audio_file": audio_name,
                "time_sec": float(st.session_state["last_time"]),
                "timecode": _fmt_time(st.session_state["last_time"]),
                "label": clicked_label,
                "note": note.strip(),
                "logged_at_epoch": time.time(),
            }
        )

st.divider()

st.subheader("Logged issues")
df = pd.DataFrame(st.session_state["events"])
st.dataframe(df, use_container_width=True, hide_index=True)

c1, c2 = st.columns(2)
with c1:
    if st.button("Undo last") and st.session_state["events"]:
        st.session_state["events"].pop()

with c2:
    if st.button("Clear all"):
        st.session_state["events"] = []

if not df.empty:
    out = io.StringIO()
    df.to_csv(out, index=False)
    st.download_button(
        "Download CSV",
        data=out.getvalue().encode("utf-8"),
        file_name="proofing_log.csv",
        mime="text/csv",
    )
