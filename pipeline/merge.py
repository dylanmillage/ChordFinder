"""Phase 4: merge word timestamps with chord segments.

Every chord change is displayed exactly once:
- if the change happens within --gap seconds before a word, it is attached
  to that word (several changes before one word: the word gets the chord
  actually sounding when it is sung);
- otherwise (intro, solo, instrumental break) it becomes a pseudo-word
  entry rendered as a musical note, so strummed sections with no vocals
  still show their chords in sequence.

Usage:
    python pipeline/merge.py <video_id> [--gap 3.0]

Creates:
    songs/<video_id>/combined.json
    list of {word, start_time, end_time, line, chord (nullable)},
    sorted by start_time; instrumental entries carry "instrumental": true.

Re-run this (and only this) after hand-editing chords.json or words.json.
"""
import argparse
import json
import sys

from common import fmt_time, song_dir

NOTE_SYMBOL = "♪"  # ♪


def merge(words: list[dict], chords: list[dict], gap: float) -> list[dict]:
    # "wi" (index into words.json) and "chord_time" (segment start in
    # chords.json) let the edit-mode UI address the source-file entry an
    # edit belongs to.
    out = [{**w, "chord": None, "wi": i} for i, w in enumerate(words)]
    pseudo = []
    for seg in sorted(chords, key=lambda c: c["start_time"]):
        target = next((w for w in out if w["start_time"] >= seg["start_time"]), None)
        if target is not None and target["start_time"] - seg["start_time"] <= gap:
            target["chord"] = seg["chord"]  # later change overwrites: it's the
            # chord sounding when this word is sung
            target["chord_time"] = seg["start_time"]
        else:
            pseudo.append({
                "word": NOTE_SYMBOL,
                "start_time": seg["start_time"],
                "end_time": seg["end_time"],
                "chord": seg["chord"],
                "chord_time": seg["start_time"],
                "instrumental": True,
            })

    combined = sorted(out + pseudo, key=lambda w: w["start_time"])

    # Group runs of consecutive instrumental entries into their own display
    # lines ("i0", "i1", ...) so the frontend renders them between lyric lines.
    group = -1
    prev_instrumental = False
    for w in combined:
        if w.get("instrumental"):
            if not prev_instrumental:
                group += 1
            w["line"] = f"i{group}"
        prev_instrumental = bool(w.get("instrumental"))
    return combined


def main() -> None:
    # Windows consoles often default to cp1252, which can't print "♪".
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("--gap", type=float, default=3.0,
                        help="max seconds between a chord change and the next "
                             "word for the chord to attach to that word; "
                             "changes further out render as instrumental notes")
    args = parser.parse_args()

    out_dir = song_dir(args.video_id)
    try:
        words = json.loads((out_dir / "words.json").read_text(encoding="utf-8"))
        chords = json.loads((out_dir / "chords.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        sys.exit(f"{e.filename} not found - run the earlier phases first.")

    combined = merge(words, chords, args.gap)

    out_path = out_dir / "combined.json"
    out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    attached = sum(1 for w in combined if w["chord"] and not w.get("instrumental"))
    instrumental = sum(1 for w in combined if w.get("instrumental"))
    print(f"Saved {len(combined)} entries ({attached} words carrying a chord, "
          f"{instrumental} instrumental chord marks) -> {out_path}\n")

    # Preview in familiar chord-sheet style: [G]word word [C]word ...
    print("=== Preview ===")
    current_line = None
    for w in combined:
        if w.get("line") != current_line:
            current_line = w.get("line")
            print(f"\n[{fmt_time(w['start_time'])}] ", end="")
        prefix = f"[{w['chord']}]" if w["chord"] else ""
        print(f"{prefix}{w['word']}", end=" ")
    print()


if __name__ == "__main__":
    main()
