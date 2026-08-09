# ChordFinder

Turn a YouTube link into a synced guitar practice view: the video plays, lyrics
highlight word-by-word in real time, and chord names appear above the word they
change on.

## Folder structure

```
ChordFinder/
├── pipeline/
│   ├── common.py            shared helpers (ffmpeg lookup, paths)
│   ├── download_audio.py    Phase 1: yt-dlp audio extraction
│   ├── transcribe.py        Phase 2: WhisperX word-level lyric timestamps
│   ├── chords.py            Phase 3: chord recognition (librosa)
│   └── merge.py             Phase 4: words + chords -> combined.json
├── frontend/
│   └── player.html          Phase 5: synced practice view (single file)
├── serve.py                 static server + save API for in-player editing
├── serve.bat                double-click launcher (starts serve.py + browser)
├── songs/
│   └── <video_id>/          one working dir per song
│       ├── audio.wav        extracted audio
│       ├── meta.json        title / duration / URL
│       ├── words.json       [{word, start_time, end_time, line}]
│       ├── chords.json      [{chord, start_time, end_time}] - hand-editable
│       └── combined.json    [{word, start_time, end_time, line, chord|null}]
└── README.md
```

The project lives at `C:\Users\dylan\Projects\ChordFinder` — deliberately
outside OneDrive: sync churns on the per-song wav files and its file locks can
block renames mid-pipeline.

## Setup

```powershell
# one-time setup (venvs kept outside the project folder)
python -m venv C:\Users\dylan\.venvs\chordfinder
C:\Users\dylan\.venvs\chordfinder\Scripts\python.exe -m pip install yt-dlp whisperx librosa

# GPU mode (installed on this machine): swap in the CUDA torch build
C:\Users\dylan\.venvs\chordfinder\Scripts\python.exe -m pip install --upgrade torch torchaudio torchvision --index-url https://download.pytorch.org/whl/cu126

# ffmpeg (if not already installed)
winget install Gyan.FFmpeg

# --- second venv just for madmom chord recognition (needs numpy<2) ---
# prerequisite: MSVC C++ compiler
winget install Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
python -m venv C:\Users\dylan\.venvs\chordfinder-chords
C:\Users\dylan\.venvs\chordfinder-chords\Scripts\python.exe -m pip install "numpy<2" scipy "cython==0.29.37" mido wheel setuptools
C:\Users\dylan\.venvs\chordfinder-chords\Scripts\python.exe -m pip install --no-build-isolation madmom
```

Below, `python` means the venv interpreter:

```powershell
$py = "C:\Users\dylan\.venvs\chordfinder\Scripts\python.exe"
```

## Adding songs from the player (easiest)

With serve.bat running, click **+ Add songs** in the player, paste one or
more YouTube URLs (set each capo if the recording is capo'd), and submit.
The server runs the whole pipeline (download → WhisperX → BTC → merge, a few
minutes per song); each song gets its own progress line, ending in a
"ready - click to open" link.

Songs process **one at a time** — transcription and chord recognition each
want the whole GPU, and running several at once overflows its 6 GB into
system RAM and slows everything down 10-100x. Each song shows a **progress
bar** with its current stage; queued songs show a striped bar and their
place in line ("waiting in line (#2)").

Progress comes from streaming each subprocess's output (`run_step` reads
with `read1` so carriage-return progress bars surface live) and mapping it
onto stage spans in `STAGE_SPAN` — download 0-10%, transcribe 10-70%,
chords 70-95%, merge 95-100%. Whisper itself prints no percentages, so the
bar rests at ~50% during the longest step; Demucs and yt-dlp do report real
percentages. Expect roughly 3-5 minutes per song end to end. If processing is ever interrupted (server closed mid-job), the song
shows a "⏸ … finish processing" link under the top bar on the next visit —
no need to re-add it.

The capo is remembered (`meta.json`) and shown as a **CAPO n** badge under
the video, plus "· capo n" in the song list — chord names for those songs
are the shapes you play, not sounding pitch. The badge is also the control:
songs without a capo show **+ CAPO**, and clicking it (any time) asks for
the fret and re-detects the chords as capo-relative shapes via the job
queue. Note: changing the capo replaces hand-edited chords and drops the
simplify backup for that song. The dropdown groups songs by artist and the
search box filters it as you type.

## Two pages

- **Player** (`frontend/player.html`) — the practice view: video, synced
  lyrics, chord diagrams, edit/simplify/capo/queue. Opened per song via
  `?v=<video_id>`.
- **Songs** (`frontend/songs.html`) — the library. Filter by search, artist,
  capo, or **chords the song uses** (two modes: "contains all selected", and
  "uses only selected" = songs you can play knowing just those shapes). Sort
  by artist/title/capo/length. Per-song **▶ / + Queue / + List**. Build
  **playlists** and play them in order or shuffled — "Play" hands the list to
  the player's existing queue, so songs chain with the same 5-second
  countdown. Queue and playlists live in `localStorage` (keys `cf-queue`,
  `cf-playlists`) and stay in sync between the two tabs.

The two tabs share a nav bar in the header. The `/api/songs?detail=1`
endpoint backs the library (adds each song's chord list and duration).

## Using it on an iPad / other devices

### On your own Wi-Fi (no setup)

Run **serve-lan.bat** instead of serve.bat. It prints an "On this network"
address (e.g. `http://192.168.1.42:8321/frontend/songs.html`) — open that on
the iPad. Everything works, including adding songs, because this PC does the
processing. Windows will ask to allow Python through the firewall: choose
**Private networks**. Anyone on your network can reach it while it runs.

### Anywhere, via Firebase (hosted, read-only)

The hosted copy serves the same two pages; only the data source changes, so
there is no second copy of the app to maintain. Audio always comes from
YouTube, so nothing large is ever uploaded — just the JSON (~1.5 MB for 25
songs, far inside Firebase's free Spark plan).

**Privacy:** song lyrics live in Firestore behind Google sign-in, restricted
by `cloud/firestore.rules` to the email addresses listed there. The public
Hosting files contain no song data.

One-time setup:

1. Create a project at <https://console.firebase.google.com> (free).
2. **Build > Firestore Database > Create database** (production mode).
3. **Build > Authentication > Sign-in method > Google > Enable.**
4. **Project settings > General > Your apps > Web app** — copy the config
   values into `frontend/firebase-config.js`.
5. **Project settings > Service accounts > Generate new private key** — save
   it as `cloud/serviceAccountKey.json` (this one IS secret; it is
   gitignored).
6. Check the email list in `cloud/firestore.rules`.
7. In `cloud/`, run `firebase login` then `firebase use --add` and pick the
   project.

After that, publishing is one step:

```powershell
cloud\deploy.bat
```

It uploads changed songs to Firestore, copies the app to `cloud/public`, and
deploys. Open the printed Hosting URL on the iPad and sign in. Re-run it
whenever you add or edit songs — the hosted copy is a snapshot, not live.

The hosted version hides Add / Edit / Import / Simplify (those need the
pipeline and the local JSON files). Queue, playlists, chord diagrams, search,
filters and playback all work; queue and playlists are stored per device.

## Player features

- **Chord diagrams**: a "Chords in this song" strip shows every shape used
  (click one to hear its first occurrence); hovering any chord label in the
  lyrics pops up its diagram; the live current-chord readout shows the shape
  you should be holding right now. Fingerings come from the MIT-licensed
  [chords-db](https://github.com/tombatossals/chords-db) (vendored:
  `vendor/chords-db-guitar.json`, condensed to `frontend/chord-shapes.json`).
- **Queue / autoplay**: the Queue panel lines up songs; when one ends the
  next starts after a 5-second countdown (cancellable). The queue persists
  in localStorage. Song switching happens without a page reload so autoplay
  is allowed by the browser.
- **Keyboard shortcuts**: Space = play/pause, ←/→ = seek 5 s.
- **♭ Simplify** and **✎ Edit** are described above.

UI-added songs use `--model medium --language en`; for other languages or
model sizes run the pipeline by hand (below).

## Running the pipeline by hand

```powershell
# Phase 1: download audio -> songs/<video_id>/audio.wav
& $py pipeline/download_audio.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"

# Phase 2: word timestamps -> songs/<video_id>/words.json
#   Default: Demucs vocal separation + Whisper large-v2 (GPU). This is what
#   fixes skipped lines - Whisper hears the isolated vocal, not the band.
#   --no-separate to skip separation; --model medium for a faster run.
& $py pipeline/transcribe.py XXXXXXXXXXX --language en

# Phase 3: chords -> songs/<video_id>/chords.json  (BTC engine, GPU)
#   --capo 2  if the recording is played capo'd (transposes labels to shapes)
#   --engine  auto|btc|madmom|librosa (madmom needs the chords venv python)
& $py pipeline/chords.py XXXXXXXXXXX

# Phase 4: merge -> songs/<video_id>/combined.json
#   --gap: chord changes more than this many seconds before the next word
#          render as instrumental ♪ marks instead (default 3.0)
& $py pipeline/merge.py XXXXXXXXXXX

# Phase 5: practice view - double-click serve.bat, or:
#   python serve.py 8321
# then open http://localhost:8321/frontend/player.html?v=XXXXXXXXXXX
```

Each script prints the video ID and the path of the file it wrote, plus a
preview (timestamped transcript / chord timeline / chord-sheet view) so you
can sanity-check before continuing. The player must be opened through the
local server, not file:// (browsers block the JSON fetch otherwise).

## Fixing mistakes

Recognition is not perfect by design — the workflow assumes a correction pass.

**In the player (easiest):** click **✎ Edit** (top right of the video).
While editing:

- click a word to fix its text; clear the box to **delete** the word, or
  type several words to **split** its time slot (how you add missing lyrics),
- click a chord to rename it (clear the box to delete it),
- click the faint **+** above a chordless word to add a chord there,
- Enter commits, Esc cancels.

Saves go straight to `songs/<id>/words.json` / `chords.json` (the source
files, so re-running `merge.py` never wipes them) and `combined.json` is
regenerated — refresh-proof, one-time fixes. This needs the page served by
`serve.py` (what serve.bat runs); a plain `python -m http.server` shows the
player fine but can't save.

**By hand (same effect):** edit `songs/<id>/chords.json` (one segment per
line) or the `word` fields in `words.json`, then re-run `merge.py`.

**Import a chord sheet (⇪ Import):** paste the plain text of a chord sheet
(chords positioned above lyric lines, the standard Ultimate Guitar layout).
The sheet's lyric lines are fuzzy-matched against this song's own transcript
— the app's lyrics are kept; only the chords are taken, attached to our
word timestamps. Sections the sheet can't anchor (intro/solo/outro) keep the
detected chords. Preview shows match coverage before anything is written;
the pre-import detection is backed up to `chords.detected.json` (restore =
copy it back over `chords.json` + re-run merge). Copy-paste is deliberate:
automated scraping of tab sites violates their terms, so the human does the
copying and the app does the aligning.

Tip: outside edit mode, click any word to jump the video there — fastest way
to check a suspect chord against what you hear.

Useful `transcribe.py` flags:

- `--model medium` — better lyric accuracy, slower. Default is `small`.
  Music is harder than speech for Whisper; if lyrics come out garbled, bump
  the model size before assuming timing is broken.
- `--language en` — skip auto-detection (detection can misfire on songs with
  long instrumental intros).
- `--device cpu|cuda` — defaults to `cuda` if a CUDA-enabled torch is
  installed, else CPU.

## Troubleshooting installs

### WhisperX

- **CPU vs GPU:** the default `pip install whisperx` gives CPU-only torch on
  Windows — works everywhere, does a ~4 min song in a few minutes with the
  `small` model. This machine (RTX 4050, 6 GB) has the CUDA build installed
  (see Setup), which makes `--model medium` the practical default.
  **"Could not locate cudnn_ops64_9.dll"** means ctranslate2 can't find cuDNN;
  `pipeline/common.py:ensure_cuda_dlls()` handles this by pointing the process
  at torch's bundled DLLs — if it recurs, run with `--device cpu` and check
  that `site-packages\torch\lib` still contains the `cudnn*` DLLs.
- **6 GB VRAM note:** use `--model small` or `medium` on GPU; `large-v3` with
  float16 can OOM. If it does, fall back to `--device cpu`.
- **`Failed to load audio` / ffmpeg errors:** whisperx shells out to `ffmpeg`.
  The scripts auto-locate a winget-installed ffmpeg, but a fresh terminal after
  `winget install` also fixes it.
- **Alignment step:** word-level times come from the wav2vec2 forced-alignment
  pass (`whisperx.align`), which downloads an alignment model (~360 MB) on
  first run — needs network. Plain Whisper timestamps are segment-level
  only; if `words.json` has one time per sentence, alignment didn't run.
- **Harmless warnings you'll see:** a long `torchcodec is not installed
  correctly` warning at startup (whisperx loads audio through ffmpeg instead,
  which works), and a Hugging Face symlink warning (caching still works).

### yt-dlp

- **"No supported JavaScript runtime" warning:** downloads still work via a
  fallback client, but if formats start going missing, install deno
  (`winget install DenoLand.Deno`) — yt-dlp picks it up automatically.
- yt-dlp has deprecated Python 3.10 (this venv's version); it still works but
  a future update may require rebuilding the venv on Python 3.11+.

### Chord recognition engine (Phase 3)

- **BTC (primary):** Bi-directional Transformer ([ISMIR 2019](https://github.com/jayg996/BTC-ISMIR19)),
  cloned into `vendor/BTC-ISMIR19` with its pretrained weights — **not
  committed** (35 MB of third-party model files); recreate with
  `git clone --depth 1 https://github.com/jayg996/BTC-ISMIR19.git vendor/BTC-ISMIR19`
  and re-apply the patches noted below. 170-chord
  vocabulary (Em7, Dsus4, A7sus4...), runs in the whisperx venv on the GPU
  (~10 s/song). Local patches applied for modern libs: `torch.load(...,
  weights_only=False)`, `yaml.safe_load`, removed-numpy-alias renames.
  Chosen after beating madmom on a real comparison (it catches the D
  walkdown turnarounds in "Something in the Orange" that madmom misses).
- **madmom:** CNN + CRF, major/minor only. Lives in its own venv (needs
  numpy < 2; compiled with VS Build Tools). `madmom_compat()` in chords.py
  restores `collections`/`numpy` aliases it expects — must run before
  `import madmom`. Solid fallback; `--engine madmom` to compare.
- **librosa (fallback of last resort):** chroma + templates + Viterbi.
  Major/minor confusions and sloppy boundaries, but installs anywhere.
- `--engine auto` picks BTC when `vendor/BTC-ISMIR19` + torch are available
  (whisperx venv), else madmom (chords venv), else librosa.
- **autochord:** was also evaluated; its `vamp` dependency fails to build
  even with MSVC installed. Not pursued.

### Simplify button

BTC's detailed labels (Em7, A7sus4) are accurate but the basic shapes are
easier to play and sound close. The **♭ Simplify** button in the player maps
every chord to its triad (Em7→Em, Dsus4→D, m7b5→m, dim→m); the detailed
labels are backed up to `songs/<id>/chords.full.json` and the same button
restores them. Caveat: chord edits made while simplified are lost if you
later restore.

## Design decisions

- Strumming-pattern detection: intentionally skipped (accuracy not worth it).
- `words.json` carries a `line` field (Whisper segment index) so the frontend
  can render real lyric lines instead of one wrapped paragraph.
- Chord JSON (Phase 3) will be hand-editable so you can fix recognition
  mistakes without re-running anything.
