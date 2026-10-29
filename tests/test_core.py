import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ultrak.analyze import SourceInfo  # noqa: E402
from ultrak.hardware import Hardware  # noqa: E402
from ultrak.pipeline import DENOISE, PipelinePlan  # noqa: E402
from ultrak.planner import (CEILING_H, PRESET_SPEED, honest_height, make_plan,  # noqa: E402
                            total_frames, tune_to_budget, useful_upscale_factor)

CPU = Hardware(ffmpeg="ffmpeg", ffprobe="ffprobe", logical_cpus=4, ram_total_gb=8,
               ram_free_gb=2.5, filters=frozenset(DENOISE_KEYS := ("nlmeans", "atadenoise")))
GPU = Hardware(ffmpeg="ffmpeg", logical_cpus=16, ram_total_gb=32, ram_free_gb=24,
               gpu_brands=("nvidia",), gpu_encoders=("hevc_nvenc",))


def source(**changes) -> SourceInfo:
    base = dict(path="/x/video.mp4", codec="h264", width=854, height=480, fps=25.0,
                duration=120.0, frames=3000, bit_rate=510_000, pix_fmt="yuv420p",
                has_audio=True, audio_codec="aac", file_bytes=7_000_000,
                detail_score=0.55, noise_score=0.4, motion_score=0.2)
    base.update(changes)
    return SourceInfo(**base)


class TestEvidenceLabels:
    def test_label_without_a_font_is_not_a_bare_comma(self, monkeypatch):
        # Ubuntu runners have no C:/Windows/Fonts: an empty label used to leave a
        # dangling "," in the graph and ffmpeg then rejected "No such filter: ''".
        from ultrak import report
        monkeypatch.setattr(report, "_font", lambda: "")
        assert report._label("AVANT") == ""

    def test_label_with_a_font_starts_with_a_comma(self, monkeypatch):
        from ultrak import report
        monkeypatch.setattr(report, "_font", lambda: "'font.ttf'")
        assert report._label("AVANT").startswith(",drawtext=fontfile=")


class TestSourceMetrics:
    def test_bpp_is_bits_per_pixel_per_frame(self):
        assert round(source().bpp, 3) == 0.050

    def test_missing_bitrate_falls_back_to_file_size(self):
        info = source(bit_rate=0)
        assert info.bpp > 0

    def test_low_density_source_gets_a_small_upscale_budget(self):
        assert useful_upscale_factor(source(bit_rate=80_000, detail_score=0.1)) < \
            useful_upscale_factor(source(bit_rate=4_000_000, detail_score=0.9))

    def test_factor_stays_in_a_sane_range(self):
        assert 1.5 <= useful_upscale_factor(source(bit_rate=50_000_000, detail_score=1.0)) <= 4.5


class TestHonestResolution:
    def test_sd_source_is_capped_below_4k(self):
        assert honest_height(source(), useful_upscale_factor(source())) < 2160

    def test_dense_1080_source_reaches_4k(self):
        info = source(width=1920, height=1080, bit_rate=9_000_000, detail_score=0.9)
        assert honest_height(info, useful_upscale_factor(info)) == 2160

    def test_ceiling_map_matches_modes(self):
        assert CEILING_H["4k"] == 2160 and CEILING_H["qhd"] == 1440


class TestPlanner:
    def test_auto_mode_refuses_an_empty_4k(self):
        job = make_plan(source(), CPU, mode="auto")
        assert job.plan.target_height < 2160
        assert any("4K" in d.reason or "4k" in d.reason for d in job.decisions)

    def test_forced_4k_is_kept_but_flagged(self):
        job = make_plan(source(), CPU, mode="4k")
        assert job.plan.target_height == 2160
        assert any("forcee" in d.reason for d in job.decisions)

    def test_dimensions_stay_even_and_aspect_is_kept(self):
        job = make_plan(source(width=700, height=400), CPU, mode="4k")
        assert job.plan.target_width % 2 == 0 and job.plan.target_height % 2 == 0
        assert abs(job.plan.target_width / job.plan.target_height - 700 / 400) < 0.02

    def test_cpu_only_machine_gets_libx264(self):
        assert make_plan(source(), CPU).plan.codec == "libx264"

    def test_nvenc_machine_is_offloaded_to_the_gpu(self):
        assert make_plan(source(), GPU).plan.codec == "hevc_nvenc"

    def test_grainy_small_source_gets_strong_denoise_when_ram_allows(self):
        roomy = Hardware(ffmpeg="f", logical_cpus=8, ram_free_gb=8.0)
        info = source(noise_score=0.8, bit_rate=60_000, detail_score=0.2)
        assert make_plan(info, roomy).plan.denoise == "strong"

    def test_big_source_never_gets_nlmeans_on_little_ram(self):
        info = source(height=1440, width=2560, noise_score=0.9, bit_rate=60_000)
        plan = make_plan(info, CPU).plan
        assert plan.denoise != "strong" and "nlmeans" not in plan.vf()

    def test_clean_source_is_not_smoothed_into_plastic(self):
        info = source(height=1080, width=1920, bit_rate=9_000_000, detail_score=0.9,
                      noise_score=0.05)
        assert make_plan(info, CPU).plan.denoise == "light"

    def test_budget_ladder_only_gets_faster_never_slower(self):
        job = make_plan(source(duration=600.0, frames=15000), CPU, mode="4k")
        before = job.plan.preset
        tune_to_budget(job, CPU, budget_seconds=60, allow_res_drop=False)
        assert PRESET_SPEED[job.plan.preset] >= PRESET_SPEED[before]

    def test_impossible_budget_is_reported_not_silently_dropped(self):
        job = make_plan(source(duration=3600.0, frames=90000), CPU, mode="4k")
        tune_to_budget(job, CPU, budget_seconds=30, allow_res_drop=False)
        assert any("budget" in d.reason.lower() for d in job.decisions)

    def test_total_frames_uses_the_output_fps(self):
        info = source(duration=10.0, fps=25.0, frames=250)
        job = make_plan(info, CPU, fps=60.0)
        assert total_frames(info, job.plan) == 600

    def test_output_name_contains_the_geometry(self):
        job = make_plan(source(), CPU, mode="4k")
        assert "3840x2160" in os.path.basename(job.dst) or "x2160" in os.path.basename(job.dst)


class TestPipeline:
    def plan(self, **kw):
        base = dict(target_width=3840, target_height=2160, src_width=854, src_height=480)
        base.update(kw)
        return PipelinePlan(**base)

    def test_denoise_runs_at_source_resolution(self):
        vf = self.plan(denoise="strong").vf()
        assert vf.index("nlmeans") < vf.index("scale=3840:2160") or \
            vf.index("nlmeans") < vf.index("scale=-2:960")

    def test_sharpen_is_applied_after_scaling(self):
        vf = self.plan(sharpen="pro").vf()
        assert vf.index("unsharp") > vf.index("scale")

    def test_fps_conversion_happens_before_scaling(self):
        vf = self.plan(fps=60.0).vf()
        assert vf.startswith("fps=60")

    def test_two_step_scaling_for_a_big_jump(self):
        assert self.plan(two_step=True).vf().count("scale=") == 2

    def test_single_step_for_a_small_jump(self):
        info = self.plan(src_height=1080, target_height=1440, two_step=True)
        assert info.vf().count("scale=") == 1

    def test_format_is_always_last(self):
        assert self.plan(denoise="medium", sharpen="pro", grade="punch", fps=50).vf(). \
            endswith("format=yuv420p")

    def test_bitrate_ceiling_scales_with_pixels(self):
        small = self.plan(target_width=1920, target_height=1080).quality_ceiling()
        big = self.plan().quality_ceiling()
        assert big > small * 3

    def test_encoder_args_include_the_ceiling(self):
        args = self.plan().encoder_args()
        assert "-maxrate" in args and "-bufsize" in args

    def test_audio_copy_by_default(self):
        assert self.plan().audio_args() == ["-c:a", "copy"]

    def test_build_command_shape(self):
        cmd = self.plan().build("in.mp4", "out.mp4")
        assert cmd[0] == "-y" and cmd[-1] == "out.mp4"
        assert "-vf" in cmd and "-map" in cmd and "in.mp4" in cmd

    def test_preview_window_uses_input_side_seek(self):
        cmd = self.plan().build("in.mp4", "out.mp4", start=12.5, duration=4.0)
        assert cmd[cmd.index("-ss") + 1] == "12.500"
        assert cmd.index("-ss") < cmd.index("-i")


class TestHardwareGuard:
    def test_score_grows_with_cpus_and_ram(self):
        assert Hardware(logical_cpus=16, ram_free_gb=30).cpu_score() > \
            Hardware(logical_cpus=2, ram_free_gb=1).cpu_score()

    def test_gpu_encoder_lookup_is_explicit(self):
        assert GPU.has_gpu_encoder("hevc_nvenc") and not CPU.has_gpu_encoder("hevc_nvenc")


class TestGuiWiring:
    def test_mode_labels_round_trip_to_planner_modes(self):
        """The combo shows labels and options() maps them back: a duplicate label
        or a missing key would raise StopIteration the first time a user clicks."""
        pytest.importorskip("tkinter")
        from ultrak.gui import MODES_LABEL
        from ultrak.planner import MODES
        assert sorted(MODES_LABEL) == sorted(MODES)
        assert len(set(MODES_LABEL.values())) == len(MODES_LABEL)
