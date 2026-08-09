"""Publish the song library to Firestore so the iPad (or any device) can
read it from the hosted app.

Only the small JSON is uploaded - lyrics, chords, metadata. Audio never
leaves this PC: playback on every device comes from the YouTube player.

Setup (one time):
    1. Create a project at https://console.firebase.google.com (free Spark plan)
    2. Build > Firestore Database > Create database (production mode)
    3. Project settings > Service accounts > Generate new private key
       -> save the downloaded file as cloud/serviceAccountKey.json
    4. pip install firebase-admin   (already done in the chordfinder venv)

Usage:
    python pipeline/publish.py            # upload songs that changed
    python pipeline/publish.py --all      # re-upload everything
    python pipeline/publish.py --list     # show what is already published
"""
import argparse
import hashlib
import json
import sys

from common import PROJECT_ROOT, song_dir  # noqa: F401  (song_dir re-exported)

KEY_PATH = PROJECT_ROOT / "cloud" / "serviceAccountKey.json"
STATE_PATH = PROJECT_ROOT / "cloud" / "published.json"


def song_payload(d):
    """The document stored per song: metadata + the merged word/chord list."""
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    combined = json.loads((d / "combined.json").read_text(encoding="utf-8"))
    chords = []
    try:
        chords = sorted({s["chord"] for s in
                         json.loads((d / "chords.json").read_text(encoding="utf-8"))})
    except Exception:
        pass
    # The hosted app can import chord sheets too, which needs the same inputs
    # the PC uses: the raw detection (for the capo/key offset and the
    # intro/solo sections) plus the current chord segments.
    det = d / "chords.detected.json"
    src = det if det.exists() else d / "chords.json"
    detected = json.loads(src.read_text(encoding="utf-8"))
    segments = json.loads((d / "chords.json").read_text(encoding="utf-8"))
    return {
        "video_id": d.name,
        "title": meta.get("title") or d.name,
        "artist": meta.get("artist") or "",
        "capo": int(meta.get("capo") or 0),
        "duration": int(meta.get("duration") or 0),
        "strum": meta.get("strum"),
        "chords": chords,
        "imported": det.exists(),
        "words": combined,
        "detected_segments": detected,
        "chord_segments": segments,
    }


def payload_hash(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def pull_cloud_edits(db) -> int:
    """Bring chord-sheet imports done on the iPad back down to this PC.

    The hosted app writes chord_segments + edited_in_cloud when you import a
    sheet there. Without this, the next publish would silently overwrite that
    edit with the PC's older copy.
    """
    import sys as _sys
    _sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))
    from merge import merge as merge_words_chords

    pulled = 0
    for doc in db.collection("songs").where("edited_in_cloud", "==", True).stream():
        d = PROJECT_ROOT / "songs" / doc.id
        data = doc.to_dict() or {}
        segments = data.get("chord_segments")
        if not (d.is_dir() and segments):
            continue
        # Keep the original detection as the import baseline, once.
        det = d / "chords.detected.json"
        if not det.exists() and (d / "chords.json").exists():
            det.write_text((d / "chords.json").read_text(encoding="utf-8"),
                           encoding="utf-8")
        body = ",\n".join("  " + json.dumps(s) for s in segments)
        (d / "chords.json").write_text("[\n" + body + "\n]\n", encoding="utf-8")
        words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        combined = merge_words_chords(words, segments, 3.0)
        (d / "combined.json").write_text(
            json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")
        doc.reference.update({"edited_in_cloud": False})
        pulled += 1
        print(f"  pulled down  {data.get('title', doc.id)} (imported on another device)")
    return pulled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="re-upload every song")
    parser.add_argument("--list", action="store_true", help="show published songs")
    parser.add_argument("--key", default=str(KEY_PATH), help="service account JSON")
    args = parser.parse_args()

    if not (key := __import__("pathlib").Path(args.key)).exists():
        sys.exit(f"Service account key not found: {key}\n"
                 "Firebase console > Project settings > Service accounts >\n"
                 "Generate new private key, then save it to that path.")

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        sys.exit("pip install firebase-admin (into the chordfinder venv)")

    firebase_admin.initialize_app(credentials.Certificate(str(key)))
    db = firestore.client()

    if args.list:
        for doc in db.collection("songs").stream():
            d = doc.to_dict()
            print(f"  {doc.id}  {d.get('artist','?')} - {d.get('title','?')}")
        return

    state = {}
    if STATE_PATH.exists() and not args.all:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    pulled = pull_cloud_edits(db)

    songs_dir = PROJECT_ROOT / "songs"
    uploaded = skipped = 0
    index = []
    for d in sorted(p for p in songs_dir.iterdir() if p.is_dir()):
        if not (d / "combined.json").exists():
            continue          # still processing; nothing to publish yet
        payload = song_payload(d)
        # Light summary for the library list: one read instead of one per song.
        index.append({k: payload[k] for k in
                      ("video_id", "title", "artist", "capo", "duration",
                       "chords", "imported", "strum")})
        h = payload_hash(payload)
        if state.get(d.name) == h:
            skipped += 1
            continue
        db.collection("songs").document(d.name).set(payload)
        state[d.name] = h
        uploaded += 1
        print(f"  uploaded  {payload['artist']} - {payload['title']}")

    db.collection("library").document("index").set(
        {"songs": index, "count": len(index)})
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    if pulled:
        print(f"\nPulled {pulled} cloud edit(s) into the local files first.")
    print(f"Published {uploaded} song(s); {skipped} already up to date.")
    if uploaded:
        print("The hosted app will show them on the next refresh.")


if __name__ == "__main__":
    main()
