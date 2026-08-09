"""Shared helpers for the ChordFinder pipeline scripts."""
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SONGS_DIR = PROJECT_ROOT / "songs"


def ensure_ffmpeg() -> str:
    """Return the directory containing ffmpeg.exe, prepending it to PATH if needed.

    yt-dlp and whisperx both shell out to ffmpeg. If it was installed via
    winget in the current session, it may not be on PATH yet, so fall back
    to the winget install location.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)

    winget_pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    hits = sorted(winget_pkgs.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"))
    if hits:
        bin_dir = str(hits[-1].parent)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        return bin_dir

    sys.exit(
        "ffmpeg not found. Install it (e.g. `winget install Gyan.FFmpeg`) and "
        "restart your terminal so it is on PATH."
    )


def ensure_cuda_dlls() -> None:
    """Make torch's bundled cuDNN/cuBLAS DLLs findable before whisperx imports
    ctranslate2, which loads them at runtime on Windows. Without this, GPU mode
    dies with 'Could not locate cudnn_ops64_9.dll'. No-op on CPU-only installs."""
    try:
        import torch
    except ImportError:
        return
    lib_dir = Path(torch.__file__).parent / "lib"
    if lib_dir.is_dir():
        os.add_dll_directory(str(lib_dir))
        os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ["PATH"]


def song_dir(video_id: str) -> Path:
    """Working directory for one song, named after the YouTube video ID."""
    return SONGS_DIR / video_id


def fmt_time(seconds: float) -> str:
    """Format seconds as m:ss.d for human-readable previews."""
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:04.1f}"
