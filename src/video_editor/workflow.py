"""Local, resumable orchestration for inspection, planning, rendering, and reports."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from video_editor.config import AppConfig
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.capabilities import detect_capabilities
from video_editor.media.discovery import SourceCandidate, discover_sources
from video_editor.media.probe import MediaProbe, probe_media
from video_editor.media.proxies import create_analysis_media
from video_editor.media.sequencing import sequence_sources
from video_editor.media.storage import (
    VolumeIdentity,
    assert_expected_volume,
    assert_free_space,
    inspect_volume,
)
from video_editor.models.edit_plan import load_plan, write_plan
from video_editor.persistence.database import JobStore, StageStatus
from video_editor.planning.sample_plan import create_sample_plans
from video_editor.rendering.compiler import compile_render
from video_editor.rendering.runner import run_render
from video_editor.reporting import write_json_report, write_markdown_report
from video_editor.validation.outputs import validate_output

STAGES = ("inspect", "proxy", "plan", "render", "validate", "report")
IMPLEMENTATION_VERSION = "phase1-workflow-v1"
EXIT_CODES = {
    ErrorCategory.CONFIGURATION: 10,
    ErrorCategory.STORAGE: 11,
    ErrorCategory.INSPECTION: 12,
    ErrorCategory.PLAN: 13,
    ErrorCategory.RENDER: 14,
    ErrorCategory.OUTPUT: 15,
    ErrorCategory.STATE: 16,
}


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _candidate_data(source: SourceCandidate) -> dict[str, Any]:
    return {
        "source_id": source.fingerprint,
        "path": str(source.path),
        "size_bytes": source.size_bytes,
        "discovery_index": source.discovery_index,
        "fingerprint": source.fingerprint,
        "identity_version": source.identity_version,
    }


def _volume_data(volume: VolumeIdentity) -> dict[str, Any]:
    return {
        "device": volume.device,
        "mount_point": str(volume.mount_point),
        "filesystem": volume.filesystem,
    }


def _volume(value: dict[str, Any]) -> VolumeIdentity:
    return VolumeIdentity(
        int(value["device"]), Path(value["mount_point"]), value.get("filesystem")
    )


def _probe_data(probe: MediaProbe) -> dict[str, Any]:
    return probe.model_dump(mode="json")


class WorkflowService:
    """Orchestrate local stages while keeping every completed result resumable."""

    def __init__(self, config: AppConfig, store: JobStore) -> None:
        self.config = config
        self.store = store
        self._probes: dict[str, MediaProbe] = {}

    def _require_open_store(self) -> None:
        try:
            _ = self.store.connection
        except RuntimeError as exc:
            raise VideoEditorError(
                ErrorCategory.STATE, "JobStore must be opened before workflow use"
            ) from exc

    def _check_volume(
        self, path: Path, expected: VolumeIdentity | None = None, required: int = 0
    ) -> VolumeIdentity:
        actual = inspect_volume(path)
        if expected is not None and actual != expected:
            assert_expected_volume(path, expected)
        assert_free_space(
            actual.mount_point, required, self.config.storage_reserve_bytes
        )
        return actual

    def _start(
        self, job_id: str, name: str, fingerprint: Any, settings: Any = None
    ) -> None:
        self.store.start_stage(
            job_id,
            name,
            _hash(fingerprint),
            _hash(settings if settings is not None else self.config),
            IMPLEMENTATION_VERSION,
        )

    def _completed(self, job: dict[str, Any], name: str) -> bool:
        stage = job["stages"].get(name)
        if not stage or stage["status"] != StageStatus.COMPLETED:
            return False
        result = stage.get("result") or {}
        # Explicit artifact paths must remain present. This catches vanished external media.
        paths: list[str] = []
        if isinstance(result, dict):
            for key in ("artifact", "path", "plan", "report"):
                value = result.get(key)
                if isinstance(value, str):
                    paths.append(value)
            paths.extend(
                str(item.get("path"))
                for item in result.get("artifacts", [])
                if isinstance(item, dict) and item.get("path")
            )
        for artifact in job.get("artifacts", []):
            if artifact.get("stage") == name:
                paths.append(str(artifact.get("path", "")))
        return not any(path and not Path(path).is_file() for path in paths)

    def _new_job(self, input_path: Path, volume: VolumeIdentity) -> str:
        config = {
            "input_path": str(input_path),
            "paths": {
                key: str(value) for key, value in vars(self.config.paths).items()
            },
        }
        return self.store.create_job(config, _volume_data(volume))

    def _inspect_stage(
        self, job_id: str, input_path: Path, volume: VolumeIdentity
    ) -> dict[str, Any]:
        sources = discover_sources(input_path)
        probes: dict[str, MediaProbe] = {}
        skipped: list[dict[str, str]] = []
        for source in sources:
            try:
                probes[source.fingerprint] = probe_media(source.path)
            except VideoEditorError as exc:
                skipped.append({"path": str(source.path), "reason": str(exc)})
        self._probes = probes
        self.store.save_sources(
            job_id,
            [
                {
                    **_candidate_data(source),
                    "probe": _probe_data(probes[source.fingerprint]),
                }
                for source in sources
                if source.fingerprint in probes
            ],
        )
        creation_times = {
            str(path): probe.creation_time
            for path, probe in ((probe.path, probe) for probe in probes.values())
        }
        groups = sequence_sources(
            [source for source in sources if source.fingerprint in probes],
            creation_times,
        )
        chronology = [
            {
                "group_id": group.group_id,
                "members": [member.source.fingerprint for member in group.members],
                "warnings": list(group.warnings),
            }
            for group in groups
        ]
        self.store.save_chronology(job_id, chronology)
        artifact = self.config.paths.workspace_dir / f"{job_id}-inventory.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                {
                    "capabilities": detect_capabilities().model_dump(mode="json"),
                    "sources": [_candidate_data(source) for source in sources],
                    "skipped_inputs": skipped,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n"
        )
        self.store.save_artifact(
            job_id, "inspect", artifact, {"source_count": len(sources)}
        )
        return {
            "artifact": str(artifact),
            "sources": [
                _candidate_data(source)
                for source in sources
                if source.fingerprint in probes
            ],
            "skipped_inputs": skipped,
            "chronology": chronology,
        }

    def inspect(self, input_path: Path) -> dict[str, Any]:
        self._require_open_store()
        input_path = Path(input_path)
        volume = self._check_volume(input_path)
        job_id = self._new_job(input_path, volume)
        self._start(job_id, "inspect", str(input_path))
        try:
            result = self._inspect_stage(job_id, input_path, volume)
            self.store.complete_stage(job_id, "inspect", result)
            return {"job_id": job_id, **result}
        except VideoEditorError as exc:
            self.store.fail_stage(job_id, "inspect", exc.category, str(exc))
            raise
        except Exception as exc:
            self.store.fail_stage(job_id, "inspect", ErrorCategory.INSPECTION, str(exc))
            raise VideoEditorError(ErrorCategory.INSPECTION, str(exc)) from exc

    def _load_or_inspect(self, input_path: Path) -> tuple[str, dict[str, Any]]:
        result = self.inspect(input_path)
        return str(result["job_id"]), result

    def plan(self, input_path: Path, output_path: Path) -> dict[str, Any]:
        self._require_open_store()
        job_id, inspected = self._load_or_inspect(Path(input_path))
        job = self.store.get_job(job_id)
        volume = _volume(job["volume"])
        output_path = Path(output_path)
        self._check_volume(volume.mount_point, volume, required=0)
        self._start(job_id, "plan", inspected["sources"], output_path)
        try:
            sources = discover_sources(Path(input_path))
            groups = sequence_sources(
                sources,
                {
                    str(path): probe.creation_time
                    for path, probe in (
                        (probe.path, probe) for probe in self._probes.values()
                    )
                },
            )
            horizontal, vertical = create_sample_plans(
                groups,
                cast(Any, self._probes),
                {source.fingerprint: source.path for source in sources},
            )
            output_path.mkdir(parents=True, exist_ok=True)
            horizontal_path = output_path / "edit-plan-horizontal.json"
            vertical_path = output_path / "edit-plan-vertical.json"
            write_plan(horizontal, horizontal_path)
            write_plan(vertical, vertical_path)
            self.store.save_artifact(job_id, "plan", horizontal_path)
            self.store.save_artifact(job_id, "plan", vertical_path)
            result = {
                "plans": [str(horizontal_path), str(vertical_path)],
                "output": str(output_path),
            }
            self.store.complete_stage(job_id, "plan", result)
            return {"job_id": job_id, **result}
        except VideoEditorError as exc:
            self.store.fail_stage(job_id, "plan", exc.category, str(exc))
            raise
        except Exception as exc:
            self.store.fail_stage(job_id, "plan", ErrorCategory.PLAN, str(exc))
            raise VideoEditorError(ErrorCategory.PLAN, str(exc)) from exc

    def render_from_plan(self, plan_path: Path) -> dict[str, Any]:
        self._require_open_store()
        plan_path = Path(plan_path)
        plan = load_plan(plan_path)
        volume = self._check_volume(plan.sources[0].path)
        job_id = self._new_job(plan.sources[0].path.parent, volume)
        self._start(job_id, "render", str(plan_path), plan.model_dump(mode="json"))
        try:
            command = compile_render(plan, "ffmpeg")
            run_render(
                command,
                lambda: self.store.fail_stage(
                    job_id,
                    "render",
                    ErrorCategory.RENDER,
                    "render interrupted",
                    interrupted=True,
                ),
            )
            self.store.save_artifact(job_id, "render", command.final_path)
            result = {
                "output": str(command.final_path),
                "warnings": [warning.message for warning in command.warnings],
            }
            self.store.complete_stage(job_id, "render", result)
            return {"job_id": job_id, **result}
        except VideoEditorError as exc:
            if (
                self.store.get_job(job_id)["stages"]["render"]["status"]
                == StageStatus.RUNNING
            ):
                self.store.fail_stage(job_id, "render", exc.category, str(exc))
            raise

    def run(self, input_path: Path) -> dict[str, Any]:
        planned = self.plan(Path(input_path), self.config.paths.output_dir)
        job_id = str(planned["job_id"])
        job = self.store.get_job(job_id)
        volume = _volume(job["volume"])
        self._start(job_id, "proxy", job["sources"])
        proxy_results: list[dict[str, Any]] = []
        try:
            self._check_volume(volume.mount_point, volume, required=0)
            for source in job["sources"]:
                probe = self._probes.get(source["source_id"])
                if probe is None:
                    continue
                proxy, audio, mapping = create_analysis_media(
                    Path(source["path"]),
                    source["source_id"],
                    self.config.paths.cache_dir,
                    probe.duration,
                )
                proxy_results.append(
                    {
                        "source_id": source["source_id"],
                        "proxy": str(proxy),
                        "audio": str(audio) if audio else None,
                        "mapping": mapping.__dict__,
                    }
                )
            self.store.complete_stage(job_id, "proxy", {"artifacts": proxy_results})
        except VideoEditorError as exc:
            self.store.fail_stage(job_id, "proxy", exc.category, str(exc))
            raise
        render_results = []
        for path in planned["plans"]:
            render_results.append(
                self._render_existing_plan(job_id, Path(path), volume)
            )
        self._start(job_id, "validate", render_results)
        try:
            for result in render_results:
                plan = load_plan(Path(result["plan"]))
                expected_duration = sum(
                    (
                        (clip.source_end - clip.source_start) / clip.speed
                        for clip in plan.clips
                    ),
                    Decimal(0),
                )
                validate_output(Path(result["output"]), plan.output, expected_duration)
            self.store.complete_stage(job_id, "validate", {"outputs": render_results})
        except VideoEditorError as exc:
            self.store.fail_stage(job_id, "validate", exc.category, str(exc))
            raise
        report_state = self.store.get_job(job_id)
        report_state["output"] = {"outputs": render_results}
        report_state["selected_moments"] = []
        report_state["skipped_inputs"] = job.get("sources", [])
        report_state["storage_roots"] = {
            key: str(value) for key, value in vars(self.config.paths).items()
        }
        report_state["estimated_peak_space_bytes"] = sum(
            item.get("size_bytes", 0) for item in job.get("sources", [])
        )
        self._start(job_id, "report", render_results)
        json_path = self.config.paths.output_dir / "report.json"
        md_path = self.config.paths.output_dir / "report.md"
        write_json_report(report_state, json_path)
        write_markdown_report(report_state, md_path)
        self.store.save_artifact(job_id, "report", json_path)
        self.store.save_artifact(job_id, "report", md_path)
        self.store.complete_stage(
            job_id, "report", {"artifacts": [str(json_path), str(md_path)]}
        )
        self.store.complete_job(job_id)
        return {
            "job_id": job_id,
            "outputs": render_results,
            "reports": [str(json_path), str(md_path)],
        }

    def _render_existing_plan(
        self, job_id: str, path: Path, volume: VolumeIdentity
    ) -> dict[str, Any]:
        plan = load_plan(path)
        self._check_volume(volume.mount_point, volume, required=0)
        if (
            self.store.get_job(job_id)["stages"].get("render", {}).get("status")
            != StageStatus.RUNNING
        ):
            self._start(job_id, "render", str(path), plan.model_dump(mode="json"))
        command = compile_render(plan, "ffmpeg")
        run_render(command, lambda: None)
        self.store.save_artifact(
            job_id, "render", command.final_path, name=command.final_path.name
        )
        self.store.complete_stage(
            job_id, "render", {"plan": str(path), "output": str(command.final_path)}
        )
        return {"plan": str(path), "output": str(command.final_path)}

    def status(self, job_id: str) -> dict[str, Any]:
        self._require_open_store()
        try:
            return self.store.get_job(job_id)
        except KeyError as exc:
            raise VideoEditorError(ErrorCategory.STATE, str(exc)) from exc

    def resume(self, job_id: str) -> dict[str, Any]:
        self._require_open_store()
        job = self.status(job_id)
        if self._completed(job, "report") and job["status"] == "completed":
            return job
        input_path = Path(job["config"]["input_path"])
        for name in STAGES:
            job = self.store.get_job(job_id)
            if self._completed(job, name):
                continue
            if name == "inspect":
                volume = _volume(job["volume"])
                self._check_volume(input_path, volume)
                self._start(job_id, "inspect", str(input_path))
                self._inspect_stage(job_id, input_path, volume)
                self.store.complete_stage(
                    job_id,
                    "inspect",
                    {
                        "artifact": str(
                            self.config.paths.workspace_dir / f"{job_id}-inventory.json"
                        )
                    },
                )
            elif name == "plan":
                return self.plan(input_path, self.config.paths.output_dir)
            elif name in {"proxy", "render", "validate", "report"}:
                return self.run(input_path)
        return self.store.get_job(job_id)


def error_exit_code(error: VideoEditorError) -> int:
    """Map stable error category to documented CLI exit code."""
    return EXIT_CODES.get(error.category, 1)
