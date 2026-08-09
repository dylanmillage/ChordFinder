"""Parity check: the browser import (frontend/import-sheet.js) must place
chords exactly where pipeline/import_sheet.py does.

    python cloud/compare_import.py <video_id> <sheet.txt>

Exits non-zero on any difference, so it can gate a deploy.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from import_sheet import import_sheet  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    vid, sheet_path = sys.argv[1], Path(sys.argv[2])
    song = ROOT / "songs" / vid
    words_path = song / "words.json"
    det = song / "chords.detected.json"
    chords_path = det if det.exists() else song / "chords.json"

    text = sheet_path.read_text(encoding="utf-8")
    words = json.loads(words_path.read_text(encoding="utf-8"))
    chords = json.loads(chords_path.read_text(encoding="utf-8"))
    py_segments, py_report = import_sheet(text, words, chords)

    cp = subprocess.run(
        ["node", str(ROOT / "cloud" / "_parity.js"), str(sheet_path),
         str(words_path), str(chords_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT / "cloud"))
    if cp.returncode:
        sys.exit(f"node failed:\n{cp.stderr[-800:]}")
    js = json.loads(cp.stdout)

    print(f"python: {py_report['matched_lines']} lines, "
          f"{py_report['chords_placed']} chords, offset {py_report['key_offset']}, "
          f"{len(py_segments)} segments")
    print(f"node:   {js['matched_lines']} lines, {js['chords_placed']} chords, "
          f"offset {js['key_offset']}, {len(js['segments'])} segments")

    problems = []
    for field, a, b in [("matched_lines", py_report["matched_lines"], js["matched_lines"]),
                        ("chords_placed", py_report["chords_placed"], js["chords_placed"]),
                        ("key_offset", py_report["key_offset"], js["key_offset"]),
                        ("kept_detected", py_report["kept_detected"], js["kept_detected"])]:
        if a != b:
            problems.append(f"  {field}: python={a} node={b}")

    if len(py_segments) != len(js["segments"]):
        problems.append(f"  segment count: python={len(py_segments)} "
                        f"node={len(js['segments'])}")
    else:
        for i, (p, j) in enumerate(zip(py_segments, js["segments"])):
            if (p["chord"] != j["chord"]
                    or abs(p["start_time"] - j["start_time"]) > 0.011
                    or abs(p["end_time"] - j["end_time"]) > 0.011):
                problems.append(f"  segment {i}: python={p} node={j}")
            if len(problems) > 8:
                break

    if problems:
        print("\nMISMATCH:")
        print("\n".join(problems[:10]))
        sys.exit(1)
    print("\nIdentical: every segment matches (chord, start, end).")


if __name__ == "__main__":
    main()
