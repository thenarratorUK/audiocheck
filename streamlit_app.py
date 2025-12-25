import csv
import io
import json
import time
import tempfile
import hashlib
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Bed Proofing Logger", layout="wide")

LABELS = ["Breath", "Pop", "Noise", "Click", "Plosive", "Mouth", "Other"]

DATA_ROOT = Path("data")  # persisted while the Streamlit Cloud container stays alive

def _fmt_time(seconds: float) -> str:
    if seconds is None:
        return ""
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60.0)
    h, m = divmod(m, 60.0)
    if h >= 1:
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    return f"{int(m):02d}:{s:06.3f}"

def _events_to_csv_bytes(events: list[dict]) -> bytes:
    buf = io.StringIO()
    fieldnames = ["audio_file", "time_sec", "timecode", "label", "note", "logged_at_epoch"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in events:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")

def _safe_key(s: str) -> str:
    s = (s or "").strip()
    keep = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
    return "".join(keep)[:64]

def _state_path(user_key: str) -> Path:
    return DATA_ROOT / user_key / "state.json"

def _load_state(user_key: str) -> dict:
    p = _state_path(user_key)
    if not p.exists():
        return {"events": [], "last_time_by_audio": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"events": [], "last_time_by_audio": {}}

def _save_state(user_key: str, state: dict) -> None:
    p = _state_path(user_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

def _write_uploaded_to_temp(uploaded) -> tuple[str, str, str]:
    """Return (audio_name, audio_path, audio_id). audio_id is sha1(name+bytes)."""
    audio_name = uploaded.name
    suffix = Path(audio_name).suffix or ".mp3"

    data = uploaded.getvalue()
    digest = hashlib.sha1(audio_name.encode("utf-8") + b"\0" + data).hexdigest()[:16]
    out_path = Path(tempfile.gettempdir()) / f"proofing_audio_{digest}{suffix}"

    if not out_path.exists():
        out_path.write_bytes(data)

    return audio_name, str(out_path), digest

# -----------------------------
# User key "page"
# -----------------------------
st.title("Proofing Logger (tap-to-mark)")

# Persist the key in the URL so refresh keeps it.
q = st.query_params
existing_key = q.get("k", "")

if not existing_key:
    st.info("Enter a user key to persist your log across refreshes (stored server-side).")
    entered = st.text_input("User key", value="", placeholder="e.g., DW-25Dec", max_chars=64)
    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button("Continue", width="stretch"):
            k = _safe_key(entered)
            if not k:
                st.error("User key must contain at least one letter/number.")
            else:
                st.query_params["k"] = k
                st.rerun()
    st.stop()

user_key = _safe_key(existing_key)
st.caption(f"User key: {user_key}")

# Load persisted state
state = _load_state(user_key)
st.session_state.setdefault("events", state.get("events", []))
st.session_state.setdefault("last_time_by_audio", state.get("last_time_by_audio", {}))

# -----------------------------
# Main UI
# -----------------------------
uploaded = st.file_uploader(
    "Upload audio (MP3/WAV).",
    type=["mp3", "wav", "m4a", "ogg"],
    accept_multiple_files=False,
)

audio_path = None
audio_name = None
audio_id = None

if uploaded is not None:
    with st.spinner("Preparing audio…"):
        audio_name, audio_path, audio_id = _write_uploaded_to_temp(uploaded)
    st.session_state["current_audio_name"] = audio_name
    st.session_state["current_audio_path"] = audio_path
    st.session_state["current_audio_id"] = audio_id
else:
    audio_name = st.session_state.get("current_audio_name")
    audio_path = st.session_state.get("current_audio_path")
    audio_id = st.session_state.get("current_audio_id")

last_time = 0.0
if audio_id:
    last_time = float(st.session_state["last_time_by_audio"].get(audio_id, 0.0))

if audio_path:
    st.subheader("Player")

    # Lazy import so first paint is fast.
    from streamlit_advanced_audio import audix

    result = audix(audio_path)
    if isinstance(result, dict) and "currentTime" in result:
        # This typically updates reliably on pause / user interaction.
        last_time = float(result["currentTime"])
        st.session_state["last_time_by_audio"][audio_id] = last_time

        # Persist on time update (cheap and makes refresh safer).
        state = {
            "events": st.session_state["events"],
            "last_time_by_audio": st.session_state["last_time_by_audio"],
        }
        _save_state(user_key, state)

    st.caption(f"Last reported time: {_fmt_time(last_time)} ({last_time:.3f}s)")

    st.subheader("Log an issue")
    note = st.text_input("Optional note", value="", placeholder="e.g., hard T / rustle / long pause")

    cols = st.columns(len(LABELS))
    clicked_label = None
    for i, label in enumerate(LABELS):
        if cols[i].button(label, width="stretch"):
            clicked_label = label

    if clicked_label:
        st.session_state["events"].append(
            {
                "audio_file": audio_name,
                "time_sec": float(last_time),
                "timecode": _fmt_time(last_time),
                "label": clicked_label,
                "note": note.strip(),
                "logged_at_epoch": time.time(),
            }
        )

        # Persist immediately after logging.
        state = {
            "events": st.session_state["events"],
            "last_time_by_audio": st.session_state["last_time_by_audio"],
        }
        _save_state(user_key, state)

st.divider()

st.subheader("Logged issues")
events = st.session_state["events"]

if events:
    st.dataframe(events, width="stretch", hide_index=True)
else:
    st.info("No issues logged yet.")

c1, c2, c3, c4 = st.columns(4)

with c1:
    if st.button("Undo last", width="stretch") and st.session_state["events"]:
        st.session_state["events"].pop()
        _save_state(user_key, {"events": st.session_state["events"], "last_time_by_audio": st.session_state["last_time_by_audio"]})

with c2:
    if st.button("Clear all", width="stretch"):
        st.session_state["events"] = []
        _save_state(user_key, {"events": st.session_state["events"], "last_time_by_audio": st.session_state["last_time_by_audio"]})

with c3:
    if st.button("Force save", width="stretch"):
        _save_state(user_key, {"events": st.session_state["events"], "last_time_by_audio": st.session_state["last_time_by_audio"]})
        st.success("Saved.")

with c4:
    if events:
        st.download_button(
            "Download CSV",
            data=_events_to_csv_bytes(events),
            file_name="proofing_log.csv",
            mime="text/csv",
            width="stretch",
        )

st.caption("Tip: bookmark this page URL — it includes your user key, so refresh will restore your log.")
