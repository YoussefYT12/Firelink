"""
config.py
---------
A single place where the project fetches the NASA FIRMS key (MAP_KEY). The key **is not written in the code**:

  1) environment variable FIRMS_MAP_KEY (preferred), or
  2) a file named .env next to the program containing the line:   FIRMS_MAP_KEY=your_key
     (This file is in .gitignore so it won't be accidentally uploaded to GitHub. Copy .env.example and name it .env)

Free key from: https://firms.modaps.eosdis.nasa.gov/api/area/
"""

import os

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


class MissingKeyError(ValueError):
    pass


def _read_env_file(path=None) -> dict:
    values = {}
    try:
        with open(path or _ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def get_map_key(explicit=None) -> str:
    """Returns the key (explicit ← environment variable ← .env file) or raises MissingKeyError with a clear message."""
    key = (explicit or os.environ.get("FIRMS_MAP_KEY") or _read_env_file().get("FIRMS_MAP_KEY") or "").strip()
    if not key or key.lower().startswith("your_"):
        raise MissingKeyError(
            "FIRMS_MAP_KEY is not set. Get a free key at https://firms.modaps.eosdis.nasa.gov/api/area/ "
            "then either put FIRMS_MAP_KEY=<your key> in a file named .env next to run_all.py "
            "(copy .env.example), or set the FIRMS_MAP_KEY environment variable."
        )
    return key
