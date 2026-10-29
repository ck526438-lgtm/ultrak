"""Build ultrak-source.zip for a GitHub release: exactly what Launch.bat needs to run.

Binaries are excluded on purpose (ffmpeg.exe is 101 Mo, GitHub caps files at 100 Mo) and
the installer fetches them on first launch.
"""

import os
import sys
import zipfile

ENTREES = ("src", "tools", "tests", "ultrak.py", "Launch.bat", "README.md", "LICENSE",
           "pyproject.toml")
EXCLURE_DOSSIER = {"__pycache__", ".pytest_cache"}
EXCLURE_FIN = (".exe", ".mp4", ".zip", ".part.mp4")


def build(dest="ultrak-source.zip"):
    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(racine, n) for n in ENTREES):
        if not os.path.exists(path):
            raise SystemExit(f"entree manquante: {path}")
    if os.path.exists(dest):
        os.remove(dest)
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in ENTREES:
            full = os.path.join(racine, entry)
            if os.path.isdir(full):
                for root, dirs, files in os.walk(full):
                    dirs[:] = sorted(d for d in dirs if d not in EXCLURE_DOSSIER)
                    for f in sorted(files):
                        if f.endswith(EXCLURE_FIN):
                            continue
                        p = os.path.join(root, f)
                        z.write(p, os.path.relpath(p, racine).replace(os.sep, "/"))
                        n += 1
            else:
                z.write(full, entry)
                n += 1
    return dest, n, os.path.getsize(dest)


if __name__ == "__main__":
    out, count, size = build(sys.argv[1] if len(sys.argv) > 1 else "ultrak-source.zip")
    print(f"{out}: {count} fichiers, {size / 1024:.1f} Ko")
