import csv
import io
import json
import time
import hashlib
import wave
from pathlib import Path

import streamlit as st
from mutagen import File as MutagenFile

st.set_page_config(page_title="Bed Proofing Logger", layout="wide")

LABELS = ["Breath", "Pop", "Noise", "Click", "Plosive", "Mouth", "Other"]

DATA_ROOT = Path("data")  # best-effort persistence on Streamlit Cloud while container stays alive

# -----------------------------
# Formatting
# -----------------------------
def _fmt_time_hh(seconds: float, decimals: int = 2) -> str:
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

def _user_dir(user_key: str) -> Path:
    return DATA_ROOT / user_key

def _state_path(user_key: str) -> Path:
    return _user_dir(user_key) / "state.json"

def _audio_dir(user_key: str) -> Path:
    return _user_dir(user_key) / "audio"

def _default_state() -> dict:
    return {
        "events": [],
        "last_time_by_audio": {},
        "duration_by_audio": {},
        "audio_files": {},            # audio_id -> {"name":..., "path":...}
        "last_audio_id": None,
        "pending_start_by_audio": {}, # audio_id -> float (apply once on next render)
    }

def _load_state(user_key: str) -> dict:
    p = _state_path(user_key)
    if not p.exists():
        return _default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Backwards compatibility with older state formats.
        for k, v in _default_state().items():
            data.setdefault(k, v)
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
            "last_time_by_audio": st.session_state["last_time_by_audio"],
            "duration_by_audio": st.session_state["duration_by_audio"],
            "audio_files": st.session_state["audio_files"],
            "last_audio_id": st.session_state.get("last_audio_id"),
            "player_start_by_audio": st.session_state["player_start_by_audio"],
            "player_nonce_by_audio": st.session_state["player_nonce_by_audio"],
        },
    )

# -----------------------------
# Audio utilities
# -----------------------------
def _hash_upload(name: str, data: bytes) -> str:
    return hashlib.sha1(name.encode("utf-8") + b"\0" + data).hexdigest()[:16]

def _store_uploaded_audio(user_key: str, uploaded) -> tuple[str, str, str]:
    """Return (audio_name, audio_path, audio_id). Stores under data/<key>/audio/ so refresh can reuse."""
    audio_name = uploaded.name
    data = uploaded.getvalue()
    audio_id = _hash_upload(audio_name, data)

    ext = (Path(audio_name).suffix or ".mp3").lower()
    out_dir = _audio_dir(user_key)
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"{audio_id}{ext}"
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

# Load persisted state into session_state
state = _load_state(user_key)

st.session_state.setdefault("events", state.get("events", []))
st.session_state.setdefault("last_time_by_audio", state.get("last_time_by_audio", {}))
st.session_state.setdefault("duration_by_audio", state.get("duration_by_audio", {}))
st.session_state.setdefault("audio_files", state.get("audio_files", {}))
st.session_state.setdefault("player_start_by_audio", state.get("player_start_by_audio", {}))
st.session_state.setdefault("player_nonce_by_audio", state.get("player_nonce_by_audio", {}))
# Back-compat: if an older state file had a one-shot pending jump, convert it to a start+nonce bump.
pending_legacy = state.get("pending_start_by_audio", {})
if isinstance(pending_legacy, dict) and pending_legacy:
    for _aid, _t in pending_legacy.items():
        try:
            st.session_state["player_start_by_audio"][_aid] = float(_t)
            st.session_state["player_nonce_by_audio"][_aid] = int(st.session_state["player_nonce_by_audio"].get(_aid, 0)) + 1
        except Exception:
            pass

st.session_state.setdefault("last_audio_id", state.get("last_audio_id"))

# -----------------------------
# Choose / upload audio
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
    with st.spinner("Storing audio for refresh-safe playback…"):
        audio_name, audio_path, audio_id = _store_uploaded_audio(user_key, uploaded)

    st.session_state["audio_files"][audio_id] = {"name": audio_name, "path": audio_path}
    st.session_state["last_audio_id"] = audio_id

    if audio_id not in st.session_state["duration_by_audio"]:
        with st.spinner("Reading duration…"):
            dur = _get_duration_seconds(audio_path)
        if dur is not None:
            st.session_state["duration_by_audio"][audio_id] = float(dur)

    _persist_now(user_key)

# If no upload on this run, allow selecting a previously stored file for this key
if audio_id is None and st.session_state["audio_files"]:
    ids = list(st.session_state["audio_files"].keys())
    last_id = st.session_state.get("last_audio_id")
    default_idx = ids.index(last_id) if (last_id in ids) else 0

    options = [(aid, st.session_state["audio_files"][aid]["name"]) for aid in ids]
    labels = [name for _, name in options]
    picked_name = st.selectbox("Previously stored for this key", labels, index=default_idx)

    picked_id = options[labels.index(picked_name)][0]
    audio_id = picked_id
    audio_name = st.session_state["audio_files"][picked_id]["name"]
    audio_path = st.session_state["audio_files"][picked_id]["path"]
    st.session_state["last_audio_id"] = picked_id
    _persist_now(user_key)

# -----------------------------
# Player + logging
# -----------------------------
if audio_id and audio_path:
    last_time = float(st.session_state["last_time_by_audio"].get(audio_id, 0.0))
    duration = st.session_state["duration_by_audio"].get(audio_id)

    st.subheader("Player")

    # Jump controls: change a per-audio mount nonce so the player remounts at a new start_time.
    nonce = int(st.session_state["player_nonce_by_audio"].get(audio_id, 0))
    start_at = float(st.session_state["player_start_by_audio"].get(audio_id, 0.0))

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Jump to last played time", width="stretch"):
            start_at = float(last_time)
            nonce += 1
            st.session_state["player_start_by_audio"][audio_id] = start_at
            st.session_state["player_nonce_by_audio"][audio_id] = nonce
            _persist_now(user_key)

    with c2:
        if st.button("Jump to 00:00:00.00", width="stretch"):
            start_at = 0.0
            nonce += 1
            st.session_state["player_start_by_audio"][audio_id] = start_at
            st.session_state["player_nonce_by_audio"][audio_id] = nonce
            _persist_now(user_key)

    with c3:
        if duration is not None:
            st.caption(f"Last played: {_fmt_time_hh(last_time)} / {_fmt_time_hh(duration)}")
        else:
            st.caption(f"Last played: {_fmt_time_hh(last_time)}")

    from streamlit_advanced_audio import audix

    player_key = f"audix_{audio_id}_{nonce}"
    result = audix(audio_path, start_time=start_at, key=player_key)

    # If the component reports time, capture it (typically updates on pause/seek/user interaction).
    if isinstance(result, dict) and "currentTime" in result:
        last_time = float(result["currentTime"])
        st.session_state["last_time_by_audio"][audio_id] = last_time
        _persist_now(user_key)

    # Friendly time readout under the player
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
        _persist_now(user_key)

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
        _persist_now(user_key)

with c2:
    if st.button("Clear all", width="stretch"):
        st.session_state["events"] = []
        _persist_now(user_key)

with c3:
    if st.button("Force save", width="stretch"):
        _persist_now(user_key)
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

st.caption("Tip: bookmark this page URL — it includes your user key, so refresh will restore your log and your uploaded audio.")
