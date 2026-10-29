"""First-run ffmpeg bootstrap: the binaries are too big for git (101 MB each)."""

import os
import urllib.request
import zipfile

from .utils import APP_DIR, DOWNLOAD_URLS, EXE, UltraKError, ensure_app_dir, human_bytes, run


def bin_dir() -> str:
    return os.path.join(str(APP_DIR), "bin")


def installed() -> str:
    path = os.path.join(bin_dir(), "ffmpeg" + EXE)
    return path if os.path.isfile(path) else ""


def _download(url: str, target: str, log=None, quiet=False) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ultrak-upscaler"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        next_report = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if not total or done < next_report:
                continue
            message = f"  telechargement {human_bytes(done)} / {human_bytes(total)} " \
                      f"({done * 100 // total}%)"
            if log:
                log(message)
            elif not quiet:
                print(f"\r{message}", end="", flush=True)
            next_report = done + max(1 << 21, total // 50)
        if not log and not quiet:
            print()


def install(force=False, quiet=False, log=None) -> str:
    """Fetch a static ffmpeg build and drop the two executables in ~/.ultrak/bin."""
    existing = installed()
    if existing and not force:
        return existing
    ensure_app_dir()
    os.makedirs(bin_dir(), exist_ok=True)
    zip_path = os.path.join(bin_dir(), "ffmpeg-download.zip")
    errors = []
    say = log if log else (lambda text: None if quiet else print(text))
    for url in DOWNLOAD_URLS:
        try:
            say(f"telechargement de ffmpeg:\n  {url}")
            _download(url, zip_path, log=log, quiet=quiet)
            found = _extract(zip_path)
            os.remove(zip_path)
            code, out, _ = run([found, "-hide_banner", "-version"])
            say("ffmpeg installe: " + (out.splitlines()[0] if code == 0 else found))
            return found
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise UltraKError("installation de ffmpeg echouee:\n  " + "\n  ".join(errors))


def _extract(zip_path: str) -> str:
    wanted = ("ffmpeg" + EXE, "ffprobe" + EXE)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            base = os.path.basename(name)
            if base in wanted:
                dest = os.path.join(bin_dir(), base)
                with zf.open(name) as src, open(dest, "wb") as out:
                    out.write(src.read())
                os.chmod(dest, 0o755)
    result = installed()
    if not result:
        raise UltraKError("archive sans ffmpeg.exe / ffprobe.exe")
    return result
