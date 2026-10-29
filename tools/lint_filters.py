"""Validate every filter string this project can emit, against the real ffmpeg binary.

Cheap (single frame each) and it catches the class of bug where a filter option is
out of range and the whole render fails on the user's machine.
"""

import subprocess
import sys

sys.path.insert(0, "src")
from ultrak.pipeline import DENOISE, GRADE, SHARPEN, SCALE_UP, PipelinePlan  # noqa: E402

FFMPEG = sys.argv[1] if len(sys.argv) > 1 else "tools/ffmpeg.exe"
SAMPLE_SIZES = ["512x288", "1920x1080"]


def check(label, graph, size="512x288"):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", f"testsrc=s={size}:r=25", "-vf", graph, "-frames:v", "1", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    ok = p.returncode == 0
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  {p.stderr.strip()[:150]}" if not ok else ""))
    return ok


failed = []
for category, table in (("denoise", DENOISE), ("sharpen", SHARPEN), ("grade", GRADE)):
    for name, chain in table.items():
        for step in chain:
            if not check(f"{category}/{name}: {step}", step):
                failed.append(f"{category}/{name}")

for label, graph in [
    ("fps dup", "fps=60"),
    ("minterpolate blend", "minterpolate=fps=60:mi_mode=blend"),
    ("minterpolate mci", "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:"
                         "vsbmc=1:scd=fdiff:scd_threshold=8"),
    ("two-step scale", f"scale=-2:960:flags={SCALE_UP},scale=3840:2160:flags={SCALE_UP}"),
    ("full pro chain", PipelinePlan(target_width=3840, target_height=2160, src_height=480,
                                    denoise="medium", sharpen="pro", grade="natural").vf()),
    ("full strong chain", PipelinePlan(target_width=3840, target_height=2160, src_height=480,
                                       denoise="strong", sharpen="strong", grade="punch",
                                       fps=60).vf()),
]:
    if not check(label, graph):
        failed.append(label)

print("\nlint ffmpeg:", "TOUS OK" if not failed else f"{len(failed)} echecs: {failed}")
sys.exit(1 if failed else 0)
