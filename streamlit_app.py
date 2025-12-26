import csv
import io
import json
import time
import hashlib
import wave
import tempfile
from pathlib import Path

import streamlit as st
from mutagen import File as MutagenFile

st.set_page_config(page_title="Bed Proofing Logger", layout="wide")

LABELS = ["Breath", "Pop", "Noise", "Click", "Plosive", "Mouth", "Other"]

DATA_ROOT = Path("data")  # best-effort persistence while the Streamlit Cloud container stays alive

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

def _default_state() -> dict:
    return {
        "events": [],
        "last_time_by_audio": {},
        "duration_by_audio": {},
        "audio_files": {},             # audio_id -> {"name":..., "path":...} (path in /tmp)
        "last_audio_id": None,
        "pending_start_by_audio": {},  # audio_id -> float (apply once on next render)
        "mount_version_by_audio": {},  # audio_id -> int (forces component remount on jump)
    }

def _load_state(user_key: str) -> dict:
    p = _state_path(user_key)
    if not p.exists():
        return _default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
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
            "pending_start_by_audio": st.session_state["pending_start_by_audio"],
            "mount_version_by_audio": st.session_state["mount_version_by_audio"],
        },
    )

def _hash_upload(name: str, data: bytes) -> str:
    return hashlib.sha1(name.encode("utf-8") + b"\0" + data).hexdigest()[:16]

def _store_uploaded_audio(uploaded) -> tuple[str, str, str]:
    audio_name = uploaded.name
    data = uploaded.getvalue()
    audio_id = _hash_upload(audio_name, data)

    ext = (Path(audio_name).suffix or ".mp3").lower()
    out_dir = Path(tempfile.gettempdir()) / "proofing_logger_audio"
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

state = _load_state(user_key)

st.session_state.setdefault("events", state["events"])
st.session_state.setdefault("last_time_by_audio", state["last_time_by_audio"])
st.session_state.setdefault("duration_by_audio", state["duration_by_audio"])
st.session_state.setdefault("audio_files", state["audio_files"])
st.session_state.setdefault("pending_start_by_audio", state["pending_start_by_audio"])
st.session_state.setdefault("mount_version_by_audio", state["mount_version_by_audio"])
st.session_state.setdefault("last_audio_id", state["last_audio_id"])

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
        audio_name, audio_path, audio_id = _store_uploaded_audio(uploaded)

    st.session_state["audio_files"][audio_id] = {"name": audio_name, "path": audio_path}
    st.session_state["last_audio_id"] = audio_id

    if audio_id not in st.session_state["duration_by_audio"]:
        with st.spinner("Reading duration…"):
            dur = _get_duration_seconds(audio_path)
        if dur is not None:
            st.session_state["duration_by_audio"][audio_id] = float(dur)

    st.session_state["mount_version_by_audio"].setdefault(audio_id, 0)
    _persist_now(user_key)

if audio_id is None and st.session_state["audio_files"]:
    ids = list(st.session_state["audio_files"].keys())
    last_id = st.session_state.get("last_audio_id")
    default_idx = ids.index(last_id) if (last_id in ids) else 0

    options = [(aid, st.session_state["audio_files"][aid]["name"]) for aid in ids]
    labels = [name for _, name in options]
    picked_name = st.selectbox("Previously stored for this key", labels, index=default_idx)

    picked_id = options[labels.index(picked_name)][0]
    picked = st.session_state["audio_files"][picked_id]
    if not Path(picked["path"]).exists():
        st.warning("That audio file is no longer available on the server. Please re-upload it.")
    else:
        audio_id = picked_id
        audio_name = picked["name"]
        audio_path = picked["path"]
        st.session_state["last_audio_id"] = picked_id
        st.session_state["mount_version_by_audio"].setdefault(audio_id, 0)
        _persist_now(user_key)

if audio_id and audio_path:
    last_time = float(st.session_state["last_time_by_audio"].get(audio_id, 0.0))
    duration = st.session_state["duration_by_audio"].get(audio_id)

    st.subheader("Player")

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Jump to last played time", width="stretch"):
            st.session_state["pending_start_by_audio"][audio_id] = float(int(last_time))
            st.session_state["mount_version_by_audio"][audio_id] = int(st.session_state["mount_version_by_audio"].get(audio_id, 0)) + 1
            _persist_now(user_key)

    with c2:
        if st.button("Jump to 00:00:00.00", width="stretch"):
            st.session_state["pending_start_by_audio"][audio_id] = 0.0
            st.session_state["mount_version_by_audio"][audio_id] = int(st.session_state["mount_version_by_audio"].get(audio_id, 0)) + 1
            _persist_now(user_key)

    with c3:
        if duration is not None:
            st.caption(f"Last played: {_fmt_time_hh(last_time)} / {_fmt_time_hh(duration)}")
        else:
            st.caption(f"Last played: {_fmt_time_hh(last_time)}")

    pending = st.session_state["pending_start_by_audio"].get(audio_id)
    mount_v = int(st.session_state["mount_version_by_audio"].get(audio_id, 0))
    player_key = f"audix_{audio_id}_{mount_v}"

    from streamlit_advanced_audio import audix

    if pending is None:
        result = audix(audio_path, key=player_key)
    else:
        result = audix(audio_path, start_time=pending, key=player_key)
        st.session_state["pending_start_by_audio"].pop(audio_id, None)
        _persist_now(user_key)

    if isinstance(result, dict) and "currentTime" in result:
        last_time = float(result["currentTime"])
        st.session_state["last_time_by_audio"][audio_id] = last_time
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
