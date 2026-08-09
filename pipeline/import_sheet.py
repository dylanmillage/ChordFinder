"""Align a pasted chord sheet (chords positioned above lyric lines) to the
transcript's word timestamps.

The sheet's LYRICS are only used for matching - the app keeps its own
transcribed lyrics. Each chord is attached to the timestamp of the
transcript word it sits above, via fuzzy line matching. Sections the sheet
can't anchor (intros, solos) keep the app's detected chords.
"""
import collections
import difflib
import re

# Root + common quality spellings + optional slash bass. Deliberately strict:
# a lyric word must not pass ("Am" passes by design - context filters it).
CHORD_RE = re.compile(
    r"^[A-G][#b♯♭]?"
    r"(?:maj|min|m|M|dim|aug|sus|add)?[0-9]*"
    r"(?:(?:sus|add|maj|dim|aug|b|#)[0-9]*)*"
    r"(?:/[A-G][#b♯♭]?)?$")
IGNORE_TOKENS = {"N.C.", "NC", "|", "-", "–", "%", "x2", "x3", "x4", "(x2)", "(x3)"}
SECTION_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")
CAPO_RE = re.compile(r"capo[^0-9]{0,10}(\d{1,2})", re.IGNORECASE)


NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
                 "Cb": "B", "Fb": "E"}


def split_chord(name: str):
    """-> (root index 0-11, suffix) or (None, None) for N/unparseable."""
    m = re.match(r"^([A-G][#b]?)(.*)$", name or "")
    if not m:
        return None, None
    root = FLAT_TO_SHARP.get(m.group(1), m.group(1))
    if root not in NOTES:
        return None, None
    return NOTES.index(root), m.group(2)


def transpose(name: str, semitones: int) -> str:
    """Shift a chord (and any slash bass) by semitones."""
    if not semitones:
        return name
    idx, suffix = split_chord(name)
    if idx is None:
        return name
    if "/" in suffix:
        body, _, bass = suffix.partition("/")
        bidx, bsuf = split_chord(bass)
        if bidx is not None:
            suffix = f"{body}/{NOTES[(bidx + semitones) % 12]}{bsuf}"
    return NOTES[(idx + semitones) % 12] + suffix


def is_minor(suffix: str) -> bool:
    return suffix.startswith("m") and not suffix.startswith("maj")


def key_offset(imported, detected):
    """How many semitones the RECORDING sounds above the SHEET.

    A capo'd recording is detected at sounding pitch while the sheet writes
    the shapes you fret, so the two disagree by a constant. Votes are
    weighted by how long the two segments overlap (a chord held for 4s says
    more than a passing one) and only compare like qualities, which shrugs
    off the timing jitter of chord-above-word placement.
    -> (offset, confidence 0-1)"""
    scores = collections.Counter()
    for a in imported:
        ai, asuf = split_chord(a["chord"])
        if ai is None:
            continue
        for b in detected:
            lo = max(a["start_time"], b["start_time"])
            hi = min(a["end_time"], b["end_time"])
            if hi <= lo:
                continue
            bi, bsuf = split_chord(b["chord"])
            if bi is None or is_minor(asuf) != is_minor(bsuf):
                continue
            scores[(bi - ai) % 12] += hi - lo
    total = sum(scores.values())
    if not total:
        return 0, 0.0
    off, best = scores.most_common(1)[0]
    return off, best / total


def normalise_token(tok: str) -> str:
    return re.sub(r"[^a-z0-9']", "", tok.lower())


def is_chord_token(tok: str) -> bool:
    t = tok.strip("(),.|")
    if not t or t in IGNORE_TOKENS:
        return False
    return bool(CHORD_RE.match(t)) and len(t) <= 10


def classify_line(line: str) -> str:
    if SECTION_RE.match(line):
        return "section"
    toks = [t for t in line.split() if t not in IGNORE_TOKENS]
    if not toks:
        return "blank"
    chordish = sum(1 for t in toks if is_chord_token(t))
    # A chord line is nearly all chord tokens; lyric lines with words like
    # "A" or "Am" don't qualify because their other words fail the regex.
    return "chords" if chordish >= max(1, round(0.75 * len(toks))) else "lyric"


def parse_sheet(text: str):
    """-> (pairs, capo, unanchored_chord_lines)
    pairs: [{chords: [(col, chord)], lyric: str}] in sheet order."""
    capo_m = CAPO_RE.search(text)
    capo = int(capo_m.group(1)) if capo_m else None
    pairs, pending, unanchored = [], None, 0
    for raw in text.splitlines():
        line = raw.rstrip()
        kind = classify_line(line)
        if kind == "chords":
            if pending:
                unanchored += 1
            pending = [(m.start(), m.group().strip("(),.|"))
                       for m in re.finditer(r"\S+", line)
                       if is_chord_token(m.group())]
        elif kind == "lyric":
            pairs.append({"chords": pending or [], "lyric": line})
            pending = None
        elif kind in ("section", "blank"):
            if pending:
                unanchored += 1
            pending = None
    if pending:
        unanchored += 1
    return pairs, capo, unanchored


def match_pairs(pairs, words):
    """Fuzzy-align each sheet lyric line to the transcript (monotonic), and
    place that line's chords on transcript word timestamps.
    -> (events, covered_spans, report)"""
    wnorm = [normalise_token(w["word"]) for w in words]
    events, covered, unmatched = [], [], []
    matched_lines = 0
    cursor = 0
    best_attempt = {"score": 0.0, "lyric": "", "near": ""}
    preview_lines = []

    LOOKAHEAD = 80   # words; verses repeat in songs, so prefer the nearest
    for pair in pairs:
        toks = [(m.start(), m.group()) for m in re.finditer(r"\S+", pair["lyric"])]
        snorm = [normalise_token(t) for _, t in toks]
        if not snorm:
            continue
        short = len(snorm) < 3
        threshold = 0.65 if short else 0.5
        max_start = len(words) - max(2, len(snorm) // 2)

        def scan(lo, hi):
            """Peak of the FIRST region scoring >= threshold (choruses repeat;
            the nearest occurrence is the right one, not the best-scoring one
            later in the song). Falls back to the best miss for diagnostics."""
            best_any_s, best_any_i = 0.0, -1
            hit_s, hit_i, first_hit = 0.0, -1, -1
            for start in range(lo, max(lo + 1, hi)):
                window = wnorm[start:start + len(snorm) + 2]
                score = difflib.SequenceMatcher(None, snorm, window).ratio()
                if score > best_any_s:
                    best_any_s, best_any_i = score, start
                if score >= threshold:
                    if first_hit < 0:
                        first_hit = start
                    if score > hit_s:
                        hit_s, hit_i = score, start
                if first_hit >= 0 and start > first_hit + len(snorm) + 2:
                    break   # past the first acceptable region: stop before repeats
            return (hit_s, hit_i) if hit_i >= 0 else (best_any_s, best_any_i)

        # Local window first: the right occurrence of a repeated line is the
        # next one, not a higher-scoring copy in a later verse/chorus.
        best_score, best_start = scan(cursor, min(max_start, cursor + LOOKAHEAD))
        if best_score < threshold and pair["chords"] and not short:
            # only chord-bearing full lines may jump ahead (e.g. the sheet
            # includes a verse the transcript timeline reaches much later)
            g_score, g_start = scan(min(max_start, cursor + LOOKAHEAD), max_start)
            if g_score > best_score:
                best_score, best_start = g_score, g_start
        if pair["chords"] and best_score > best_attempt["score"]:
            near = " ".join(w["word"] for w in
                            words[best_start:best_start + len(snorm)]) if best_start >= 0 else ""
            best_attempt = {"score": best_score,
                            "lyric": pair["lyric"].strip()[:60], "near": near[:60]}
        if best_start < 0 or best_score < threshold:
            if pair["chords"]:   # chordless chatter lines fail silently
                unmatched.append(pair["lyric"].strip()[:60])
            continue
        matched_lines += 1
        span_len = min(len(snorm) + 2, len(words) - best_start)
        window = wnorm[best_start:best_start + span_len]
        # sheet-token index -> transcript word index, via matching blocks
        mapping = {}
        sm = difflib.SequenceMatcher(None, snorm, window)
        for block in sm.get_matching_blocks():
            for k in range(block.size):
                mapping[block.a + k] = best_start + block.b + k
        line_placements = []
        for col, chord in pair["chords"]:
            tok_idx = 0
            for i, (tcol, _) in enumerate(toks):
                if tcol <= col:
                    tok_idx = i
            widx = mapping.get(tok_idx)
            if widx is None:  # proportional fallback within the span
                widx = best_start + min(span_len - 1,
                                        round(tok_idx * span_len / max(1, len(snorm))))
            chord = chord.replace("♯", "#").replace("♭", "b")
            events.append({"t": words[widx]["start_time"], "chord": chord,
                           "word": words[widx]["word"]})
            line_placements.append([widx - best_start, chord])
        if pair["chords"]:
            preview_lines.append({
                "words": [w["word"] for w in words[best_start:best_start + span_len]],
                "placements": line_placements,   # [word index in line, chord]
            })
        covered.append((words[best_start]["start_time"] - 0.5,
                        words[min(best_start + span_len, len(words)) - 1]["end_time"] + 0.5))
        cursor = best_start + max(1, span_len - 2)

    report = {
        "chord_lines": sum(1 for p in pairs if p["chords"]),
        "lyric_lines": len(pairs),
        "matched_lines": matched_lines,
        "chords_placed": len(events),
        "unmatched": unmatched[:8],
        "best_attempt": best_attempt,
        "preview_lines": preview_lines[:50],
    }
    return events, covered, report


def events_to_segments(events):
    """Collapse chord placements into {chord, start_time, end_time} spans."""
    events = sorted(events, key=lambda e: e["t"])
    merged_events = []
    for e in events:
        if merged_events and e["chord"] == merged_events[-1]["chord"] \
                and e["t"] - merged_events[-1]["t"] < 12:
            continue  # same chord restated shortly after: keep one change
        merged_events.append(e)
    segments = []
    for i, e in enumerate(merged_events):
        end = merged_events[i + 1]["t"] if i + 1 < len(merged_events) else e["t"] + 4.0
        segments.append({"chord": e["chord"],
                         "start_time": round(e["t"], 2),
                         "end_time": round(end, 2)})
    return segments


def build_segments(segments, covered, detected, offset=0):
    """Imported chords own the covered (sung) spans; the app's detected
    chords are kept everywhere else (intro/solo/outro), transposed by
    `offset` so the whole chart stays in the sheet's key."""
    def in_covered(t):
        return any(a <= t <= b for a, b in covered)

    kept = [{**s, "chord": transpose(s["chord"], -offset)}
            for s in detected if not in_covered(s["start_time"])]
    combined = sorted(segments + kept, key=lambda s: s["start_time"])
    return combined, len(segments), len(kept)


def import_sheet(text: str, words: list, detected_chords: list):
    """-> (segments, report). Raises ValueError when nothing usable."""
    pairs, capo, unanchored = parse_sheet(text)
    if not any(p["chords"] for p in pairs):
        raise ValueError("no chord-over-lyric lines found - paste the plain "
                         "text of a chord sheet (chords positioned above lyrics)")
    events, covered, report = match_pairs(pairs, words)
    if not events:
        ba = report["best_attempt"]
        raise ValueError(
            "none of the sheet's lyrics appear in this song's transcript "
            f"(best line similarity {ba['score']:.0%}). Closest miss: sheet "
            f"\"{ba['lyric']}\" vs transcript \"{ba['near']}\". Double-check "
            "that this tab is for this exact song and version.")
    # A capo'd recording is DETECTED at sounding pitch but WRITTEN as shapes
    # in the sheet. Without correcting for it the intro/solo sections (which
    # keep detected chords) would sit a few semitones off the verses.
    imported = events_to_segments(events)
    offset, confidence = key_offset(imported, detected_chords)
    if confidence < 0.45:
        offset = 0
    segments, imported_count, kept_count = build_segments(imported, covered,
                                                          detected_chords, offset)
    report["key_offset"] = offset
    report["key_confidence"] = round(confidence, 2)
    report["sheet_capo"] = capo
    report["unanchored_chord_lines"] = unanchored
    report["imported_segments"] = imported_count
    report["kept_detected"] = kept_count
    report["total_segments"] = len(segments)
    report["sample"] = [{"chord": e["chord"], "word": e["word"],
                         "time": round(e["t"], 1)} for e in events[:10]]
    return segments, report
