from importlib.metadata import PackageNotFoundError, version

try:  # single source of truth is pyproject [project].version
    __version__ = version("reveille")
except PackageNotFoundError:  # running from raw source, not installed
    __version__ = "0+unknown"
