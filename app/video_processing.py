"""Video validation and derivative generation through the system FFmpeg runtime."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class VideoProcessingError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessedVideo:
    duration_seconds: float
    width: int
    height: int
    thumbnail: bytes
    variants: dict[str, bytes]
    adaptive_files: dict[str, bytes]


def video_processor_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoProcessingError("Video processing failed") from exc


def process_video_variants(content: bytes) -> ProcessedVideo:
    if not video_processor_available():
        raise VideoProcessingError("FFmpeg runtime is not installed")
    if not content:
        raise VideoProcessingError("Video is empty")
    with tempfile.TemporaryDirectory(prefix="heymarket-video-") as temp:
        root = Path(temp)
        source = root / "source"
        source.write_bytes(content)
        probe = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(source),
            ]
        )
        try:
            metadata = json.loads(probe.stdout.decode("utf-8"))
            stream = metadata["streams"][0]
            width = int(stream["width"])
            height = int(stream["height"])
            duration = float(metadata["format"]["duration"])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VideoProcessingError("Invalid or unsupported video") from exc
        if duration <= 0 or duration > 600:
            raise VideoProcessingError("Video duration must be between 1 second and 10 minutes")
        if width < 160 or height < 120 or width > 3840 or height > 3840:
            raise VideoProcessingError("Unsupported video resolution")
        thumbnail_path = root / "thumbnail.jpg"
        _run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(min(1.0, duration / 2)),
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(640,iw)':-2",
                "-q:v",
                "3",
                str(thumbnail_path),
            ]
        )
        variants: dict[str, bytes] = {}
        adaptive_files: dict[str, bytes] = {}
        renditions: list[tuple[str, int, str, int]] = []
        for name, max_height, bitrate, bandwidth in (
            ("preview", 480, "900k", 1_100_000),
            ("standard", 720, "1800k", 2_100_000),
            ("high", 1080, "3500k", 3_900_000),
        ):
            if name == "high" and height < 900:
                continue
            target = root / f"{name}.mp4"
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source),
                    "-vf",
                    f"scale=-2:'min({max_height},ih)'",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-b:v",
                    bitrate,
                    "-movflags",
                    "+faststart",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    str(target),
                ]
            )
            variants[name] = target.read_bytes()
            rendition_height = min(max_height, height)
            rendition_width = max(2, int(width * rendition_height / height) // 2 * 2)
            renditions.append((name, rendition_height, bitrate, bandwidth))

            # Generate HLS using MPEG-TS segments. The caller stores this whole
            # directory together, preserving the relative paths used below.
            playlist = root / f"{name}.m3u8"
            segment_pattern = root / f"{name}_%03d.ts"
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source),
                    "-vf",
                    f"scale=-2:'min({max_height},ih)'",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-b:v",
                    bitrate,
                    "-maxrate",
                    bitrate,
                    "-bufsize",
                    str(int(bitrate[:-1]) * 2) + "k",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-hls_time",
                    "4",
                    "-hls_playlist_type",
                    "vod",
                    "-hls_segment_filename",
                    str(segment_pattern),
                    str(playlist),
                ]
            )
            adaptive_files[playlist.name] = playlist.read_bytes()
            for segment in sorted(root.glob(f"{name}_*.ts")):
                adaptive_files[segment.name] = segment.read_bytes()

        master_lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for name, rendition_height, _bitrate, bandwidth in renditions:
            rendition_width = max(2, int(width * rendition_height / height) // 2 * 2)
            master_lines.extend(
                [
                    f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={rendition_width}x{rendition_height}",
                    f"{name}.m3u8",
                ]
            )
        adaptive_files["master.m3u8"] = ("\n".join(master_lines) + "\n").encode("utf-8")
        return ProcessedVideo(
            duration_seconds=duration,
            width=width,
            height=height,
            thumbnail=thumbnail_path.read_bytes(),
            variants=variants,
            adaptive_files=adaptive_files,
        )
