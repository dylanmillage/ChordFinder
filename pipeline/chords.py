"""Phase 3: automatic chord recognition.

Three engines, same output format:
- btc (preferred): Bi-directional Transformer (ISMIR 2019) with the
  170-chord large vocabulary (7ths, sus, dim...). Needs vendor/BTC-ISMIR19
  and a torch-equipped python (the whisperx venv; GPU used automatically).
- madmom: CNN chord features + CRF decoding, major/minor only. Needs the
  madmom venv (C:\\Users\\dylan\\.venvs\\chordfinder-chords).
- librosa (fallback): chroma template matching. Installs anywhere.

--engine auto (default) picks the best one importable in the running venv.

Usage:
    python pipeline/chords.py <video_id> [--capo N] [--min-duration 0.6]
                              [--engine auto|btc|madmom|librosa]

--capo N transposes detected chord names down N semitones so the labels
match the shapes you actually play on a capo'd recording (e.g. a song
sounding in F#m played capo-2 shows Em).

Creates:
    songs/<video_id>/chords.json  list of {chord, start_time, end_time},
    one entry per line - open it in a text editor to fix any wrong labels,
    then re-run merge.py (no need to re-run this script).
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from common import PROJECT_ROOT, ensure_ffmpeg, fmt_time, song_dir

BTC_DIR = PROJECT_ROOT / "vendor" / "BTC-ISMIR19"

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# The flat no-chord template matches busy/spread chroma too eagerly;
# scale it down so it only wins on genuinely non-harmonic frames.
N_TEMPLATE_SCALE = 0.8


def chord_labels() -> list[str]:
    """25 states: 12 major, 12 minor, and N (no chord)."""
    return NOTE_NAMES + [n + "m" for n in NOTE_NAMES] + ["N"]


def chord_templates() -> np.ndarray:
    """Binary chroma templates for each state, L2-normalised."""
    templates = []
    for root in range(12):
        t = np.zeros(12)
        t[[root, (root + 4) % 12, (root + 7) % 12]] = 1  # major triad
        templates.append(t)
    for root in range(12):
        t = np.zeros(12)
        t[[root, (root + 3) % 12, (root + 7) % 12]] = 1  # minor triad
        templates.append(t)
    templates.append(np.full(12, 1.0))  # N: flat spectrum
    templates = np.array(templates)
    templates = templates / np.linalg.norm(templates, axis=1, keepdims=True)
    templates[-1] *= N_TEMPLATE_SCALE
    return templates


FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
                 "Cb": "B", "Fb": "E"}

# mir_eval-style quality names -> guitar chart suffixes
QUALITY_SUFFIX = {"": "", "maj": "", "min": "m", "maj7": "maj7", "min7": "m7",
                  "7": "7", "maj6": "6", "min6": "m6", "sus2": "sus2",
                  "sus4": "sus4", "dim": "dim", "aug": "aug", "dim7": "dim7",
                  "hdim7": "m7b5", "minmaj7": "mMaj7"}


def normalise_label(label: str) -> str:
    """Engine labels ('A:maj', 'Bb:min7', 'N', 'X') -> chart names ('A',
    'A#m7', 'N'). X (unknown) is treated as no-chord."""
    if label in ("N", "X"):
        return "N"
    root, _, quality = label.partition(":")
    root = FLAT_TO_SHARP.get(root, root)
    return root + QUALITY_SUFFIX.get(quality, quality)


def recognise_btc(audio_path) -> list[dict]:
    """BTC bi-directional transformer (vendor/BTC-ISMIR19), 170-chord
    vocabulary. Runs the repo's test.py in a subprocess with the current
    interpreter (needs torch; uses the GPU automatically)."""
    if not (BTC_DIR / "test.py").exists():
        raise RuntimeError(f"{BTC_DIR} not found - clone jayg996/BTC-ISMIR19 there")
    print("Running BTC transformer (large vocabulary)...")
    with tempfile.TemporaryDirectory() as tmp:
        # test.py scans a directory for audio; the song dir has just audio.wav
        cp = subprocess.run(
            [sys.executable, "test.py", "--voca", "True",
             "--audio_dir", str(Path(audio_path).parent), "--save_dir", tmp],
            cwd=str(BTC_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if cp.returncode:
            tail = (cp.stderr or cp.stdout or "").strip()[-400:]
            raise RuntimeError(f"BTC failed: {tail}")
        lab = Path(tmp) / (Path(audio_path).stem + ".lab")
        segments = []
        for line in lab.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 3:
                segments.append({"chord": normalise_label(parts[2]),
                                 "start_time": round(float(parts[0]), 2),
                                 "end_time": round(float(parts[1]), 2)})
    return segments


def madmom_compat() -> None:
    """madmom 0.16.1 predates Python 3.10 and numpy 1.24: restore the
    removed collections/numpy aliases it imports. Must run before any
    `import madmom`."""
    import collections
    import collections.abc as abc
    for name in ("MutableMapping", "MutableSequence", "Iterable",
                 "Callable", "Mapping", "Sequence", "Hashable"):
        if not hasattr(collections, name):
            setattr(collections, name, getattr(abc, name))
    for name, typ in (("float", float), ("int", int), ("bool", bool),
                      ("complex", complex), ("object", object), ("str", str)):
        if not hasattr(np, name):
            setattr(np, name, typ)


def recognise_madmom(audio_path) -> list[dict]:
    """Segment-level recognition with madmom's CNN + CRF chord pipeline."""
    madmom_compat()
    from madmom.features.chords import (CNNChordFeatureProcessor,
                                        CRFChordRecognitionProcessor)
    print("Extracting CNN chord features (madmom)...")
    feats = CNNChordFeatureProcessor()(str(audio_path))
    print("CRF decoding...")
    segs = CRFChordRecognitionProcessor()(feats)
    return [{"chord": normalise_label(label),
             "start_time": round(float(start), 2),
             "end_time": round(float(end), 2)}
            for start, end, label in segs]


def transpose_chord(chord: str, semitones: int) -> str:
    """Shift a chord's root down by `semitones` (capo compensation),
    preserving any quality suffix (Em7 -> Dm7)."""
    if chord == "N" or semitones == 0:
        return chord
    m = re.match(r"^([A-G]#?)(.*)$", chord)
    if not m:
        return chord
    root, suffix = m.groups()
    idx = (NOTE_NAMES.index(root) - semitones) % 12
    return NOTE_NAMES[idx] + suffix


def recognise_librosa(audio_path, smooth: float, hop_length: int = 2048):
    """Return (labels_per_frame, frame_times)."""
    import librosa

    print("Loading audio...")
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)

    print("Separating harmonic content (drops drums/percussion)...")
    y_harm = librosa.effects.harmonic(y)

    print("Computing chroma...")
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr, hop_length=hop_length)
    chroma = chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9)

    print("Matching chord templates + Viterbi smoothing...")
    templates = chord_templates()
    scores = templates @ chroma                      # cosine similarity per frame
    scores = np.power(np.clip(scores, 1e-9, None), 4)  # sharpen
    prob = scores / scores.sum(axis=0, keepdims=True)

    n_states = len(chord_labels())
    transition = librosa.sequence.transition_loop(n_states, smooth)
    states = librosa.sequence.viterbi(prob, transition)

    times = librosa.frames_to_time(np.arange(chroma.shape[1] + 1),
                                   sr=sr, hop_length=hop_length)
    return states, times


def states_to_segments(states, times) -> list[dict]:
    """Collapse per-frame states into {chord, start_time, end_time} segments."""
    labels = chord_labels()
    segments = []
    start = 0
    for i in range(1, len(states) + 1):
        if i == len(states) or states[i] != states[start]:
            segments.append({
                "chord": labels[states[start]],
                "start_time": round(float(times[start]), 2),
                "end_time": round(float(times[i]), 2),
            })
            start = i
    return segments


def clean_segments(segments: list[dict], min_duration: float) -> list[dict]:
    """Absorb blips shorter than min_duration into the previous segment,
    then drop no-chord stretches and merge repeats."""
    cleaned = []
    for seg in segments:
        dur = seg["end_time"] - seg["start_time"]
        if cleaned and dur < min_duration:
            cleaned[-1]["end_time"] = seg["end_time"]
        elif cleaned and seg["chord"] == cleaned[-1]["chord"]:
            cleaned[-1]["end_time"] = seg["end_time"]
        else:
            cleaned.append(dict(seg))
    merged = []
    for seg in cleaned:
        if seg["chord"] == "N":
            continue
        if merged and seg["chord"] == merged[-1]["chord"]:
            merged[-1]["end_time"] = seg["end_time"]
        else:
            merged.append(seg)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("--capo", type=int, default=0,
                        help="capo fret on the recording; transposes labels down")
    parser.add_argument("--min-duration", type=float, default=0.6,
                        help="discard chord segments shorter than this (seconds)")
    parser.add_argument("--smooth", type=float, default=0.9,
                        help="Viterbi self-transition prob (librosa engine "
                             "only); higher = fewer, longer segments")
    parser.add_argument("--engine", default="auto",
                        choices=["auto", "btc", "madmom", "librosa"])
    args = parser.parse_args()

    out_dir = song_dir(args.video_id)
    audio_path = out_dir / "audio.wav"
    if not audio_path.exists():
        sys.exit(f"{audio_path} not found - run download_audio.py first.")

    ensure_ffmpeg()  # madmom shells out to ffmpeg for non-PCM audio
    engine = args.engine
    if engine == "auto":
        if (BTC_DIR / "test.py").exists():
            try:
                import torch  # noqa: F401
                engine = "btc"
            except ImportError:
                pass
        if engine == "auto":
            try:
                madmom_compat()
                import madmom  # noqa: F401
                engine = "madmom"
            except ImportError:
                engine = "librosa"
    print(f"Engine: {engine}")

    if engine == "btc":
        segments = recognise_btc(audio_path)
    elif engine == "madmom":
        segments = recognise_madmom(audio_path)
    else:
        states, times = recognise_librosa(audio_path, args.smooth)
        segments = states_to_segments(states, times)
    segments = clean_segments(segments, args.min_duration)

    if args.capo:
        for seg in segments:
            seg["chord"] = transpose_chord(seg["chord"], args.capo)
        print(f"Applied capo offset: labels transposed down {args.capo} semitone(s)")

    # One entry per line so the file is easy to hand-correct.
    out_path = out_dir / "chords.json"
    body = ",\n".join("  " + json.dumps(seg) for seg in segments)
    out_path.write_text("[\n" + body + "\n]\n", encoding="utf-8")

    # Record the capo in meta.json so the player can tell you to put one on.
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["capo"] = args.capo
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"\nSaved {len(segments)} chord segments -> {out_path}")
    print("(Wrong labels? Edit that file by hand, then just re-run merge.py.)\n")

    print("=== Chord timeline ===")
    for seg in segments:
        dur = seg["end_time"] - seg["start_time"]
        print(f"  [{fmt_time(seg['start_time'])}] {seg['chord']:<4} ({dur:4.1f}s)")

    from collections import Counter
    counts = Counter(s["chord"] for s in segments)
    print("\nChord usage:", ", ".join(f"{c}x{n}" for c, n in counts.most_common()))


if __name__ == "__main__":
    main()
