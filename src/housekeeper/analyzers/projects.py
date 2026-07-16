def classify_project_kind(markers: list[str]) -> str:
    return (
        "python"
        if any(x in markers for x in ("pyproject.toml", "requirements.txt"))
        else "javascript" if "package.json" in markers else "source"
    )


def run_project_analysis(database, config):
    return None
