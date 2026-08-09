"""ChordFinder practice-view server.

Serves the project folder like `python -m http.server`, plus:
- POST /api/edit/<video_id>   edit-mode saves (words/chords -> source JSON)
- GET  /api/songs             list songs + processing state
- POST /api/add               {url, capo}: run the whole pipeline for a new
                              song in a background thread
- GET  /api/status/<video_id> job progress for the UI to poll

Usage:
    python serve.py [port]     (default 8321; binds localhost only)
"""
import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))
from import_sheet import import_sheet  # noqa: E402
from merge import merge as merge_words_chords  # noqa: E402

VID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
MERGE_GAP = 3.0  # keep in sync with merge.py's --gap default

# The pipeline runs in two dedicated venvs (see README).
PY_MAIN = Path.home() / ".venvs" / "chordfinder" / "Scripts" / "python.exe"
PY_CHORDS = Path.home() / ".venvs" / "chordfinder-chords" / "Scripts" / "python.exe"

JOBS: dict[str, dict] = {}  # video_id -> {"state": ..., "title": ..., "detail": ...}
JOBS_LOCK = threading.Lock()

PROBE_SRC = """\
import json, sys, yt_dlp
info = yt_dlp.YoutubeDL({'quiet': True, 'noplaylist': True}).extract_info(sys.argv[1], download=False)
if info.get('_type') == 'playlist':
    info = info['entries'][0]
print(json.dumps({'id': info['id'], 'title': info.get('title')}))
"""


def set_job(video_id: str, **fields) -> None:
    with JOBS_LOCK:
        JOBS.setdefault(video_id, {}).update(fields)


PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")

# Each pipeline stage owns a slice of the overall 0-100% progress bar,
# roughly proportional to how long it takes on this machine.
STAGE_SPAN = {
    "download":   (0, 10),
    "transcribe": (10, 70),
    "chords":     (70, 95),
    "merge":      (95, 100),
}

# Sub-stage markers: transcribe.py/chords.py print these, and only some
# steps emit percentages, so named phases keep the bar moving honestly.
TRANSCRIBE_MARKS = [("separating vocals", 2), ("loading audio", 62),
                    ("transcribing", 66), ("aligning", 86)]
CHORD_MARKS = [("engine:", 5), ("running btc", 25), ("cnn chord features", 25),
               ("crf decoding", 70), ("saved", 90)]


def run_step(name: str, cmd: list, on_line=None) -> str:
    """Run a pipeline command, streaming its output so progress can be
    reported live. Returns the full captured output."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([str(c) for c in cmd], cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env)
    tail = collections.deque(maxlen=20)
    captured, buf = [], ""
    while True:
        # read1 returns as soon as any bytes are available, so progress
        # bars (which use \r, not \n) surface in near real time.
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for line in parts:
            line = line.strip()
            if not line:
                continue
            captured.append(line)
            tail.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
    if buf.strip():
        captured.append(buf.strip())
        tail.append(buf.strip())
        if on_line:
            try:
                on_line(buf.strip())
            except Exception:
                pass
    proc.wait()
    if proc.returncode:
        raise RuntimeError(f"{name} failed: {(tail[-1] if tail else 'unknown')[-300:]}")
    return "\n".join(captured)


def stage_reporter(video_id: str, stage: str, marks=None, pct_scale=None):
    """-> callback that turns a subprocess line into an overall percentage."""
    start, end = STAGE_SPAN[stage]
    span = end - start
    state = {"inner": 0}

    def set_inner(inner: float) -> None:
        inner = max(0.0, min(100.0, inner))
        if inner >= state["inner"]:      # never let the bar go backwards
            state["inner"] = inner
            set_job(video_id, percent=round(start + span * inner / 100))

    def on_line(line: str) -> None:
        low = line.lower()
        for text, inner in (marks or []):
            if text in low:
                set_inner(inner)
        m = PCT_RE.search(line)
        if m:
            pct = float(m.group(1))
            if pct_scale:                # e.g. Demucs owns 2-62% of the stage
                lo, hi = pct_scale
                set_inner(lo + (hi - lo) * pct / 100)
            else:
                set_inner(pct)
    return on_line


def probe_video(url: str) -> dict:
    out = run_step("resolving the URL", [PY_MAIN, "-c", PROBE_SRC, url])
    for line in reversed(out.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("could not read video info")


def write_combined(video_id: str, words: list, chords: list) -> None:
    combined = merge_words_chords(words, chords, MERGE_GAP)
    (ROOT / "songs" / video_id / "combined.json").write_text(
        json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")


def pipeline_job(video_id: str, url: str, capo: int) -> None:
    """Download -> transcribe -> chords -> merge, updating JOBS as it goes."""
    try:
        set_job(video_id, state="downloading audio", percent=0)
        run_step("download", [PY_MAIN, "pipeline/download_audio.py", url],
                 on_line=stage_reporter(video_id, "download"))

        set_job(video_id, state="separating vocals + transcribing")
        # defaults: Demucs vocal separation + Whisper large-v2 on the GPU.
        # Demucs prints percentages and owns the first ~60% of this stage;
        # named marks carry the bar through Whisper and alignment.
        run_step("transcribe", [PY_MAIN, "pipeline/transcribe.py", video_id,
                                "--language", "en"],
                 on_line=stage_reporter(video_id, "transcribe",
                                        marks=TRANSCRIBE_MARKS, pct_scale=(2, 62)))

        set_job(video_id, state="detecting chords")
        # --engine auto: BTC transformer in this venv; madmom/librosa fallback.
        cmd = [PY_MAIN, "pipeline/chords.py", video_id]
        if capo:
            cmd += ["--capo", str(capo)]
        run_step("chords", cmd,
                 on_line=stage_reporter(video_id, "chords", marks=CHORD_MARKS))

        set_job(video_id, state="merging", percent=95)
        song = ROOT / "songs" / video_id
        words = json.loads((song / "words.json").read_text(encoding="utf-8"))
        chords = json.loads((song / "chords.json").read_text(encoding="utf-8"))
        write_combined(video_id, words, chords)
        set_job(video_id, state="done")
    except Exception as exc:
        set_job(video_id, state="error", detail=str(exc)[:500])


def capo_job(video_id: str, capo: int) -> None:
    """Re-run chord detection with a different capo, then re-merge.
    Replaces chords.json (and drops the simplify backup, which would
    otherwise restore chords at the old capo)."""
    try:
        set_job(video_id, state=f"re-detecting chords (capo {capo})", percent=0)
        run_step("chords", [PY_MAIN, "pipeline/chords.py", video_id,
                            "--capo", str(capo)],
                 on_line=stage_reporter(video_id, "chords", marks=CHORD_MARKS))
        set_job(video_id, state="merging", percent=95)
        song = ROOT / "songs" / video_id
        full = song / "chords.full.json"
        if full.exists():
            full.unlink()
        words = json.loads((song / "words.json").read_text(encoding="utf-8"))
        chords = json.loads((song / "chords.json").read_text(encoding="utf-8"))
        write_combined(video_id, words, chords)
        set_job(video_id, state="done", percent=100)
    except Exception as exc:
        set_job(video_id, state="error", detail=str(exc)[:500])


# Songs are processed ONE at a time: transcription and chord recognition
# both want the whole GPU, and running several at once overflows its memory
# into system RAM, slowing everything down 10-100x.
JOB_QUEUE: "queue.Queue[tuple]" = queue.Queue()


QUEUE_ORDER: list = []   # video_ids still waiting, in order (JOBS_LOCK)


def job_worker() -> None:
    while True:
        kind, video_id, url, capo = JOB_QUEUE.get()
        with JOBS_LOCK:
            if video_id in QUEUE_ORDER:
                QUEUE_ORDER.remove(video_id)
        try:
            if kind == "capo":
                capo_job(video_id, capo)
            else:
                pipeline_job(video_id, url, capo)
        finally:
            JOB_QUEUE.task_done()


threading.Thread(target=job_worker, daemon=True).start()


def enqueue_job(video_id: str, url, capo: int, title: str,
                kind: str = "full") -> None:
    with JOBS_LOCK:
        QUEUE_ORDER.append(video_id)
    set_job(video_id, state="waiting in line", title=title, detail="", percent=0)
    JOB_QUEUE.put((kind, video_id, url, capo))


def queue_position(video_id: str) -> int:
    """1-based place in line, or 0 when not waiting."""
    with JOBS_LOCK:
        return QUEUE_ORDER.index(video_id) + 1 if video_id in QUEUE_ORDER else 0


def list_songs(detail: bool = False) -> list[dict]:
    out = []
    songs_dir = ROOT / "songs"
    if songs_dir.is_dir():
        for d in sorted(songs_dir.iterdir()):
            if not d.is_dir():
                continue
            title, artist, capo, duration, strum = d.name, "", 0, 0, None
            meta = d / "meta.json"
            if meta.exists():
                try:
                    mj = json.loads(meta.read_text(encoding="utf-8"))
                    title = mj.get("title") or d.name
                    artist = mj.get("artist") or ""
                    capo = int(mj.get("capo") or 0)
                    duration = int(mj.get("duration") or 0)
                    strum = mj.get("strum")
                except Exception:
                    pass
            entry = {"video_id": d.name, "title": title, "artist": artist,
                     "capo": capo, "strum": strum,
                     "ready": (d / "combined.json").exists(),
                     "simplified": (d / "chords.full.json").exists(),
                     # the import flow always backs up the detection first,
                     # so this file existing == chords came from a sheet
                     "imported": (d / "chords.detected.json").exists()}
            if detail:
                entry["duration"] = duration
                chords = []
                try:
                    segs = json.loads((d / "chords.json").read_text(encoding="utf-8"))
                    chords = sorted({s["chord"] for s in segs})
                except Exception:
                    pass
                entry["chords"] = chords
            out.append(entry)
    with JOBS_LOCK:
        for vid, job in JOBS.items():
            entry = next((e for e in out if e["video_id"] == vid), None)
            if entry is None:
                entry = {"video_id": vid, "title": job.get("title") or vid, "ready": False}
                out.append(entry)
            if job["state"] not in ("done",):
                entry["state"] = job["state"]
    return out


def write_chords(path: Path, chords: list[dict]) -> None:
    """Same hand-editable one-segment-per-line format chords.py writes."""
    body = ",\n".join("  " + json.dumps(seg) for seg in chords)
    path.write_text("[\n" + body + "\n]\n", encoding="utf-8")


CHORD_RE = re.compile(r"^([A-G]#?)(.*)$")


def simplify_chord(chord: str) -> str:
    """Collapse a chord to its basic playable shape: Em7 -> Em, Dsus4 -> D,
    A7sus4 -> A, Bm7b5 -> Bm. Minor-family suffixes keep the m."""
    m = CHORD_RE.match(chord)
    if not m:
        return chord
    root, suffix = m.groups()
    minorish = (suffix.startswith("m") and not suffix.startswith("maj")) \
        or suffix.startswith("dim")
    return root + ("m" if minorish else "")


def toggle_simplify(video_id: str) -> bool:
    """Simplify all chords (backing up the detailed ones), or restore the
    backup if one exists. Returns True when now simplified."""
    song = ROOT / "songs" / video_id
    chords_path = song / "chords.json"
    full_path = song / "chords.full.json"
    if full_path.exists():
        chords = json.loads(full_path.read_text(encoding="utf-8"))
        write_chords(chords_path, chords)
        full_path.unlink()
        simplified = False
    else:
        chords = json.loads(chords_path.read_text(encoding="utf-8"))
        full_path.write_text(chords_path.read_text(encoding="utf-8"),
                             encoding="utf-8")
        merged = []
        for seg in chords:
            seg = {**seg, "chord": simplify_chord(seg["chord"])}
            if merged and seg["chord"] == merged[-1]["chord"]:
                merged[-1]["end_time"] = seg["end_time"]
            else:
                merged.append(seg)
        chords = merged
        write_chords(chords_path, chords)
        simplified = True
    words = json.loads((song / "words.json").read_text(encoding="utf-8"))
    write_combined(video_id, words, chords)
    return simplified


def apply_edit(video_id: str, edit: dict) -> None:
    song = ROOT / "songs" / video_id
    words_path = song / "words.json"
    chords_path = song / "chords.json"
    words = json.loads(words_path.read_text(encoding="utf-8"))
    chords = json.loads(chords_path.read_text(encoding="utf-8"))

    kind = edit.get("type")
    if kind == "word":
        # Empty text deletes the word; several words split the original
        # word's time span evenly (how missing lyrics get added).
        idx = int(edit["index"])
        if not 0 <= idx < len(words):
            raise ValueError(f"word index {idx} out of range")
        parts = str(edit["word"]).split()
        if not parts:
            del words[idx]
        elif len(parts) == 1:
            words[idx]["word"] = parts[0]
        else:
            old = words.pop(idx)
            dur = (old["end_time"] - old["start_time"]) / len(parts)
            for j, part in enumerate(parts):
                words.insert(idx + j, {
                    "word": part,
                    "start_time": round(old["start_time"] + j * dur, 3),
                    "end_time": round(old["start_time"] + (j + 1) * dur, 3),
                    "line": old.get("line"),
                })
        words_path.write_text(json.dumps(words, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    elif kind == "chord":  # rename, or delete when the new label is empty
        t = float(edit["chord_time"])
        seg = next((s for s in chords if abs(s["start_time"] - t) < 0.005), None)
        if seg is None:
            raise ValueError(f"no chord segment starting at {t}")
        label = str(edit["chord"]).strip()
        if label:
            seg["chord"] = label
        else:
            chords.remove(seg)
        write_chords(chords_path, chords)
    elif kind == "add_chord":
        chords.append({"chord": str(edit["chord"]).strip(),
                       "start_time": round(float(edit["start_time"]), 2),
                       "end_time": round(float(edit["end_time"]), 2)})
        chords.sort(key=lambda s: s["start_time"])
        write_chords(chords_path, chords)
    else:
        raise ValueError(f"unknown edit type: {kind!r}")

    write_combined(video_id, words, chords)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # Local dev server: make browsers revalidate instead of using stale
        # cached copies, so UI updates show up on plain refresh.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _respond(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path.startswith("/api/songs"):
            self._respond(200, list_songs(detail="detail=1" in self.path))
            return
        m = re.match(r"^/api/status/([^/?]+)$", self.path)
        if m:
            vid = m.group(1)
            with JOBS_LOCK:
                job = dict(JOBS.get(vid, {}))
            if not job:
                ready = (ROOT / "songs" / vid / "combined.json").exists()
                job = {"state": "done" if ready else "unknown"}
            pos = queue_position(vid)
            if pos:
                job["queue_position"] = pos
            self._respond(200, {"ok": True, **job})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/add":
            try:
                body = self._read_body()
                url = str(body.get("url", "")).strip()
                capo = max(0, min(11, int(body.get("capo") or 0)))
                if not url:
                    raise ValueError("no URL given")
                if not PY_MAIN.exists() or not PY_CHORDS.exists():
                    raise RuntimeError("pipeline venvs not found - see README setup")
                info = probe_video(url)
                vid, title = info["id"], info.get("title") or info["id"]
            except Exception as exc:
                self._respond(400, {"ok": False, "error": str(exc)})
                return
            if (ROOT / "songs" / vid / "combined.json").exists():
                self._respond(200, {"ok": True, "video_id": vid, "title": title,
                                    "state": "done"})
                return
            with JOBS_LOCK:
                running = vid in JOBS and JOBS[vid]["state"] not in ("done", "error")
            if not running:
                enqueue_job(vid, url, capo, title)
            self._respond(200, {"ok": True, "video_id": vid, "title": title,
                                "state": "waiting in line"})
            return

        m = re.match(r"^/api/process/([^/?]+)$", self.path)
        if m:   # finish a song whose processing was interrupted
            vid = m.group(1)
            song = ROOT / "songs" / vid
            if not (VID_RE.match(vid) and (song / "meta.json").exists()):
                self._respond(404, {"ok": False, "error": "unknown video id"})
                return
            if (song / "combined.json").exists():
                self._respond(200, {"ok": True, "video_id": vid, "state": "done"})
                return
            with JOBS_LOCK:
                running = vid in JOBS and JOBS[vid]["state"] not in ("done", "error")
            if not running:
                try:
                    body = self._read_body() if self.headers.get("Content-Length") else {}
                    meta = json.loads((song / "meta.json").read_text(encoding="utf-8"))
                    capo = max(0, min(11, int(body.get("capo") or meta.get("capo") or 0)))
                    enqueue_job(vid, meta["url"], capo, meta.get("title") or vid)
                except Exception as exc:
                    self._respond(400, {"ok": False, "error": str(exc)})
                    return
            self._respond(200, {"ok": True, "video_id": vid,
                                "state": "waiting in line"})
            return

        m = re.match(r"^/api/capo/([^/?]+)$", self.path)
        if m:   # re-detect chords with a different capo
            vid = m.group(1)
            song = ROOT / "songs" / vid
            if not (VID_RE.match(vid) and (song / "combined.json").exists()):
                self._respond(404, {"ok": False, "error": "song not processed yet"})
                return
            with JOBS_LOCK:
                running = vid in JOBS and JOBS[vid]["state"] not in ("done", "error")
            if running:
                self._respond(400, {"ok": False, "error": "song is busy - try again shortly"})
                return
            try:
                body = self._read_body()
                capo = max(0, min(11, int(body.get("capo") or 0)))
                meta = json.loads((song / "meta.json").read_text(encoding="utf-8"))
                enqueue_job(vid, None, capo, meta.get("title") or vid, kind="capo")
            except Exception as exc:
                self._respond(400, {"ok": False, "error": str(exc)})
                return
            self._respond(200, {"ok": True, "video_id": vid,
                                "state": "waiting in line"})
            return

        m = re.match(r"^/api/strum/([^/?]+)$", self.path)
        if m:   # save (or clear) a manually entered strumming pattern
            vid = m.group(1)
            song = ROOT / "songs" / vid
            if not (VID_RE.match(vid) and (song / "meta.json").exists()):
                self._respond(404, {"ok": False, "error": "unknown video id"})
                return
            try:
                body = self._read_body()
                pattern = body.get("pattern")
                meta = json.loads((song / "meta.json").read_text(encoding="utf-8"))
                if pattern:
                    if not (isinstance(pattern, list) and len(pattern) in (8, 16)
                            and all(p in ("D", "U", "-") for p in pattern)):
                        raise ValueError("pattern must be 8 or 16 slots of D/U/-")
                    strum = {"pattern": pattern}
                    if body.get("bpm"):
                        strum["bpm"] = max(20, min(300, int(body["bpm"])))
                    meta["strum"] = strum
                else:
                    meta.pop("strum", None)
                (song / "meta.json").write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                self._respond(400, {"ok": False, "error": str(exc)})
                return
            self._respond(200, {"ok": True})
            return

        m = re.match(r"^/api/import/([^/?]+)$", self.path)
        if m:   # paste-import a chord sheet: preview, or commit=true to apply
            vid = m.group(1)
            song = ROOT / "songs" / vid
            if not (VID_RE.match(vid) and (song / "words.json").exists()):
                self._respond(404, {"ok": False, "error": "song not processed yet"})
                return
            try:
                body = self._read_body()
                words = json.loads((song / "words.json").read_text(encoding="utf-8"))
                # Re-importing must compare against the ORIGINAL detection,
                # not a previous import's output, or the key-offset check and
                # the kept intro/solo chords compound each other.
                det = song / "chords.detected.json"
                src = det if det.exists() else song / "chords.json"
                chords = json.loads(src.read_text(encoding="utf-8"))
                segments, report = import_sheet(str(body.get("sheet", "")), words, chords)
                if body.get("commit"):
                    backup = song / "chords.detected.json"
                    if not backup.exists():   # keep the pre-import detection once
                        write_chords(backup, chords)
                    full = song / "chords.full.json"
                    if full.exists():         # simplify backup is now stale
                        full.unlink()
                    write_chords(song / "chords.json", segments)
                    write_combined(vid, words, segments)
            except ValueError as exc:
                self._respond(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._respond(400, {"ok": False, "error": f"import failed: {exc}"})
                return
            self._respond(200, {"ok": True, "committed": bool(body.get("commit")),
                                **report})
            return

        m = re.match(r"^/api/(edit|simplify)/([^/?]+)$", self.path)
        action, video_id = (m.group(1), m.group(2)) if m else (None, None)
        if not (video_id and VID_RE.match(video_id)
                and (ROOT / "songs" / video_id).is_dir()):
            self._respond(404, {"ok": False, "error": "unknown video id"})
            return
        try:
            if action == "simplify":
                simplified = toggle_simplify(video_id)
                self._respond(200, {"ok": True, "simplified": simplified})
                return
            apply_edit(video_id, self._read_body())
        except Exception as exc:  # surface the reason to the UI
            self._respond(400, {"ok": False, "error": str(exc)})
            return
        self._respond(200, {"ok": True})


def lan_ip() -> str:
    """This machine's address on the local network (no traffic is sent)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8321
    # --lan also serves phones/tablets on the same network (or over Tailscale).
    open_to_network = "--lan" in sys.argv
    host = "0.0.0.0" if open_to_network else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ChordFinder: http://localhost:{port}/frontend/songs.html")
    if open_to_network:
        print(f"On this network: http://{lan_ip()}:{port}/frontend/songs.html")
        print("  (other devices need Windows Firewall to allow Python on "
              "private networks; anyone on the network can reach it)")
    print("Edit-mode saves write to songs/<id>/*.json. Ctrl+C to stop.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
