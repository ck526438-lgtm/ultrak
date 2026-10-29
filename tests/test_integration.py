"""End-to-end tests against a real ffmpeg binary. Skipped when ffmpeg is absent."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ultrak.analyze import measure_content, probe  # noqa: E402
from ultrak.batch import analyze as analyze_source  # noqa: E402
from ultrak.engine import render, verify  # noqa: E402
from ultrak.hardware import Hardware, detect, find_ffmpeg  # noqa: E402
from ultrak.planner import make_plan  # noqa: E402
from ultrak.report import fidelity, pair, poster, zoom  # noqa: E402
from ultrak.utils import UltraKError, run  # noqa: E402

try:
    FFMPEG = find_ffmpeg(probe=lambda p: True)
except UltraKError:  # pragma: no cover - environment dependent
    FFMPEG = ""

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg introuvable")


@pytest.fixture(scope="module")
def hw():
    return detect(FFMPEG, use_cache=False)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    folder = tmp_path_factory.mktemp("ultrak")
    path = str(folder / "source.mp4")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", "testsrc2=s=640x360:d=2:r=25", "-vf", "noise=alls=15:allf=t+u",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
           "-pix_fmt", "yuv420p", path]
    code, _, err = run(cmd, timeout=180)
    assert code == 0, err
    return path


def test_probe_reads_geometry_and_density(hw, clip):
    info = probe(clip, hw.ffprobe)
    assert (info.width, info.height) == (640, 360)
    assert info.frames == 50 and abs(info.duration - 2.0) < 0.1
    assert 0 < info.bpp < 1
    assert not info.has_audio


def test_content_measurement_separates_grain_from_structure(hw, clip):
    info = measure_content(clip, probe(clip, hw.ffprobe), hw.ffmpeg, passes=1)
    assert 0 <= info.detail_score <= 1 and 0 <= info.noise_score <= 1
    assert info.hf_native >= info.hf_struct


def test_auto_plan_of_a_low_density_source_is_not_4k(hw, clip):
    job = make_plan(analyze_source(clip, hw.ffmpeg, hw, content=True), hw, mode="auto")
    assert job.plan.target_height < 2160


def test_render_produces_a_valid_larger_file(hw, clip, tmp_path):
    src = analyze_source(clip, hw.ffmpeg, hw, content=False)
    job = make_plan(src, hw, mode="1080", preset="veryfast", denoise="light", audio="none")
    dst = str(tmp_path / "out.mp4")
    result = render(hw.ffmpeg, job.plan, clip, dst, hw, expect_frames=src.frames)
    assert result.ok, result.error
    assert os.path.getsize(dst) > 5_000
    report = verify(dst, hw, job.plan, src)
    assert "1920x1080" in report and "OK" in report
    assert not os.path.exists(dst + ".ultrak-part.mp4")


def test_preview_is_faster_than_the_full_render_and_measurable(hw, clip, tmp_path):
    src = analyze_source(clip, hw.ffmpeg, hw, content=False)
    job = make_plan(src, hw, mode="qhd", preset="veryfast", denoise="none", audio="none")
    out = str(tmp_path / "slice.mp4")
    result = render(hw.ffmpeg, job.plan, clip, out, hw, start=0.2, duration=0.4)
    assert result.ok and 0 < result.frames <= 12


def test_comparison_images_are_generated(hw, clip, tmp_path):
    src = analyze_source(clip, hw.ffmpeg, hw, content=False)
    job = make_plan(src, hw, mode="1080", preset="veryfast", denoise="medium", audio="none")
    dst = str(tmp_path / "out.mp4")
    assert render(hw.ffmpeg, job.plan, clip, dst, hw).ok
    for maker, name in ((pair, "pair.png"), (zoom, "zoom.png"), (poster, "poster.png")):
        path = maker(hw, clip, dst, str(tmp_path / name), at=0.4)
        assert os.path.getsize(path) > 3_000, name


def test_fidelity_metrics_come_back_numeric(hw, clip, tmp_path):
    src = analyze_source(clip, hw.ffmpeg, hw, content=False)
    job = make_plan(src, hw, mode="1080", preset="veryfast", denoise="light", audio="none")
    dst = str(tmp_path / "out.mp4")
    assert render(hw.ffmpeg, job.plan, clip, dst, hw).ok
    metrics = fidelity(hw, clip, dst, seconds=1.0)
    assert metrics.get("ssim") and 15 < metrics["psnr_y_db"] < 70


def test_cancelled_render_leaves_no_partial_file(hw, clip, tmp_path):
    import threading
    src = analyze_source(clip, hw.ffmpeg, hw, content=False)
    job = make_plan(src, hw, mode="1080", preset="slow", audio="none")
    dst = str(tmp_path / "out.mp4")
    cancel = threading.Event()
    cancel.set()
    result = render(hw.ffmpeg, job.plan, clip, dst, hw, cancel=cancel)
    assert not result.ok
    assert not os.path.exists(dst) and not os.path.exists(dst + ".ultrak-part.mp4")
