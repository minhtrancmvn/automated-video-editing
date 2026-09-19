"""Compile validated edit plans into safe FFmpeg argument vectors."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from video_editor.models.edit_plan import (
    EditPlan,
    OutputSpec,
    TimelineClip,
    Transition,
    timeline_duration,
)


@dataclass(frozen=True, slots=True)
class RenderWarning:
    """Structured warning emitted when render capability falls back."""

    code: str
    message: str
    reason: str
    requested_encoder: str
    selected_encoder: str


@dataclass(frozen=True, slots=True)
class RenderCommand:
    """All data needed to execute and finalize one render."""

    args: tuple[str, ...]
    partial_path: Path
    final_path: Path
    expected_duration: Decimal
    output: OutputSpec | None = None
    warnings: tuple[RenderWarning, ...] = ()


def _number(value: Decimal) -> str:
    return format(value, "f")


def _transition(plan: EditPlan, index: int) -> Transition | None:
    return next(
        (
            item
            for item in plan.transitions
            if item.from_clip == index and item.to_clip == index + 1
        ),
        None,
    )


def _atempo(speed: Decimal) -> str:
    """Build atempo chain valid for FFmpeg's 0.5..2.0 filter range."""
    value = float(speed)
    factors: list[float] = []
    while value > 2.0:
        factors.append(2.0)
        value /= 2.0
    while value < 0.5:
        factors.append(0.5)
        value /= 0.5
    factors.append(value)
    return ",".join(f"atempo={factor:.12g}" for factor in factors)


def _safe_color(value: str | None) -> str:
    if value is None:
        return "black"
    if re.fullmatch(
        r"(?:black|white|gray|grey|red|green|blue|yellow|cyan|magenta)", value
    ):
        return value
    if re.fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value):
        return value
    return "black"


def _video_framing(
    clip: TimelineClip,
    width: int,
    height: int,
    frame_rate: Decimal,
    source_index: int,
    index: int,
) -> tuple[str, list[str]]:
    label = f"vclip{index}"
    common = [
        f"[{source_index}:v]trim=start={_number(clip.source_start)}:end={_number(clip.source_end)}",
        "setpts=PTS-STARTPTS",
        f"setpts=PTS/{_number(clip.speed)}",
        f"fps={_number(frame_rate)}",
        "settb=AVTB",
    ]
    if clip.framing.mode == "center_crop":
        return label, [
            ",".join(
                common
                + [
                    f"scale={width}:{height}:force_original_aspect_ratio=increase",
                    f"crop={width}:{height}",
                    "format=yuv420p",
                    f"[{label}]",
                ]
            )
        ]

    background = _safe_color(clip.framing.background)
    split = f"vsplit{index}"
    bg = f"vbg{index}"
    fg = f"vfg{index}"
    fg_scaled = f"vfgscaled{index}"
    return label, [
        ",".join(common + [f"split=2[{split}][{fg}]"]),
        (
            f"[{split}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2,format=yuv420p,settb=AVTB[{bg}]"
        ),
        (
            f"[{fg}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background},"
            f"format=yuv420p,settb=AVTB[{fg_scaled}]"
        ),
        f"[{bg}][{fg_scaled}]overlay=(W-w)/2:(H-h)/2,format=yuv420p,settb=AVTB[{label}]",
    ]


def _audio_filter(
    source_index: int,
    clip: TimelineClip,
    duration: Decimal,
    label: str,
    use_source: bool,
) -> str:
    if use_source:
        return (
            f"[{source_index}:a]atrim=start={_number(clip.source_start)}:"
            f"end={_number(clip.source_end)},asetpts=PTS-STARTPTS,"
            f"{_atempo(clip.speed)},aresample=48000,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,asettb=1/48000[{label}]"
        )
    return (
        f"anullsrc=r=48000:cl=stereo,atrim=duration={_number(duration)},"
        "asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:"
        f"sample_rates=48000:channel_layouts=stereo,asettb=1/48000[{label}]"
    )


def _hardware_probe(
    ffmpeg: str, encoder: str
) -> tuple[bool, str]:
    """Probe encoder and return capability plus actionable failure reason."""
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=16x16:r=16",
        "-frames:v",
        "16",
        "-an",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, shell=False, check=False
        )
    except OSError as exc:
        return False, f"could not run encoder probe: {exc}"
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip()
    return False, detail or f"encoder probe exited with status {result.returncode}"


def compile_render(
    plan: EditPlan, ffmpeg: str, encoder: str = "libx264"
) -> RenderCommand:
    """Compile plan into one shell-free FFmpeg command."""
    selected_encoder = encoder
    warnings: list[RenderWarning] = []
    if encoder != "libx264":
        supported, reason = _hardware_probe(ffmpeg, encoder)
        if not supported:
            selected_encoder = "libx264"
            warnings.append(
                RenderWarning(
                    code="hardware_encoder_fallback",
                    message=(
                        f"encoder {encoder} unavailable; using libx264 fallback"
                    ),
                    reason=reason,
                    requested_encoder=encoder,
                    selected_encoder=selected_encoder,
                )
            )

    final_path = plan.sources[0].path.parent / f"{plan.output.kind}.mp4"
    partial_path = final_path.with_name(final_path.name + ".partial")
    args: list[str] = [ffmpeg, "-hide_banner", "-y"]
    for source in plan.sources:
        args.extend(["-i", str(source.path)])

    source_indexes = {source.id: index for index, source in enumerate(plan.sources)}
    graph: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    clip_durations: list[Decimal] = []
    for index, clip in enumerate(plan.clips):
        source_index = source_indexes[clip.source_id]
        duration = (clip.source_end - clip.source_start) / clip.speed
        clip_durations.append(duration)
        video_label, video_graph = _video_framing(
            clip,
            plan.output.width,
            plan.output.height,
            plan.output.frame_rate,
            source_index,
            index,
        )
        graph.extend(video_graph)
        video_labels.append(video_label)
        if plan.output.audio == "none":
            continue
        audio_label = f"aclip{index}"
        # Silence policy is explicit and must never inspect or map source audio.
        use_source_audio = plan.output.audio == "source" and plan.sources[source_index].has_audio
        graph.append(
            _audio_filter(
                source_index,
                clip,
                duration,
                audio_label,
                use_source=use_source_audio,
            )
        )
        audio_labels.append(audio_label)

    current_video = video_labels[0]
    current_audio = audio_labels[0] if audio_labels else None
    current_duration = clip_durations[0]
    for index in range(1, len(video_labels)):
        transition = _transition(plan, index - 1)
        next_video = video_labels[index]
        video_out = f"vjoin{index}"
        if transition is not None and transition.kind == "dissolve" and transition.duration > 0:
            offset = current_duration - transition.duration
            graph.append(
                f"[{current_video}][{next_video}]xfade=transition=fade:"
                f"duration={_number(transition.duration)}:offset={_number(offset)}"
                f"[{video_out}]"
            )
            current_duration += clip_durations[index] - transition.duration
        else:
            graph.append(
                f"[{current_video}][{next_video}]concat=n=2:v=1:a=0[{video_out}]"
            )
            current_duration += clip_durations[index]
        current_video = video_out
        if current_audio is not None:
            audio_out = f"ajoin{index}"
            if transition is not None and transition.kind == "dissolve" and transition.duration > 0:
                graph.append(
                    f"[{current_audio}][{audio_labels[index]}]acrossfade="
                    f"d={_number(transition.duration)}:c1=tri:c2=tri[{audio_out}]"
                )
            else:
                graph.append(
                    f"[{current_audio}][{audio_labels[index]}]concat=n=2:v=0:a=1"
                    f"[{audio_out}]"
                )
            current_audio = audio_out

    graph.append(f"[{current_video}]format=yuv420p,settb=AVTB[vout]")
    args.extend(["-filter_complex", ";".join(graph), "-map", "[vout]"])
    if current_audio is not None:
        args.extend(["-map", f"[{current_audio}]"])
    args.extend(
        [
            "-r",
            _number(plan.output.frame_rate),
            "-c:v",
            selected_encoder,
            "-pix_fmt",
            "yuv420p",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-movflags",
            "+faststart",
        ]
    )
    if current_audio is not None:
        args.extend(["-c:a", "aac", "-ar", "48000"])
    args.extend(["-f", "mp4", str(partial_path)])
    return RenderCommand(
        tuple(args),
        partial_path,
        final_path,
        timeline_duration(plan),
        plan.output,
        tuple(warnings),
    )


def probe_hardware_encoder(ffmpeg: str, encoder: str) -> bool:
    """Prove encoder can encode 16 generated frames, without source media."""
    return _hardware_probe(ffmpeg, encoder)[0]
