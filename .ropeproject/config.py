def set_prefs(prefs):
    prefs["source_folders"] = ["src", "tests", "scripts", "docker"]
    prefs["python_path"] = ["src"]
    prefs.add(
        "ignored_resources",
        "*.pyc *~ .ropeproject .git __pycache__ "
        ".pytest_cache .mypy_cache .ruff_cache .venv build dist",
    )
