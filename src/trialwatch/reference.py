"""Reference data files (GeoNames for places, MeSH for conditions; ADR-008): downloading one as served,
and its sha256, which each load records (core.reference_load) with where the file came from."""

import hashlib
from pathlib import Path

import httpx


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, path: Path, timeout: float, follow_redirects: bool = True) -> bool:
    """Download to `path`; False when the server has no such file: 404, or, without following redirects,
    any answer but 200 (NLM answers a file it doesn't have with a redirect to an error page)."""
    with httpx.stream("GET", url, timeout=httpx.Timeout(timeout), follow_redirects=follow_redirects) as response:
        if response.status_code == 404:
            return False
        if not follow_redirects and response.status_code != 200:
            return False
        response.raise_for_status()
        with path.open("wb") as file:
            # the bytes as served: a .gz file may come with Content-Encoding gzip, which must not be undone
            for chunk in response.iter_raw():
                file.write(chunk)
    return True
