"""Phase 1: download the audio track of a YouTube video.

Usage:
    python pipeline/download_audio.py <youtube-url-or-video-id>

Creates:
    songs/<video_id>/audio.wav   the extracted audio
    songs/<video_id>/meta.json   title / duration / URL, used by later phases
"""
import argparse
import json
import sys

from common import ensure_ffmpeg, song_dir


def derive_artist(info: dict) -> str:
    """Best-effort artist name: yt metadata, else 'X - Topic' channels,
    else the 'Artist - Song' title convention used by lyric channels."""
    artist = info.get("artist") or info.get("creator")
    if artist:
        return artist.split(",")[0].strip()
    uploader = info.get("uploader") or ""
    if uploader.endswith(" - Topic"):
        return uploader[: -len(" - Topic")]
    title = info.get("title") or ""
    if " - " in title:
        return title.split(" - ")[0].strip()
    return uploader or "Unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="YouTube URL or bare video ID")
    args = parser.parse_args()

    ffmpeg_dir = ensure_ffmpeg()
    import yt_dlp  # imported after ffmpeg check so the error message is clearer

    # First resolve metadata (without downloading) so we know the video ID
    # and can name the working directory after it.
    probe_opts = {"noplaylist": True, "quiet": True}
    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(args.url, download=False)
    if info.get("_type") == "playlist":  # e.g. a ytsearch: query
        info = info["entries"][0]

    video_id = info["id"]
    out_dir = song_dir(video_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / "audio.wav"

    print(f"Title:    {info.get('title')}")
    print(f"Video ID: {video_id}")
    print(f"Duration: {info.get('duration')}s")

    if audio_path.exists():
        print(f"Audio already downloaded: {audio_path}")
    else:
        dl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / "audio.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "wav"}
            ],
            "ffmpeg_location": ffmpeg_dir,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        if not audio_path.exists():
            sys.exit("Download finished but audio.wav was not created.")

    meta = {
        "video_id": video_id,
        "title": info.get("title"),
        "artist": derive_artist(info),
        "duration": info.get("duration"),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"\nSaved: {audio_path}")
    print(f"Next:  python pipeline/transcribe.py {video_id}")


if __name__ == "__main__":
    main()
