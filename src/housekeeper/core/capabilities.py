from ..acceleration.capability_detection import detect_backend


def capabilities() -> dict:
    return detect_backend().capabilities()
