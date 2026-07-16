from pathlib import Path


def calculate_hash_distance(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def extract_image_metadata(path: Path, config):
    try:
        from PIL import Image

        with Image.open(path) as im:
            if im.width * im.height > config.section("images")["max_pixels"]:
                return {"analysis_status": "ERROR", "analysis_error": "pixel limit"}
            return {
                "format": im.format,
                "width": im.width,
                "height": im.height,
                "analysis_status": "OK",
            }
    except Exception as exc:
        return {"analysis_status": "ERROR", "analysis_error": str(exc)}


def run_image_analysis(database, config):
    return None
