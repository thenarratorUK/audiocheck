import csv
import io
import time
import tempfile
import hashlib
from pathlib import Path

import streamlit as st

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

def _write_uploaded_to_temp(uploaded) -> tuple[str, str]:
    """Return (audio_name, audio_path). Uses a hash so reruns don't rewrite the same file."""
    audio_name = uploaded.name
    suffix = Path(audio_name).suffix or ".mp3"

    data = uploaded.getvalue()
    digest = hashlib.sha1(data).hexdigest()[:16]
    out_path = Path(tempfile.gettempdir()) / f"proofing_audio_{digest}{suffix}"

    if not out_path.exists():
        out_path.write_bytes(data)

    return audio_name, str(out_path)

def _events_to_csv_bytes(events: list[dict]) -> bytes:
    buf = io.StringIO()
    fieldnames = ["audio_file", "time_sec", "timecode", "label", "note", "logged_at_epoch"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in events:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")

st.title("Proofing Logger (tap-to-mark)")

st.session_state.setdefault("events", [])
st.session_state.setdefault("audio_name", None)
st.session_state.setdefault("audio_path", None)
st.session_state.setdefault("last_time", 0.0)

st.caption("Note: first load on Streamlit Community Cloud can be slow if the app is waking up.")

uploaded = st.file_uploader(
    "Upload audio (MP3/WAV).",
    type=["mp3", "wav", "m4a", "ogg"],
    accept_multiple_files=False,
)

if uploaded is not None:
    with st.spinner("Preparing audio…"):
        audio_name, audio_path = _write_uploaded_to_temp(uploaded)
        st.session_state["audio_name"] = audio_name
        st.session_state["audio_path"] = audio_path

audio_path = st.session_state.get("audio_path")
audio_name = st.session_state.get("audio_name")

if audio_path:
    st.subheader("Player")

    # Lazy import so the page can render quickly before heavier JS/component initialisation.
    with st.spinner("Loading player…"):
        from streamlit_advanced_audio import audix
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
        if cols[i].button(label, width="stretch"):
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
events = st.session_state["events"]

if events:
    st.dataframe(events, width="stretch", hide_index=True)
else:
    st.info("No issues logged yet.")

c1, c2, c3 = st.columns(3)

with c1:
    if st.button("Undo last", width="stretch") and st.session_state["events"]:
        st.session_state["events"].pop()

with c2:
    if st.button("Clear all", width="stretch"):
        st.session_state["events"] = []

with c3:
    if events:
        st.download_button(
            "Download CSV",
            data=_events_to_csv_bytes(events),
            file_name="proofing_log.csv",
            mime="text/csv",
            width="stretch",
        )
