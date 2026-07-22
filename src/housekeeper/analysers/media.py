from pathlib import Path
import json
import subprocess


def extract_audio_metadata(path: Path, config):
    try:
        from mutagen import File  # type: ignore[import-not-found]

        media = File(path, easy=True)
        if media is None:
            return {"analysis_status": "UNSUPPORTED"}
        info = getattr(media, "info", None)
        return {
            "analysis_status": "OK",
            "format": type(media).__name__,
            "duration_seconds": getattr(info, "length", None),
            "bitrate": getattr(info, "bitrate", None),
            "tags": {
                key: value[:5] if isinstance(value, list) else str(value)[:500]
                for key, value in (media.tags or {}).items()
            },
        }
    except ImportError:
        return {
            "analysis_status": "UNSUPPORTED",
            "analysis_error": "optional mutagen parser unavailable",
        }
    except (OSError, ValueError, RuntimeError) as exc:
        return {"analysis_status": "ERROR", "analysis_error": str(exc)}


def extract_basic_media_metadata(path: Path, config):
    if path.suffix.lower() in {".mp3", ".wav", ".flac", ".m4a", ".ogg"}:
        return extract_audio_metadata(path, config)
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            text=True,
            capture_output=True,
            timeout=int(config.section("performance")["parser_timeout_seconds"]),
            check=False,
        )
        if result.returncode != 0:
            return {
                "analysis_status": "ERROR",
                "analysis_error": result.stderr[:500] or "ffprobe failed",
            }
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        return {
            "analysis_status": "OK",
            "format": payload.get("format", {}).get("format_name"),
            "duration_seconds": payload.get("format", {}).get("duration"),
            "bitrate": payload.get("format", {}).get("bit_rate"),
            "stream_count": len(streams),
            "video": next(
                (
                    {
                        "codec": stream.get("codec_name"),
                        "width": stream.get("width"),
                        "height": stream.get("height"),
                        "frame_rate": stream.get("r_frame_rate"),
                    }
                    for stream in streams
                    if stream.get("codec_type") == "video"
                ),
                None,
            ),
            "audio_streams": sum(1 for stream in streams if stream.get("codec_type") == "audio"),
        }
    except FileNotFoundError:
        return {"analysis_status": "UNSUPPORTED", "analysis_error": "ffprobe is not installed"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"analysis_status": "ERROR", "analysis_error": str(exc)}


def run_media_analysis(database, config, scope=None, job_id=None):
    from .registry import run_content_analysis

    return run_content_analysis(database, config, "media", job_id=job_id)
