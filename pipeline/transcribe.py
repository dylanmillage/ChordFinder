"""Phase 2: word-level lyric timestamps via WhisperX.

Runs Whisper transcription, then the wav2vec2 forced-alignment pass that
gives per-word start/end times (plain Whisper only gives segment times).

Usage:
    python pipeline/transcribe.py <video_id> [--model small] [--language en]
                                  [--device cpu|cuda] [--batch-size 8]

Creates:
    songs/<video_id>/words.json  list of {word, start_time, end_time, line}

`line` is the Whisper segment index — the frontend uses it later to break
lyrics into lines instead of one giant paragraph.
"""
import argparse
import json
import sys

from common import ensure_cuda_dlls, ensure_ffmpeg, fmt_time, song_dir


def fill_missing_times(words: list[dict]) -> None:
    """Alignment occasionally fails on a token (numbers, odd spellings) and
    returns it without timestamps. Fill those from the neighbouring words so
    every word is highlightable."""
    for i, w in enumerate(words):
        if w["start_time"] is None:
            prev_end = words[i - 1]["end_time"] if i > 0 else 0.0
            next_start = None
            for later in words[i + 1:]:
                if later["start_time"] is not None:
                    next_start = later["start_time"]
                    break
            w["start_time"] = prev_end
            w["end_time"] = next_start if next_start is not None else prev_end + 0.5


def separate_vocals(audio_path) -> "Path | None":
    """Run Demucs to isolate the vocal stem; Whisper transcribes music far
    more reliably without the band. Stems are written to a temp folder in
    the song dir and the caller should delete it after use. Returns the
    vocals wav, or None if separation failed (caller falls back to the mix)."""
    import shutil
    import subprocess
    sep_dir = audio_path.parent / "stems_tmp"
    shutil.rmtree(sep_dir, ignore_errors=True)
    cp = subprocess.run(
        [sys.executable, "-m", "demucs", "-n", "htdemucs",
         "--two-stems", "vocals", "-o", str(sep_dir), str(audio_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    vocals = sep_dir / "htdemucs" / audio_path.stem / "vocals.wav"
    if cp.returncode or not vocals.exists():
        tail = (cp.stderr or cp.stdout or "").strip().splitlines()
        print(f"Vocal separation failed ({tail[-1] if tail else 'unknown'}); "
              "transcribing the full mix instead.")
        shutil.rmtree(sep_dir, ignore_errors=True)
        return None
    return vocals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", help="YouTube video ID (songs/<id>/ must exist)")
    parser.add_argument("--model", default="large-v2",
                        help="Whisper model size: tiny/base/small/medium/large-v2/large-v3")
    parser.add_argument("--language", default=None,
                        help="Force language code (e.g. en). Default: auto-detect")
    parser.add_argument("--device", default=None, choices=["cpu", "cuda"],
                        help="Default: cuda if available, else cpu")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--no-separate", action="store_true",
                        help="skip Demucs vocal separation (default: separate)")
    args = parser.parse_args()

    out_dir = song_dir(args.video_id)
    audio_path = out_dir / "audio.wav"
    if not audio_path.exists():
        sys.exit(f"{audio_path} not found - run download_audio.py first.")

    ensure_ffmpeg()  # whisperx.load_audio shells out to ffmpeg
    ensure_cuda_dlls()  # must run before whisperx imports ctranslate2
    import shutil
    import torch
    import whisperx

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    # large models in float16 can OOM a 6 GB card; int8_float16 halves the
    # weights at nearly identical quality.
    if device == "cuda":
        compute_type = "int8_float16" if args.model.startswith("large") else "float16"
    else:
        compute_type = "int8"
    print(f"Device: {device} ({compute_type}), model: {args.model}")

    transcribe_source = audio_path
    if not args.no_separate:
        print("Separating vocals (Demucs)...")
        vocals = separate_vocals(audio_path)
        if vocals is not None:
            transcribe_source = vocals

    print("Loading audio...")
    audio = whisperx.load_audio(str(transcribe_source))

    print("Transcribing...")
    model = whisperx.load_model(args.model, device,
                                compute_type=compute_type, language=args.language)
    result = model.transcribe(audio, batch_size=args.batch_size)
    language = result["language"]
    print(f"Language: {language}, segments: {len(result['segments'])}")

    print("Aligning (wav2vec2 forced alignment)...")
    align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
    aligned = whisperx.align(result["segments"], align_model, metadata,
                             audio, device, return_char_alignments=False)
    shutil.rmtree(out_dir / "stems_tmp", ignore_errors=True)  # ~100 MB, transient

    words = []
    for line_idx, seg in enumerate(aligned["segments"]):
        for w in seg.get("words", []):
            words.append({
                "word": w["word"].strip(),
                "start_time": round(w["start"], 3) if "start" in w else None,
                "end_time": round(w["end"], 3) if "end" in w else None,
                "line": line_idx,
            })
    fill_missing_times(words)

    out_path = out_dir / "words.json"
    out_path.write_text(json.dumps(words, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(words)} words -> {out_path}\n")

    # Human-readable preview for sanity-checking timing against the song.
    print("=== Transcript (line start times) ===")
    current_line = None
    for w in words:
        if w["line"] != current_line:
            current_line = w["line"]
            print(f"\n[{fmt_time(w['start_time'])}] ", end="")
        print(w["word"], end=" ")
    print("\n\n=== First 15 words (exact timings) ===")
    for w in words[:15]:
        print(f"  {w['start_time']:7.2f} - {w['end_time']:7.2f}  {w['word']}")


if __name__ == "__main__":
    main()
