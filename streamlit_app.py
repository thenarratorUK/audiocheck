import csv
import io
import json
import time
import tempfile
import hashlib
import wave
from pathlib import Path

import streamlit as st
from mutagen import File as MutagenFile

st.set_page_config(page_title="Bed Proofing Logger", layout="wide")

LABELS = ["Breath", "Pop", "Noise", "Click", "Plosive", "Mouth", "Other"]

DATA_ROOT = Path("data")  # best-effort persistence while the Streamlit Cloud container stays alive

# -----------------------------
# Formatting
# -----------------------------
def _fmt_time_hh(seconds: float, decimals: int = 3) -> str:
    if seconds is None:
        seconds = 0.0
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60.0)
    h, m = divmod(m, 60.0)
    s_fmt = f"{s:0{2 + 1 + decimals}.{decimals}f}"
    return f"{int(h):02d}:{int(m):02d}:{s_fmt}"

def _events_to_csv_bytes(events: list[dict]) -> bytes:
    buf = io.StringIO()
    fieldnames = ["audio_file", "time_sec", "timecode", "label", "note", "logged_at_epoch"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in events:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")

# -----------------------------
# Persistence
# -----------------------------
def _safe_key(s: str) -> str:
    s = (s or "").strip()
    keep = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
    return "".join(keep)[:64]

def _state_path(user_key: str) -> Path:
    return DATA_ROOT / user_key / "state.json"

def _default_state() -> dict:
    return {
        "events": [],
        "last_played": {  # single "resume hint" for the user
            "audio_file": None,
            "time_sec": 0.0,
        },
    }

def _load_state(user_key: str) -> dict:
    p = _state_path(user_key)
    if not p.exists():
        return _default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for k, v in _default_state().items():
            data.setdefault(k, v)
        if isinstance(data.get("last_played"), dict):
            data["last_played"].setdefault("audio_file", None)
            data["last_played"].setdefault("time_sec", 0.0)
        else:
            data["last_played"] = {"audio_file": None, "time_sec": 0.0}
        return data
    except Exception:
        return _default_state()

def _save_state(user_key: str, state: dict) -> None:
    p = _state_path(user_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

def _persist_now(user_key: str) -> None:
    _save_state(
        user_key,
        {
            "events": st.session_state["events"],
            "last_played": st.session_state["last_played"],
        },
    )

# -----------------------------
# Audio utilities (for playback only)
# -----------------------------
def _write_uploaded_to_tmp(uploaded) -> tuple[str, str, str]:
    """Return (audio_name, audio_path, audio_id). Uses a hash so reruns don't rewrite the same file."""
    audio_name = uploaded.name
    suffix = Path(audio_name).suffix or ".mp3"

    data = uploaded.getvalue()
    audio_id = hashlib.sha1(audio_name.encode("utf-8") + b"\0" + data).hexdigest()[:16]
    out_path = Path(tempfile.gettempdir()) / f"proofing_audio_{audio_id}{suffix.lower()}"

    if not out_path.exists():
        out_path.write_bytes(data)

    return audio_name, str(out_path), audio_id

def _get_duration_seconds(audio_path: str) -> float | None:
    p = Path(audio_path)
    ext = p.suffix.lower()

    if ext == ".wav":
        try:
            with wave.open(str(p), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return float(frames) / float(rate)
        except Exception:
            return None

    try:
        mf = MutagenFile(str(p))
        if mf is not None and getattr(mf, "info", None) is not None:
            length = getattr(mf.info, "length", None)
            if length is not None:
                return float(length)
    except Exception:
        return None

    return None

# -----------------------------
# User key gate
# -----------------------------
st.title("Proofing Logger (tap-to-mark)")

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
st.session_state.setdefault("events", state["events"])
st.session_state.setdefault("last_played", state["last_played"])

# Resume hint (no automation; just a reminder)
lp = st.session_state["last_played"]
if lp.get("audio_file"):
    st.info(f"Last played time: {_fmt_time_hh(lp.get('time_sec', 0.0))} in {lp.get('audio_file')}")
else:
    st.info("Last played time: 00:00:00.000 (no file yet)")

# -----------------------------
# Upload and play
# -----------------------------
uploaded = st.file_uploader(
    "Upload audio (MP3/WAV).",
    type=["mp3", "wav", "m4a", "ogg"],
    accept_multiple_files=False,
)

audio_path = None
audio_name = None
duration = None
last_time = float(lp.get("time_sec", 0.0)) if lp else 0.0

if uploaded is not None:
    with st.spinner("Preparing audio…"):
        audio_name, audio_path, _ = _write_uploaded_to_tmp(uploaded)
    duration = _get_duration_seconds(audio_path)

if audio_path:
    st.subheader("Player")

    from streamlit_advanced_audio import audix

    result = audix(audio_path)

    # Update last_time from the component when available (usually on pause/seek/user interaction).
    if isinstance(result, dict) and "currentTime" in result:
        last_time = float(result["currentTime"])
        st.session_state["last_played"] = {"audio_file": audio_name, "time_sec": last_time}
        _persist_now(user_key)

    if duration is not None:
        st.caption(f"Current: {_fmt_time_hh(last_time)} / {_fmt_time_hh(float(duration))}")
    else:
        st.caption(f"Current: {_fmt_time_hh(last_time)}")

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
                "timecode": _fmt_time_hh(last_time),
                "label": clicked_label,
                "note": note.strip(),
                "logged_at_epoch": time.time(),
            }
        )
        st.session_state["last_played"] = {"audio_file": audio_name, "time_sec": float(last_time)}
        _persist_now(user_key)

st.divider()

# -----------------------------
# Logged issues
# -----------------------------
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
        _persist_now(user_key)

with c2:
    if st.button("Clear all", width="stretch"):
        st.session_state["events"] = []
        _persist_now(user_key)

with c3:
    if events:
        st.download_button(
            "Download CSV",
            data=_events_to_csv_bytes(events),
            file_name="proofing_log.csv",
            mime="text/csv",
            width="stretch",
        )

st.caption("Tip: bookmark this page URL — it includes your user key, so refresh will restore your log and last-played reminder.")
