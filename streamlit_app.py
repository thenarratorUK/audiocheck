import csv
import io
import json
import time
import tempfile
import hashlib
import wave
from pathlib import Path

import pandas as pd
import streamlit as st
from mutagen import File as MutagenFile

st.set_page_config(page_title="Bed Proofing Logger", layout="wide")

LABELS = ["Breath", "Pop", "Noise", "Click", "Plosive", "Mouth", "Other"]

DATA_ROOT = Path("data")  # best-effort persistence while the Streamlit Cloud container stays alive

# -----------------------------
# Formatting / parsing
# -----------------------------
def fmt_time_hh(seconds: float, decimals: int = 3) -> str:
    if seconds is None:
        seconds = 0.0
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60.0)
    h, m = divmod(m, 60.0)
    s_fmt = f"{s:0{2 + 1 + decimals}.{decimals}f}"
    return f"{int(h):02d}:{int(m):02d}:{s_fmt}"

def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "??:??:??.???"
    return fmt_time_hh(seconds)

def parse_timecode_to_seconds(tc: str) -> float | None:
    tc = (tc or "").strip()
    if not tc:
        return None

    parts = tc.split(":")
    try:
        if len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            return h * 3600.0 + m * 60.0 + s
        if len(parts) == 2:
            m = int(parts[0])
            s = float(parts[1])
            return m * 60.0 + s
    except ValueError:
        pass

    try:
        return float(tc)
    except ValueError:
        return None

def events_to_csv_bytes(events: list[dict]) -> bytes:
    buf = io.StringIO()
    fieldnames = ["audio_file", "time_sec", "timecode", "label", "note", "logged_at_epoch"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in events:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")

# -----------------------------
# Persistence (events + last played hint only)
# -----------------------------
def safe_key(s: str) -> str:
    s = (s or "").strip()
    keep = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
    return "".join(keep)[:64]

def state_path(user_key: str) -> Path:
    return DATA_ROOT / user_key / "state.json"

def default_state() -> dict:
    return {
        "events": [],
        "last_played": {
            "audio_file": None,
            "time_sec": 0.0,
        },
    }

def load_state(user_key: str) -> dict:
    p = state_path(user_key)
    if not p.exists():
        return default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for k, v in default_state().items():
            data.setdefault(k, v)
        if not isinstance(data.get("last_played"), dict):
            data["last_played"] = {"audio_file": None, "time_sec": 0.0}
        data["last_played"].setdefault("audio_file", None)
        data["last_played"].setdefault("time_sec", 0.0)
        if not isinstance(data.get("events"), list):
            data["events"] = []
        return data
    except Exception:
        return default_state()

def save_state(user_key: str, state: dict) -> None:
    p = state_path(user_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

def persist_now(user_key: str) -> None:
    save_state(
        user_key,
        {
            "events": st.session_state["events"],
            "last_played": st.session_state["last_played"],
        },
    )

# -----------------------------
# Audio utilities (session-only)
# -----------------------------
def _hash_upload(name: str, data: bytes) -> str:
    return hashlib.sha1(name.encode("utf-8") + b"\0" + data).hexdigest()[:16]

def write_uploaded_to_tmp(uploaded) -> tuple[str, str, str]:
    audio_name = uploaded.name
    suffix = Path(audio_name).suffix or ".mp3"

    data = uploaded.getvalue()
    audio_id = _hash_upload(audio_name, data)

    out_path = Path(tempfile.gettempdir()) / f"proofing_audio_{audio_id}{suffix.lower()}"
    if not out_path.exists():
        out_path.write_bytes(data)

    return audio_name, str(out_path), audio_id

def get_duration_seconds(audio_path: str) -> float | None:
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
# App
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
            k = safe_key(entered)
            if not k:
                st.error("User key must contain at least one letter/number.")
            else:
                st.query_params["k"] = k
                st.rerun()
    st.stop()

user_key = safe_key(existing_key)
st.caption(f"User key: {user_key}")

state = load_state(user_key)
st.session_state.setdefault("events", state["events"])
st.session_state.setdefault("last_played", state["last_played"])

# Session-only uploaded audio catalogue
st.session_state.setdefault("uploaded_audio", {})  # audio_id -> {"name":..., "path":..., "duration":...}
st.session_state.setdefault("active_audio_id", None)

# Note input is per-session; clear after logging via a one-shot flag.
st.session_state.setdefault("note_input", "")
st.session_state.setdefault("clear_note_next", False)

lp = st.session_state["last_played"]
lp_file = lp.get("audio_file")
lp_time = float(lp.get("time_sec", 0.0) or 0.0)

if lp_file:
    st.info(f"Last played time: {fmt_time_hh(lp_time)} in {lp_file}")
else:
    st.info("Last played time: 00:00:00.000 (no file yet)")

uploaded_files = st.file_uploader(
    "Upload audio (MP3/WAV) — you can select multiple files.",
    type=["mp3", "wav", "m4a", "ogg"],
    accept_multiple_files=True,
)

if uploaded_files:
    with st.spinner("Preparing audio…"):
        newest = None
        for up in uploaded_files:
            audio_name_tmp, audio_path_tmp, audio_id = write_uploaded_to_tmp(up)
            newest = audio_id
            if audio_id not in st.session_state["uploaded_audio"]:
                dur = get_duration_seconds(audio_path_tmp)
                st.session_state["uploaded_audio"][audio_id] = {
                    "name": audio_name_tmp,
                    "path": audio_path_tmp,
                    "duration": dur,
                }

    if newest and newest in st.session_state["uploaded_audio"]:
        st.session_state["active_audio_id"] = newest

catalog = st.session_state["uploaded_audio"]
if catalog:
    durations = [v.get("duration") for v in catalog.values() if isinstance(v.get("duration"), (int, float))]
    total = float(sum(durations)) if durations else None

    st.subheader("Uploaded files (this session)")
    st.caption(
        f"Files: {len(catalog)}"
        + (f" • Total duration: {fmt_duration(total)}" if total is not None else " • Total duration: unknown")
    )

    items = sorted(catalog.items(), key=lambda kv: kv[1]["name"].lower())
    labels = []
    ids = []
    for aid, meta in items:
        labels.append(f'{meta["name"]} — {fmt_duration(meta.get("duration"))}')
        ids.append(aid)

    active_id = st.session_state.get("active_audio_id")
    default_idx = ids.index(active_id) if active_id in ids else 0
    if active_id not in ids:
        st.session_state["active_audio_id"] = ids[0]

    picked_label = st.selectbox("Choose file to play", labels, index=default_idx)
    picked_id = ids[labels.index(picked_label)]
    st.session_state["active_audio_id"] = picked_id

    audio_name = catalog[picked_id]["name"]
    audio_path = catalog[picked_id]["path"]
    duration = catalog[picked_id].get("duration")
else:
    audio_name = None
    audio_path = None
    duration = None

last_time = lp_time

if audio_path:
    st.subheader("Player")

    from streamlit_advanced_audio import audix

    result = audix(audio_path)

    if isinstance(result, dict) and "currentTime" in result:
        last_time = float(result["currentTime"])
        st.session_state["last_played"] = {"audio_file": audio_name, "time_sec": last_time}
        persist_now(user_key)

    if duration is not None:
        st.caption(f"Current: {fmt_time_hh(last_time)} / {fmt_time_hh(float(duration))}")
    else:
        st.caption(f"Current: {fmt_time_hh(last_time)}")

    st.subheader("Log an issue")

    # Clear note *before* widget instantiation on the first rerun after a log.
    if st.session_state.get("clear_note_next"):
        st.session_state["note_input"] = ""
        st.session_state["clear_note_next"] = False

    st.text_input(
        "Optional note",
        key="note_input",
        placeholder="e.g., hard T / rustle / long pause",
    )

    cols = st.columns(len(LABELS))
    clicked_label = None
    for i, label in enumerate(LABELS):
        if cols[i].button(label, width="stretch"):
            clicked_label = label

    if clicked_label:
        note_text = (st.session_state.get("note_input") or "").strip()

        st.session_state["events"].append(
            {
                "audio_file": audio_name,
                "time_sec": float(last_time),
                "timecode": fmt_time_hh(float(last_time)),
                "label": clicked_label,
                "note": note_text,
                "logged_at_epoch": time.time(),
            }
        )

        # Defer clearing until the next rerun (so we don't mutate the widget's key after instantiation).
        st.session_state["clear_note_next"] = True

        st.session_state["last_played"] = {"audio_file": audio_name, "time_sec": float(last_time)}
        persist_now(user_key)
        st.rerun()

st.divider()

st.subheader("Logged issues")

events = st.session_state["events"]

if not events:
    st.info("No issues logged yet.")
else:
    df = pd.DataFrame(events)

    for c in ["audio_file", "time_sec", "timecode", "label", "note", "logged_at_epoch"]:
        if c not in df.columns:
            df[c] = ""

    def repair_timecode(row):
        tc = str(row.get("timecode", "") or "").strip()
        if tc:
            return tc
        try:
            return fmt_time_hh(float(row.get("time_sec", 0.0) or 0.0))
        except Exception:
            return "00:00:00.000"

    df["timecode"] = df.apply(repair_timecode, axis=1)

    if "Delete" not in df.columns:
        df.insert(0, "Delete", False)
    else:
        df["Delete"] = df["Delete"].fillna(False).astype(bool)

    edited = st.data_editor(
        df,
        key="events_editor",
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        disabled=["audio_file", "time_sec", "logged_at_epoch"],
        column_config={
            "Delete": st.column_config.CheckboxColumn("Delete", help="Tick and press 'Apply deletions'."),
            "timecode": st.column_config.TextColumn("timecode", help="Editable. Format HH:MM:SS.mmm (or MM:SS.mmm)."),
            "label": st.column_config.TextColumn("label"),
            "note": st.column_config.TextColumn("note"),
        },
    )

    c1, c2, c3, c4 = st.columns(4)

    def rebuild_events_from_frame(frame: pd.DataFrame) -> list[dict]:
        new_events = []
        for _, r in frame.iterrows():
            tc = str(r.get("timecode", "") or "").strip()
            sec = parse_timecode_to_seconds(tc)
            if sec is None:
                try:
                    sec = float(r.get("time_sec", 0.0) or 0.0)
                except Exception:
                    sec = 0.0

            new_events.append(
                {
                    "audio_file": str(r.get("audio_file", "") or ""),
                    "time_sec": float(sec),
                    "timecode": fmt_time_hh(float(sec)),
                    "label": str(r.get("label", "") or ""),
                    "note": str(r.get("note", "") or ""),
                    "logged_at_epoch": float(r.get("logged_at_epoch", 0.0) or 0.0),
                }
            )
        return new_events

    with c1:
        if st.button("Apply deletions", width="stretch"):
            kept = edited[~edited["Delete"].fillna(False).astype(bool)].copy()
            if "Delete" in kept.columns:
                kept = kept.drop(columns=["Delete"])
            st.session_state["events"] = rebuild_events_from_frame(kept)
            persist_now(user_key)
            st.rerun()

    with c2:
        if st.button("Undo last", width="stretch") and st.session_state["events"]:
            st.session_state["events"].pop()
            persist_now(user_key)
            st.rerun()

    with c3:
        if st.button("Clear all", width="stretch"):
            st.session_state["events"] = []
            persist_now(user_key)
            st.rerun()

    with c4:
        st.download_button(
            "Download CSV",
            data=events_to_csv_bytes(st.session_state["events"]),
            file_name="proofing_log.csv",
            mime="text/csv",
            width="stretch",
        )

    def normalise_for_compare(frame: pd.DataFrame) -> pd.DataFrame:
        f = frame.copy()
        if "Delete" in f.columns:
            f = f.drop(columns=["Delete"])
        return f.fillna("")

    try:
        current_df = normalise_for_compare(df)
        edited_df = normalise_for_compare(edited)
        if not current_df.equals(edited_df):
            st.session_state["events"] = rebuild_events_from_frame(edited_df)
            persist_now(user_key)
    except Exception:
        pass

st.caption("Tip: bookmark this page URL — it includes your user key, so refresh will restore your log and last-played reminder.")
