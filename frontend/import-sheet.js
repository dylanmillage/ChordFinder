/* Chord-sheet import, in the browser.
 *
 * A faithful port of pipeline/import_sheet.py (+ the merge step) so the
 * hosted version can import a pasted tab without the PC. Both must produce
 * the same placements - see cloud/compare_import.py for the parity check.
 */
(function () {
  const NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  const FLAT_TO_SHARP = { Db: "C#", Eb: "D#", Gb: "F#", Ab: "G#", Bb: "A#",
                          Cb: "B", Fb: "E" };
  const CHORD_RE =
    /^[A-G][#b♯♭]?(?:maj|min|m|M|dim|aug|sus|add)?[0-9]*(?:(?:sus|add|maj|dim|aug|b|#)[0-9]*)*(?:\/[A-G][#b♯♭]?)?$/;
  const IGNORE = new Set(["N.C.", "NC", "|", "-", "–", "%", "x2", "x3", "x4",
                          "(x2)", "(x3)"]);
  const NOTE_SYMBOL = "♪";
  const MERGE_GAP = 3.0;

  const norm = t => t.toLowerCase().replace(/[^a-z0-9']/g, "");

  function isChordToken(tok) {
    const t = tok.replace(/^[(,.|]+|[),.|]+$/g, "");
    if (!t || IGNORE.has(t)) return false;
    return CHORD_RE.test(t) && t.length <= 10;
  }

  function classifyLine(line) {
    if (/^\s*\[[^\]]+\]\s*$/.test(line)) return "section";
    const toks = line.split(/\s+/).filter(t => t && !IGNORE.has(t));
    if (!toks.length) return "blank";
    const chordish = toks.filter(isChordToken).length;
    return chordish >= Math.max(1, Math.round(0.75 * toks.length)) ? "chords" : "lyric";
  }

  function tokensWithCols(line) {
    const out = [];
    const re = /\S+/g;
    let m;
    while ((m = re.exec(line)) !== null) out.push([m.index, m[0]]);
    return out;
  }

  function parseSheet(text) {
    const capoM = /capo[^0-9]{0,10}(\d{1,2})/i.exec(text);
    const capo = capoM ? parseInt(capoM[1], 10) : null;
    const pairs = [];
    let pending = null, unanchored = 0;
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.replace(/\s+$/, "");
      const kind = classifyLine(line);
      if (kind === "chords") {
        if (pending) unanchored++;
        pending = tokensWithCols(line)
          .filter(([, t]) => isChordToken(t))
          .map(([c, t]) => [c, t.replace(/^[(,.|]+|[),.|]+$/g, "")]);
      } else if (kind === "lyric") {
        pairs.push({ chords: pending || [], lyric: line });
        pending = null;
      } else {
        if (pending) unanchored++;
        pending = null;
      }
    }
    if (pending) unanchored++;
    return { pairs, capo, unanchored };
  }

  /* ---- difflib-compatible sequence matching ---- */

  // Longest matching block in a[alo:ahi] vs b[blo:bhi]; ties go to the
  // earliest position, matching Python's difflib.
  function longestMatch(a, b, alo, ahi, blo, bhi) {
    let besti = alo, bestj = blo, bestsize = 0;
    let j2len = new Map();
    for (let i = alo; i < ahi; i++) {
      const newj2len = new Map();
      for (let j = blo; j < bhi; j++) {
        if (a[i] !== b[j]) continue;
        const k = (j2len.get(j - 1) || 0) + 1;
        newj2len.set(j, k);
        if (k > bestsize) { besti = i - k + 1; bestj = j - k + 1; bestsize = k; }
      }
      j2len = newj2len;
    }
    return [besti, bestj, bestsize];
  }

  function matchingBlocks(a, b) {
    const queue = [[0, a.length, 0, b.length]];
    const blocks = [];
    while (queue.length) {
      const [alo, ahi, blo, bhi] = queue.pop();
      const [i, j, k] = longestMatch(a, b, alo, ahi, blo, bhi);
      if (!k) continue;
      blocks.push([i, j, k]);
      if (alo < i && blo < j) queue.push([alo, i, blo, j]);
      if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
    }
    blocks.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
    return blocks;
  }

  function ratio(a, b) {
    if (!a.length && !b.length) return 1;
    let matches = 0;
    for (const [, , k] of matchingBlocks(a, b)) matches += k;
    return (2 * matches) / (a.length + b.length);
  }

  /* ---- chord helpers ---- */

  function splitChord(name) {
    const m = /^([A-G][#b]?)(.*)$/.exec(name || "");
    if (!m) return [null, null];
    const root = FLAT_TO_SHARP[m[1]] || m[1];
    const i = NOTES.indexOf(root);
    return i < 0 ? [null, null] : [i, m[2]];
  }

  function transpose(name, semitones) {
    if (!semitones) return name;
    const [idx, suffix] = splitChord(name);
    if (idx === null) return name;
    let suf = suffix;
    if (suf.includes("/")) {
      const [body, bass] = suf.split("/");
      const [bidx, bsuf] = splitChord(bass);
      if (bidx !== null)
        suf = `${body}/${NOTES[(bidx + semitones + 12) % 12]}${bsuf}`;
    }
    return NOTES[(((idx + semitones) % 12) + 12) % 12] + suf;
  }

  const isMinor = s => s.startsWith("m") && !s.startsWith("maj");

  function keyOffset(imported, detected) {
    const scores = new Map();
    for (const a of imported) {
      const [ai, asuf] = splitChord(a.chord);
      if (ai === null) continue;
      for (const b of detected) {
        const lo = Math.max(a.start_time, b.start_time);
        const hi = Math.min(a.end_time, b.end_time);
        if (hi <= lo) continue;
        const [bi, bsuf] = splitChord(b.chord);
        if (bi === null || isMinor(asuf) !== isMinor(bsuf)) continue;
        const d = (((bi - ai) % 12) + 12) % 12;
        scores.set(d, (scores.get(d) || 0) + (hi - lo));
      }
    }
    let total = 0, best = 0, bestOff = 0;
    for (const [k, v] of scores) { total += v; if (v > best) { best = v; bestOff = k; } }
    return total ? [bestOff, best / total] : [0, 0];
  }

  /* ---- matching + assembly ---- */

  const LOOKAHEAD = 80;

  function matchPairs(pairs, words) {
    const wnorm = words.map(w => norm(w.word));
    const events = [], covered = [], unmatched = [], previewLines = [];
    let matchedLines = 0, cursor = 0;
    let bestAttempt = { score: 0, lyric: "", near: "" };

    for (const pair of pairs) {
      const toks = tokensWithCols(pair.lyric);
      const snorm = toks.map(([, t]) => norm(t));
      if (!snorm.length) continue;
      const short = snorm.length < 3;
      const threshold = short ? 0.65 : 0.5;
      const maxStart = words.length - Math.max(2, Math.floor(snorm.length / 2));

      const scan = (lo, hi) => {
        let bestAnyS = 0, bestAnyI = -1, hitS = 0, hitI = -1, firstHit = -1;
        for (let start = lo; start < Math.max(lo + 1, hi); start++) {
          const window = wnorm.slice(start, start + snorm.length + 2);
          const sc = ratio(snorm, window);
          if (sc > bestAnyS) { bestAnyS = sc; bestAnyI = start; }
          if (sc >= threshold) {
            if (firstHit < 0) firstHit = start;
            if (sc > hitS) { hitS = sc; hitI = start; }
          }
          if (firstHit >= 0 && start > firstHit + snorm.length + 2) break;
        }
        return hitI >= 0 ? [hitS, hitI] : [bestAnyS, bestAnyI];
      };

      let [score, start] = scan(cursor, Math.min(maxStart, cursor + LOOKAHEAD));
      if (score < threshold && pair.chords.length && !short) {
        const [g, gi] = scan(Math.min(maxStart, cursor + LOOKAHEAD), maxStart);
        if (g > score) { score = g; start = gi; }
      }
      if (score > bestAttempt.score)
        bestAttempt = { score, lyric: pair.lyric.trim().slice(0, 60),
                        near: wnorm.slice(Math.max(0, start), Math.max(0, start) + 8).join(" ") };
      if (start < 0 || score < threshold) {
        if (pair.chords.length) unmatched.push(pair.lyric.trim().slice(0, 60));
        continue;
      }

      matchedLines++;
      const spanLen = Math.min(snorm.length + 2, words.length - start);
      const window = wnorm.slice(start, start + spanLen);
      const mapping = new Map();
      for (const [ai, bi, k] of matchingBlocks(snorm, window))
        for (let x = 0; x < k; x++) mapping.set(ai + x, start + bi + x);

      const placements = [];
      for (const [col, chordRaw] of pair.chords) {
        let tokIdx = 0;
        toks.forEach(([tcol], i) => { if (tcol <= col) tokIdx = i; });
        let widx = mapping.get(tokIdx);
        if (widx === undefined)
          widx = start + Math.min(spanLen - 1,
                   Math.round(tokIdx * spanLen / Math.max(1, snorm.length)));
        const chord = chordRaw.replace(/♯/g, "#").replace(/♭/g, "b");
        events.push({ t: words[widx].start_time, chord, word: words[widx].word });
        placements.push([widx - start, chord]);
      }
      if (pair.chords.length)
        previewLines.push({ words: words.slice(start, start + spanLen).map(w => w.word),
                            placements });
      covered.push([words[start].start_time - 0.5,
                    words[Math.min(start + spanLen, words.length) - 1].end_time + 0.5]);
      cursor = start + Math.max(1, spanLen - 2);
    }

    return { events, covered, report: {
      chord_lines: pairs.filter(p => p.chords.length).length,
      lyric_lines: pairs.length,
      matched_lines: matchedLines,
      chords_placed: events.length,
      unmatched: unmatched.slice(0, 8),
      best_attempt: bestAttempt,
      preview_lines: previewLines.slice(0, 50),
    } };
  }

  function eventsToSegments(events) {
    const sorted = [...events].sort((a, b) => a.t - b.t);
    const merged = [];
    for (const e of sorted) {
      const last = merged[merged.length - 1];
      if (last && e.chord === last.chord && e.t - last.t < 12) continue;
      merged.push(e);
    }
    return merged.map((e, i) => ({
      chord: e.chord,
      start_time: +e.t.toFixed(2),
      end_time: +((i + 1 < merged.length ? merged[i + 1].t : e.t + 4.0).toFixed(2)),
    }));
  }

  function buildSegments(segments, covered, detected, offset) {
    const inCovered = t => covered.some(([a, b]) => a <= t && t <= b);
    const kept = detected.filter(s => !inCovered(s.start_time))
                         .map(s => ({ ...s, chord: transpose(s.chord, -offset) }));
    return [[...segments, ...kept].sort((a, b) => a.start_time - b.start_time),
            segments.length, kept.length];
  }

  // Port of pipeline/merge.py
  function mergeWordsChords(words, chords, gap = MERGE_GAP) {
    const out = words.map((w, i) => ({ ...w, chord: null, wi: i }));
    const pseudo = [];
    for (const seg of [...chords].sort((a, b) => a.start_time - b.start_time)) {
      const target = out.find(w => w.start_time >= seg.start_time);
      if (target && target.start_time - seg.start_time <= gap) {
        target.chord = seg.chord;
        target.chord_time = seg.start_time;
      } else {
        pseudo.push({ word: NOTE_SYMBOL, start_time: seg.start_time,
                      end_time: seg.end_time, chord: seg.chord,
                      chord_time: seg.start_time, instrumental: true });
      }
    }
    const combined = [...out, ...pseudo].sort((a, b) => a.start_time - b.start_time);
    let group = -1, prevInstrumental = false;
    for (const w of combined) {
      if (w.instrumental) {
        if (!prevInstrumental) group++;
        w.line = `i${group}`;
      }
      prevInstrumental = !!w.instrumental;
    }
    return combined;
  }

  function importSheet(text, words, detectedChords) {
    const { pairs, capo, unanchored } = parseSheet(text);
    if (!pairs.some(p => p.chords.length))
      throw new Error("no chord-over-lyric lines found - paste the plain text " +
                      "of a chord sheet (chords positioned above lyrics)");
    const { events, covered, report } = matchPairs(pairs, words);
    if (!events.length) {
      const ba = report.best_attempt;
      throw new Error("none of the sheet's lyrics appear in this song's " +
        `transcript (best line similarity ${Math.round(ba.score * 100)}%). ` +
        `Closest miss: sheet "${ba.lyric}" vs transcript "${ba.near}". ` +
        "Double-check that this tab is for this exact song and version.");
    }
    const imported = eventsToSegments(events);
    let [offset, confidence] = keyOffset(imported, detectedChords);
    if (confidence < 0.45) offset = 0;
    const [segments, importedCount, keptCount] =
      buildSegments(imported, covered, detectedChords, offset);
    Object.assign(report, {
      key_offset: offset,
      key_confidence: +confidence.toFixed(2),
      sheet_capo: capo,
      unanchored_chord_lines: unanchored,
      imported_segments: importedCount,
      kept_detected: keptCount,
      total_segments: segments.length,
    });
    return { segments, report };
  }

  window.ImportSheet = { importSheet, mergeWordsChords, transpose };
})();
