"""Local, resumable orchestration for inspection, planning, rendering, and reports."""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import ValidationError

from video_editor.config import AppConfig
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.capabilities import detect_capabilities
from video_editor.media.discovery import (
    SourceCandidate,
    bounded_fingerprint,
    discover_sources,
)
from video_editor.media.probe import MediaProbe, probe_media
from video_editor.media.proxies import (
    ProxySettings,
    create_analysis_media,
    valid_cached_media,
)
from video_editor.media.proxies import (
    settings_hash as proxy_settings_hash,
)
from video_editor.media.sequencing import sequence_sources
from video_editor.media.storage import (
    VolumeIdentity,
    assert_free_space,
    inspect_volume,
    is_within,
)
from video_editor.models.edit_plan import (
    EditPlan,
    load_plan,
    timeline_duration,
    write_plan,
)
from video_editor.persistence.database import JobStore, StageStatus
from video_editor.planning.sample_plan import create_sample_plans
from video_editor.rendering.compiler import compile_render
from video_editor.rendering.runner import run_render
from video_editor.reporting import write_json_report, write_markdown_report
from video_editor.validation.outputs import validate_output

STAGES = ("inspect", "proxy", "plan", "render", "validate", "report")
IMPLEMENTATION_VERSION = "phase1-workflow-v2"
_MIN_WRITE_BYTES = 1
EXIT_CODES = {
    ErrorCategory.CONFIGURATION: 10,
    ErrorCategory.STORAGE: 11,
    ErrorCategory.INSPECTION: 12,
    ErrorCategory.PLAN: 13,
    ErrorCategory.RENDER: 14,
    ErrorCategory.OUTPUT: 15,
    ErrorCategory.STATE: 16,
}
_T = TypeVar("_T")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _candidate_data(source: SourceCandidate, probe: MediaProbe) -> dict[str, Any]:
    return {
        "source_id": source.fingerprint,
        "path": str(source.path),
        "size_bytes": source.size_bytes,
        "discovery_index": source.discovery_index,
        "fingerprint": source.fingerprint,
        "identity_version": source.identity_version,
        "probe": probe.model_dump(mode="json"),
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


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _duration_seconds(plan: EditPlan) -> int:
    return max(
        1, int(timeline_duration(plan).to_integral_value(rounding="ROUND_CEILING"))
    )


class WorkflowService:
    """Orchestrate local stages while keeping every completed result resumable."""

    def __init__(self, config: AppConfig, store: JobStore) -> None:
        self.config = config
        self.store = store
        # Phase 1 explicitly serializes all renders. Keep configured concurrency
        # validated for forward-compatible config, but never run parallel renders.
        self._render_lock = threading.Lock()

    def _require_open_store(self) -> None:
        try:
            _ = self.store.connection
        except RuntimeError as exc:
            raise VideoEditorError(
                ErrorCategory.STATE, "JobStore must be opened before workflow use"
            ) from exc

    def _destination_volume(
        self,
        destination: Path,
        required: int,
        expected: VolumeIdentity | None = None,
    ) -> VolumeIdentity:
        if (
            not destination.exists()
            and destination.anchor == "/"
            and destination.parts[1:2] == ("Volumes",)
        ):
            # Never treat an absent macOS mount path as an internal parent.
            inspect_volume(destination)
        existing = _existing_parent(destination)
        actual = inspect_volume(existing)
        if expected is not None and actual != expected:
            raise VideoEditorError(
                ErrorCategory.STORAGE,
                f"volume changed for {destination}: expected {expected}, found {actual}",
            )
        assert_free_space(
            actual.mount_point,
            max(_MIN_WRITE_BYTES, required),
            self.config.storage_reserve_bytes,
        )
        return actual

    def _configured_destination(self, root: Path, job_id: str) -> Path:
        destination = root / job_id
        if destination.resolve() == root.resolve() or not is_within(root, destination):
            raise VideoEditorError(
                ErrorCategory.STORAGE, f"unsafe job destination: {destination}"
            )
        return destination

    def _protect_sources(self, destination: Path, sources: list[Path]) -> None:
        resolved = destination.resolve()
        for source in sources:
            source_resolved = source.resolve()
            if resolved == source_resolved:
                raise VideoEditorError(
                    ErrorCategory.STORAGE,
                    f"generated destination equals source media: {source}",
                )

    @staticmethod
    def validate_configured_roots(
        config: AppConfig, input_path: Path, source_paths: list[Path] | None = None
    ) -> None:
        roots = [input_path.resolve()]
        roots.extend(path.resolve().parent for path in source_paths or [])
        state_root = config.paths.state_dir.resolve()
        for name, generated in (
            ("workspace", config.paths.workspace_dir),
            ("cache", config.paths.cache_dir),
            ("output", config.paths.output_dir),
        ):
            generated_root = generated.resolve()
            if (
                state_root == generated_root
                or state_root.is_relative_to(generated_root)
                or generated_root.is_relative_to(state_root)
            ):
                raise VideoEditorError(
                    ErrorCategory.STORAGE,
                    f"configured state root overlaps {name} root: "
                    f"{config.paths.state_dir} and {generated}",
                )
        for name, configured in (
            ("workspace", config.paths.workspace_dir),
            ("cache", config.paths.cache_dir),
            ("output", config.paths.output_dir),
            ("state", config.paths.state_dir),
        ):
            destination = configured.resolve()
            for source_root in roots:
                if (
                    destination == source_root
                    or destination.is_relative_to(source_root)
                    or source_root.is_relative_to(destination)
                ):
                    raise VideoEditorError(
                        ErrorCategory.STORAGE,
                        f"configured {name} root overlaps input/source root: "
                        f"{configured} and {source_root}",
                    )

    def _validate_roots(
        self, input_path: Path, source_paths: list[Path] | None = None
    ) -> None:
        self.validate_configured_roots(self.config, input_path, source_paths)

    def _preflight_plan_sources(self, plan: EditPlan) -> None:
        source_paths = [source.path for source in plan.sources]
        self._validate_roots(source_paths[0].parent, source_paths)
        for source in plan.sources:
            path = source.path
            try:
                stat = path.stat()
            except OSError as exc:
                raise VideoEditorError(
                    ErrorCategory.PLAN,
                    f"plan source is not readable: {path}: {exc}",
                ) from exc
            if not stat_module.S_ISREG(stat.st_mode):
                raise VideoEditorError(
                    ErrorCategory.PLAN, f"plan source is not a regular file: {path}"
                )
            if not os.access(path, os.R_OK):
                raise VideoEditorError(
                    ErrorCategory.PLAN, f"plan source is not readable: {path}"
                )
            try:
                identity = bounded_fingerprint(path)
            except VideoEditorError as exc:
                raise VideoEditorError(
                    ErrorCategory.PLAN, f"cannot verify plan source: {path}: {exc}"
                ) from exc
            if identity != source.identity.rsplit(":", 1)[-1]:
                raise VideoEditorError(
                    ErrorCategory.PLAN,
                    f"plan source identity changed: {path}",
                )

    def _stage_keys(self, fingerprint: Any, settings: Any) -> tuple[str, str]:
        return _hash(fingerprint), _hash(settings)

    def _start(
        self,
        job_id: str,
        name: str,
        fingerprint: Any,
        settings: Any,
        *,
        preserve_artifacts: bool = False,
    ) -> None:
        input_hash, settings_hash = self._stage_keys(fingerprint, settings)
        self.store.start_stage(
            job_id,
            name,
            input_hash,
            settings_hash,
            IMPLEMENTATION_VERSION,
            preserve_artifacts=preserve_artifacts,
        )

    def _proxy_artifact_valid(
        self, path: Path, metadata: dict[str, Any] | None
    ) -> bool:
        if not metadata:
            return False
        settings_data = metadata.get("settings")
        if not isinstance(settings_data, dict):
            return False
        try:
            settings = ProxySettings(
                max_width=int(settings_data["max_width"]),
                fps=int(settings_data["fps"]),
                video_codec=str(settings_data["video_codec"]),
            )
            expected_kind = str(metadata["kind"])
            expected_settings_hash = proxy_settings_hash(settings)
            if metadata.get("settings_hash") != expected_settings_hash:
                return False
            mapping = metadata["mapping"]
            if not isinstance(mapping, dict):
                return False
            source_id = metadata.get("source_id")
            source_identity = metadata.get("source_identity")
            if not isinstance(source_id, str) or not isinstance(source_identity, str):
                return False
            if mapping.get("source_id") != source_id:
                return False
            if ":" not in source_identity:
                return False
            if mapping.get("settings_hash") != metadata.get("settings_hash"):
                return False
            if mapping.get("tool_version") != metadata.get("tool_version"):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return valid_cached_media(
            path,
            settings,
            kind=expected_kind,
            ffprobe="ffprobe",
        )

    def _artifact_valid(
        self, name: str, path: Path, metadata: dict[str, Any] | None = None
    ) -> bool:
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            if name == "plan":
                load_plan(path)
            elif name == "proxy":
                if not self._proxy_artifact_valid(path, metadata):
                    path.unlink(missing_ok=True)
                    return False
            elif name == "render":
                if not metadata or not isinstance(metadata.get("plan"), str):
                    return False
                plan = load_plan(Path(metadata["plan"]))
                validate_output(path, plan.output, timeline_duration(plan))
            elif name == "inspect" or (name == "report" and path.suffix == ".json"):
                value = json.loads(path.read_text())
                if not isinstance(value, dict):
                    return False
        except (OSError, ValueError, TypeError, VideoEditorError, ValidationError):
            return False
        return True

    def _completed(self, job: dict[str, Any], name: str) -> bool:
        stage = job.get("stages", {}).get(name)
        if not stage or stage.get("status") != StageStatus.COMPLETED:
            return False
        artifacts = [
            Path(item["path"])
            for item in job.get("artifacts", [])
            if item.get("stage") == name and isinstance(item.get("path"), str)
        ]
        if name in {"inspect", "proxy", "plan", "render", "report"} and not artifacts:
            return False
        return all(
            self._artifact_valid(
                name,
                Path(item["path"]),
                cast(dict[str, Any], item.get("metadata")),
            )
            for item in job.get("artifacts", [])
            if item.get("stage") == name and isinstance(item.get("path"), str)
        )

    def _stage_reusable(
        self,
        job: dict[str, Any],
        name: str,
        fingerprint: Any,
        settings: Any,
    ) -> bool:
        stage = job.get("stages", {}).get(name)
        input_hash, settings_hash = self._stage_keys(fingerprint, settings)
        return bool(
            stage
            and stage.get("input_fingerprint") == input_hash
            and stage.get("settings_hash") == settings_hash
            and stage.get("implementation_version") == IMPLEMENTATION_VERSION
            and self._completed(job, name)
        )

    def _new_job(self, input_path: Path) -> str:
        self._validate_roots(input_path)
        source_volume = inspect_volume(input_path)
        destinations = {
            name: _volume_data(self._destination_volume(path, _MIN_WRITE_BYTES))
            for name, path in (
                ("workspace", self.config.paths.workspace_dir),
                ("cache", self.config.paths.cache_dir),
                ("output", self.config.paths.output_dir),
            )
        }
        config = {
            "input_path": str(input_path),
            "paths": {
                key: str(value) for key, value in vars(self.config.paths).items()
            },
            "destination_volumes": destinations,
        }
        return self.store.create_job(config, _volume_data(source_volume))

    def _expected_destination_volume(
        self, job: dict[str, Any], name: str
    ) -> VolumeIdentity:
        values = job.get("config", {}).get("destination_volumes", {})
        value = values.get(name)
        if not isinstance(value, dict):
            raise VideoEditorError(
                ErrorCategory.STATE, f"missing recorded {name} volume"
            )
        return _volume(value)

    def _run_stage(
        self,
        job_id: str,
        name: str,
        fingerprint: Any,
        settings: Any,
        category: ErrorCategory,
        operation: Callable[[], _T],
    ) -> _T:
        self._start(
            job_id,
            name,
            fingerprint,
            settings,
            preserve_artifacts=name == "render",
        )
        try:
            return operation()
        except VideoEditorError as exc:
            self.store.fail_stage(
                job_id,
                name,
                exc.category,
                str(exc),
                interrupted=exc.interrupted,
            )
            raise
        except KeyboardInterrupt as exc:
            message = f"{name} interrupted"
            self.store.fail_stage(job_id, name, category, message, interrupted=True)
            raise VideoEditorError(category, message, interrupted=True) from exc
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            self.store.fail_stage(job_id, name, category, str(exc))
            raise VideoEditorError(category, f"{name} failed: {exc}") from exc
        except Exception as exc:
            self.store.fail_stage(job_id, name, category, str(exc))
            raise VideoEditorError(
                category, f"{name} failed unexpectedly: {exc}"
            ) from exc

    def _discover(self, input_path: Path) -> list[SourceCandidate]:
        return discover_sources(input_path)

    def _inspect_identity(self, input_path: Path) -> list[dict[str, Any]]:
        return [
            {
                "path": str(source.path),
                "fingerprint": source.fingerprint,
                "size_bytes": source.size_bytes,
            }
            for source in self._discover(input_path)
        ]

    def _inspect_stage(self, job_id: str, input_path: Path) -> dict[str, Any]:
        sources = self._discover(input_path)
        self._validate_roots(input_path, [source.path for source in sources])
        workspace = self._configured_destination(
            self.config.paths.workspace_dir, job_id
        )
        job = self.store.get_job(job_id)
        self._destination_volume(
            workspace,
            max(_MIN_WRITE_BYTES, sum(source.size_bytes for source in sources) // 1000),
            self._expected_destination_volume(job, "workspace"),
        )
        probes: dict[str, MediaProbe] = {}
        skipped: list[dict[str, str]] = []
        for source in sources:
            try:
                probes[source.fingerprint] = probe_media(source.path)
            except VideoEditorError as exc:
                skipped.append(
                    {
                        "path": str(source.path),
                        "category": str(exc.category),
                        "reason": str(exc),
                    }
                )
        usable = [source for source in sources if source.fingerprint in probes]
        self.store.replace_sources(
            job_id,
            [_candidate_data(source, probes[source.fingerprint]) for source in usable],
        )
        creation_times = {
            str(probe.path): probe.creation_time for probe in probes.values()
        }
        groups = sequence_sources(usable, creation_times)
        chronology = [
            {
                "group_id": group.group_id,
                "members": [
                    {
                        "source_id": member.source.fingerprint,
                        "warnings": list(member.warnings),
                    }
                    for member in group.members
                ],
                "warnings": list(group.warnings),
            }
            for group in groups
        ]
        self.store.replace_chronology(job_id, chronology)
        workspace.mkdir(parents=True, exist_ok=True)
        artifact = workspace / "inventory.json"
        self._protect_sources(artifact, [source.path for source in sources])
        artifact.write_text(
            json.dumps(
                {
                    "capabilities": detect_capabilities().model_dump(mode="json"),
                    "sources": [
                        _candidate_data(source, probes[source.fingerprint])
                        for source in usable
                    ],
                    "skipped_inputs": skipped,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n"
        )
        self.store.save_artifact(
            job_id, "inspect", artifact, {"source_count": len(usable)}
        )
        result = {
            "artifact": str(artifact),
            "sources": [
                _candidate_data(source, probes[source.fingerprint]) for source in usable
            ],
            "skipped_inputs": skipped,
            "warnings": [
                warning.model_dump(mode="json")
                for probe in probes.values()
                for warning in probe.warnings
            ],
            "chronology": chronology,
        }
        self.store.complete_stage(job_id, "inspect", result)
        return result

    def inspect(self, input_path: Path) -> dict[str, Any]:
        self._require_open_store()
        input_path = Path(input_path)
        job_id = self._new_job(input_path)
        identity = self._inspect_identity(input_path)
        result = self._run_stage(
            job_id,
            "inspect",
            identity,
            {"workspace": str(self.config.paths.workspace_dir)},
            ErrorCategory.INSPECTION,
            lambda: self._inspect_stage(job_id, input_path),
        )
        return {"job_id": job_id, **result}

    def _source_probes(self, job: dict[str, Any]) -> dict[str, MediaProbe]:
        probes: dict[str, MediaProbe] = {}
        for source in job.get("sources", []):
            try:
                probes[str(source["source_id"])] = MediaProbe.model_validate(
                    source["probe"]
                )
            except (KeyError, TypeError, ValidationError) as exc:
                raise VideoEditorError(
                    ErrorCategory.STATE,
                    f"persisted probe is invalid for {source.get('path', 'unknown')}: {exc}",
                ) from exc
        return probes

    def _groups(self, job: dict[str, Any]) -> Any:
        sources = [
            SourceCandidate(
                path=Path(source["path"]),
                size_bytes=int(source["size_bytes"]),
                discovery_index=int(source["discovery_index"]),
                fingerprint=str(source["fingerprint"]),
                identity_version=str(source["identity_version"]),
            )
            for source in job.get("sources", [])
        ]
        probes = self._source_probes(job)
        creation_times = {
            str(probe.path): probe.creation_time for probe in probes.values()
        }
        return sequence_sources(sources, creation_times)

    def _plan_stage(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        sources = job.get("sources", [])
        output = self._configured_destination(self.config.paths.output_dir, job_id)
        self._destination_volume(
            output,
            max(
                _MIN_WRITE_BYTES,
                sum(int(item.get("size_bytes", 0)) for item in sources) // 10000,
            ),
            self._expected_destination_volume(job, "output"),
        )
        probes = self._source_probes(job)
        groups = self._groups(job)
        horizontal, vertical = create_sample_plans(
            groups,
            cast(Any, probes),
            {str(source["source_id"]): Path(source["path"]) for source in sources},
        )
        output.mkdir(parents=True, exist_ok=True)
        horizontal_path = output / "edit-plan-horizontal.json"
        vertical_path = output / "edit-plan-vertical.json"
        source_paths = [Path(item["path"]) for item in sources]
        self._protect_sources(horizontal_path, source_paths)
        self._protect_sources(vertical_path, source_paths)
        write_plan(horizontal, horizontal_path)
        write_plan(vertical, vertical_path)
        for path in (horizontal_path, vertical_path):
            load_plan(path)
            self.store.save_artifact(job_id, "plan", path)
        result = {
            "plans": [str(horizontal_path), str(vertical_path)],
            "output": str(output),
        }
        self.store.complete_stage(job_id, "plan", result)
        return result

    def plan(self, input_path: Path, output_path: Path) -> dict[str, Any]:
        self._require_open_store()
        requested = Path(output_path)
        if requested.resolve() != self.config.paths.output_dir.resolve():
            raise VideoEditorError(
                ErrorCategory.STORAGE,
                f"plan output must use configured output root {self.config.paths.output_dir}",
            )
        input_path = Path(input_path)
        job_id = self._new_job(input_path)
        self._ensure_inspect(job_id, input_path)
        result = self._ensure_plan(job_id)
        return {"job_id": job_id, **result}

    def _ensure_inspect(self, job_id: str, input_path: Path) -> dict[str, Any]:
        identity = self._inspect_identity(input_path)
        settings = {"workspace": str(self.config.paths.workspace_dir)}
        job = self.store.get_job(job_id)
        if self._stage_reusable(job, "inspect", identity, settings):
            return cast(dict[str, Any], job["stages"]["inspect"]["result"])
        return self._run_stage(
            job_id,
            "inspect",
            identity,
            settings,
            ErrorCategory.INSPECTION,
            lambda: self._inspect_stage(job_id, input_path),
        )

    def _ensure_proxy(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        fingerprint = [
            {
                "source_id": item["source_id"],
                "identity": item.get("fingerprint", item["source_id"]),
            }
            for item in job.get("sources", [])
        ]
        proxy_settings = ProxySettings()
        settings = {
            "cache": str(self.config.paths.cache_dir),
            "proxy_settings": {
                "max_width": proxy_settings.max_width,
                "fps": proxy_settings.fps,
                "video_codec": proxy_settings.video_codec,
            },
            "tool_version": "ffmpeg",
        }
        if self._stage_reusable(job, "proxy", fingerprint, settings):
            return cast(dict[str, Any], job["stages"]["proxy"]["result"])

        def operation() -> dict[str, Any]:
            current = self.store.get_job(job_id)
            cache = self._configured_destination(self.config.paths.cache_dir, job_id)
            estimate = max(
                _MIN_WRITE_BYTES,
                sum(
                    int(item.get("size_bytes", 0))
                    for item in current.get("sources", [])
                )
                // 2,
            )
            self._destination_volume(
                cache, estimate, self._expected_destination_volume(current, "cache")
            )
            cache.mkdir(parents=True, exist_ok=True)
            probes = self._source_probes(current)
            artifacts: list[dict[str, Any]] = []
            for source in current.get("sources", []):
                source_path = Path(source["path"])
                self._protect_sources(cache, [source_path])
                probe = probes[str(source["source_id"])]
                source_id = str(source["source_id"])
                source_identity = (
                    f"{source['identity_version']}:{source['fingerprint']}"
                )
                tool_version = "ffmpeg"
                proxy, audio, mapping = create_analysis_media(
                    source_path,
                    source_id,
                    cache,
                    probe.duration,
                    settings=proxy_settings,
                    tool_version=tool_version,
                )
                mapping_data = {
                    key: str(value) if hasattr(value, "as_tuple") else value
                    for key, value in mapping.__dict__.items()
                }
                media_metadata = {
                    "source_id": source_id,
                    "source_identity": source_identity,
                    "settings": {
                        "max_width": proxy_settings.max_width,
                        "fps": proxy_settings.fps,
                        "video_codec": proxy_settings.video_codec,
                    },
                    "settings_hash": mapping.settings_hash,
                    "tool_version": mapping.tool_version,
                    "mapping": mapping_data,
                }
                for path in (proxy, audio):
                    if path is not None:
                        self.store.save_artifact(
                            job_id,
                            "proxy",
                            path,
                            {
                                **media_metadata,
                                "kind": "audio" if path == audio else "proxy",
                            },
                        )
                artifacts.append(
                    {
                        "source_id": source["source_id"],
                        "proxy": str(proxy),
                        "audio": str(audio) if audio else None,
                        "mapping": {
                            key: str(value) if hasattr(value, "as_tuple") else value
                            for key, value in mapping.__dict__.items()
                        },
                    }
                )
            result = {"artifacts": artifacts}
            self.store.complete_stage(job_id, "proxy", result)
            return result

        return self._run_stage(
            job_id, "proxy", fingerprint, settings, ErrorCategory.RENDER, operation
        )

    def _ensure_plan(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        fingerprint = {
            "sources": [item["source_id"] for item in job.get("sources", [])],
            "chronology": job.get("chronology", []),
        }
        settings = {
            "output": str(self.config.paths.output_dir),
            "planner": "phase1-sample-v1",
        }
        if self._stage_reusable(job, "plan", fingerprint, settings):
            return cast(dict[str, Any], job["stages"]["plan"]["result"])
        return self._run_stage(
            job_id,
            "plan",
            fingerprint,
            settings,
            ErrorCategory.PLAN,
            lambda: self._plan_stage(job_id),
        )

    def _render_plan(self, job_id: str, path: Path) -> dict[str, Any]:
        plan = load_plan(path)
        output_root = self._configured_destination(self.config.paths.output_dir, job_id)
        output_name = (
            "long.mp4"
            if plan.output.width == 1920 and plan.output.height == 1080
            else "short-01.mp4"
        )
        command = compile_render(
            plan,
            "ffmpeg",
            output_dir=output_root,
            output_name=output_name,
        )
        source_paths = [source.path for source in plan.sources]
        self._protect_sources(command.final_path, source_paths)
        metadata: dict[str, Any] = {
            "plan": str(path),
            "warnings": [warning.message for warning in command.warnings],
        }

        def persist_published(final_path: Path) -> None:
            self.store.save_artifact(job_id, "render", final_path, metadata)

        with self._render_lock:
            run_render(command, lambda: None, persist_published)
        validated = validate_output(
            command.final_path, plan.output, timeline_duration(plan)
        )
        metadata.update(
            {
                "width": validated.video.width if validated.video else None,
                "height": validated.video.height if validated.video else None,
                "duration": validated.duration,
                "video_codec": validated.video.codec_name if validated.video else None,
                "audio_codec": validated.audio.codec_name if validated.audio else None,
            }
        )
        self.store.save_artifact(job_id, "render", command.final_path, metadata)
        return {
            "plan": str(path),
            "output": str(command.final_path),
            **metadata,
        }

    def _valid_render_results(
        self, job: dict[str, Any], plan_paths: list[Path]
    ) -> dict[Path, dict[str, Any]]:
        expected = {path.resolve() for path in plan_paths}
        results: dict[Path, dict[str, Any]] = {}
        for artifact in job.get("artifacts", []):
            if artifact.get("stage") != "render":
                continue
            metadata = artifact.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("plan"), str
            ):
                continue
            plan_path = Path(metadata["plan"])
            if plan_path.resolve() not in expected:
                continue
            output_path = Path(str(artifact.get("path", "")))
            if not self._artifact_valid("render", output_path, metadata):
                continue
            results[plan_path.resolve()] = {
                "plan": str(plan_path),
                "output": str(output_path),
                "warnings": list(metadata.get("warnings", [])),
                **metadata,
            }
        return results

    def _recover_render_results(
        self, job_id: str, plan_paths: list[Path]
    ) -> dict[Path, dict[str, Any]]:
        """Recover valid finals when interruption happened before artifact persistence."""
        output = self._configured_destination(self.config.paths.output_dir, job_id)
        recovered: dict[Path, dict[str, Any]] = {}
        for plan_path in plan_paths:
            plan = load_plan(plan_path)
            name = (
                "long.mp4"
                if plan.output.width == 1920 and plan.output.height == 1080
                else "short-01.mp4"
            )
            output_path = output / name
            metadata = {"plan": str(plan_path), "warnings": []}
            if self._artifact_valid("render", output_path, metadata):
                self.store.save_artifact(job_id, "render", output_path, metadata)
                recovered[plan_path.resolve()] = {
                    "plan": str(plan_path),
                    "output": str(output_path),
                    **metadata,
                }
        return recovered

    def _ensure_render(self, job_id: str, planned: dict[str, Any]) -> dict[str, Any]:
        plan_paths = [Path(value) for value in planned.get("plans", [])]
        fingerprint = [
            {"path": str(path), "digest": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in plan_paths
        ]
        settings = {
            "output": str(self.config.paths.output_dir),
            "encoder": "libx264",
            "render_concurrency": 1,
        }
        job = self.store.get_job(job_id)
        if self._stage_reusable(job, "render", fingerprint, settings):
            return cast(dict[str, Any], job["stages"]["render"]["result"])

        def operation() -> dict[str, Any]:
            current = self.store.get_job(job_id)
            output = self._configured_destination(self.config.paths.output_dir, job_id)
            plans = [load_plan(path) for path in plan_paths]
            estimate = max(
                _MIN_WRITE_BYTES,
                max(
                    _duration_seconds(plan)
                    * plan.output.width
                    * plan.output.height
                    // 8
                    for plan in plans
                ),
            )
            self._destination_volume(
                output, estimate, self._expected_destination_volume(current, "output")
            )
            output.mkdir(parents=True, exist_ok=True)
            for path, plan in zip(plan_paths, plans, strict=True):
                name = (
                    "long.mp4"
                    if plan.output.width == 1920 and plan.output.height == 1080
                    else "short-01.mp4"
                )
                output_path = output / name
                metadata = {"plan": str(path), "warnings": []}
                # Persist expected final before render. Resume can recover a renamed
                # final even if process died before normal artifact persistence.
                self.store.save_artifact(job_id, "render", output_path, metadata)
            preserved = self._valid_render_results(current, plan_paths)
            preserved.update(
                {
                    path: result
                    for path, result in self._recover_render_results(
                        job_id, plan_paths
                    ).items()
                    if path not in preserved
                }
            )
            results: list[dict[str, Any]] = []
            for path in plan_paths:
                existing = preserved.get(path.resolve())
                if existing is not None:
                    results.append(existing)
                    continue
                results.append(self._render_plan(job_id, path))
            result = {"outputs": results, "estimated_bytes": estimate}
            self.store.complete_stage(job_id, "render", result)
            return result

        return self._run_stage(
            job_id, "render", fingerprint, settings, ErrorCategory.RENDER, operation
        )

    def _validate_recorded_outputs(self, rendered: dict[str, Any]) -> bool:
        try:
            for item in rendered.get("outputs", []):
                plan = load_plan(Path(item["plan"]))
                validate_output(
                    Path(item["output"]), plan.output, timeline_duration(plan)
                )
        except (
            KeyError,
            OSError,
            VideoEditorError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            return False
        return True

    def _ensure_validate(self, job_id: str, rendered: dict[str, Any]) -> dict[str, Any]:
        fingerprint = rendered.get("outputs", [])
        settings = {"tolerance": "0.20"}
        job = self.store.get_job(job_id)
        stage = job.get("stages", {}).get("validate")
        input_hash, settings_hash = self._stage_keys(fingerprint, settings)
        if (
            stage
            and stage.get("status") == StageStatus.COMPLETED
            and stage.get("input_fingerprint") == input_hash
            and stage.get("settings_hash") == settings_hash
            and stage.get("implementation_version") == IMPLEMENTATION_VERSION
            and self._validate_recorded_outputs(rendered)
        ):
            return cast(dict[str, Any], stage["result"])

        def operation() -> dict[str, Any]:
            checked: list[dict[str, Any]] = []
            for item in rendered.get("outputs", []):
                plan = load_plan(Path(item["plan"]))
                probe = validate_output(
                    Path(item["output"]), plan.output, timeline_duration(plan)
                )
                checked.append(
                    {
                        "path": item["output"],
                        "width": probe.video.width if probe.video else None,
                        "height": probe.video.height if probe.video else None,
                        "duration": probe.duration,
                        "video_codec": probe.video.codec_name if probe.video else None,
                        "audio_codec": probe.audio.codec_name if probe.audio else None,
                    }
                )
            result = {"outputs": checked}
            self.store.complete_stage(job_id, "validate", result)
            return result

        return self._run_stage(
            job_id, "validate", fingerprint, settings, ErrorCategory.OUTPUT, operation
        )

    def _report_state(
        self,
        job_id: str,
        inspected: dict[str, Any],
        planned: dict[str, Any],
        rendered: dict[str, Any],
        validated: dict[str, Any],
    ) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        selected: list[dict[str, Any]] = []
        for plan_path in planned.get("plans", []):
            plan = load_plan(Path(plan_path))
            selected.extend(
                {
                    "plan": str(plan_path),
                    "source_id": clip.source_id,
                    "source_start": str(clip.source_start),
                    "source_end": str(clip.source_end),
                    "selection_reason": clip.selection_reason,
                }
                for clip in plan.clips
            )
        stage_times: dict[str, dict[str, Any]] = {}
        for name, stage in job.get("stages", {}).items():
            duration: float | None = None
            if stage.get("started_at") and stage.get("completed_at"):
                duration = (
                    datetime.fromisoformat(stage["completed_at"])
                    - datetime.fromisoformat(stage["started_at"])
                ).total_seconds()
            stage_times[name] = {
                "started_at": stage.get("started_at"),
                "completed_at": stage.get("completed_at"),
                "duration_seconds": duration,
            }
        probe_warnings = inspected.get("warnings", [])
        chronology_warnings: list[dict[str, Any]] = []
        for group in inspected.get("chronology", job.get("chronology", [])):
            group_id = group.get("group_id")
            for warning in group.get("warnings", []):
                chronology_warnings.append(
                    {"group_id": group_id, "source_id": None, "warning": warning}
                )
            for member in group.get("members", []):
                if not isinstance(member, dict):
                    continue
                for warning in member.get("warnings", []):
                    chronology_warnings.append(
                        {
                            "group_id": group_id,
                            "source_id": member.get("source_id"),
                            "warning": warning,
                        }
                    )
        fallback_warnings = [
            warning
            for output in rendered.get("outputs", [])
            for warning in output.get("warnings", [])
        ]
        estimate = max(_MIN_WRITE_BYTES, int(rendered.get("estimated_bytes", 0)))
        return {
            **job,
            "selected_moments": selected,
            "output": {"outputs": validated.get("outputs", [])},
            "skipped_inputs": inspected.get("skipped_inputs", []),
            "warnings": [*probe_warnings, *chronology_warnings],
            "fallbacks": fallback_warnings,
            "chronology_warnings": chronology_warnings,
            "stage_times": stage_times,
            "storage_roots": {
                key: str(value) for key, value in vars(self.config.paths).items()
            },
            "estimated_peak_space_bytes": estimate,
            "estimated_peak_space_scope": (
                "render-output byte growth only; workspace and cache excluded"
            ),
            "cloud_usage": 0,
        }

    def _write_report_stage(
        self, job_id: str, report_state: dict[str, Any], *, started: bool = False
    ) -> dict[str, Any]:
        if not started:
            self._start(
                job_id,
                "report",
                report_state.get("output", {}),
                {"output": str(self.config.paths.output_dir)},
            )
        report_root = self._configured_destination(self.config.paths.output_dir, job_id)
        try:
            job = self.store.get_job(job_id)
            self._destination_volume(
                report_root,
                max(_MIN_WRITE_BYTES, len(json.dumps(report_state, default=str))),
                self._expected_destination_volume(job, "output"),
            )
            report_root.mkdir(parents=True, exist_ok=True)
            json_path = report_root / "report.json"
            md_path = report_root / "report.md"
            write_json_report(report_state, json_path)
            write_markdown_report(report_state, md_path)
            self.store.save_artifact(job_id, "report", json_path)
            self.store.save_artifact(job_id, "report", md_path)
            result = {"artifacts": [str(json_path), str(md_path)]}
            self.store.complete_stage(job_id, "report", result)
            return result
        except VideoEditorError as exc:
            self.store.fail_stage(job_id, "report", exc.category, str(exc))
            raise
        except Exception as exc:
            self.store.fail_stage(job_id, "report", ErrorCategory.OUTPUT, str(exc))
            raise VideoEditorError(
                ErrorCategory.OUTPUT, f"cannot write report: {exc}"
            ) from exc

    def _ensure_report(self, job_id: str, state: dict[str, Any]) -> dict[str, Any]:
        fingerprint = state.get("output", {})
        settings = {"output": str(self.config.paths.output_dir)}
        job = self.store.get_job(job_id)
        if self._stage_reusable(job, "report", fingerprint, settings):
            return cast(dict[str, Any], job["stages"]["report"]["result"])
        return self._run_stage(
            job_id,
            "report",
            fingerprint,
            settings,
            ErrorCategory.OUTPUT,
            lambda: self._write_report_stage(job_id, state, started=True),
        )

    def _validate_recorded_destinations(self, job: dict[str, Any]) -> None:
        for name, root in (
            ("workspace", self.config.paths.workspace_dir),
            ("cache", self.config.paths.cache_dir),
            ("output", self.config.paths.output_dir),
        ):
            self._destination_volume(
                root, _MIN_WRITE_BYTES, self._expected_destination_volume(job, name)
            )

    def _execute(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        input_path = Path(job["config"]["input_path"])
        source_volume = _volume(job["volume"])
        current_source_volume = inspect_volume(input_path)
        if current_source_volume != source_volume:
            raise VideoEditorError(
                ErrorCategory.STORAGE,
                f"source volume changed for {input_path}: expected {source_volume}, found {current_source_volume}",
            )
        self._validate_recorded_destinations(job)
        inspected = self._ensure_inspect(job_id, input_path)
        self._ensure_proxy(job_id)
        planned = self._ensure_plan(job_id)
        rendered = self._ensure_render(job_id, planned)
        validated = self._ensure_validate(job_id, rendered)
        report_state = self._report_state(
            job_id, inspected, planned, rendered, validated
        )
        reports = self._ensure_report(job_id, report_state)
        self.store.complete_job(job_id)
        return {
            "job_id": job_id,
            "outputs": rendered.get("outputs", []),
            "reports": reports.get("artifacts", []),
        }

    def render_from_plan(self, plan_path: Path) -> dict[str, Any]:
        self._require_open_store()
        plan_path = Path(plan_path)
        plan = load_plan(plan_path)
        self._preflight_plan_sources(plan)
        job_id = self._new_job(plan.sources[0].path.parent)
        output = self._configured_destination(self.config.paths.output_dir, job_id)
        fingerprint = {
            "path": str(plan_path),
            "digest": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        }
        settings = {"output": str(output), "encoder": "libx264"}

        def operation() -> dict[str, Any]:
            job = self.store.get_job(job_id)
            estimate = max(
                _MIN_WRITE_BYTES,
                _duration_seconds(plan) * plan.output.width * plan.output.height // 8,
            )
            self._destination_volume(
                output,
                max(_MIN_WRITE_BYTES, estimate),
                self._expected_destination_volume(job, "output"),
            )
            output.mkdir(parents=True, exist_ok=True)
            result = self._render_plan(job_id, plan_path)
            self.store.complete_stage(job_id, "render", result)
            return result

        result = self._run_stage(
            job_id, "render", fingerprint, settings, ErrorCategory.RENDER, operation
        )
        return {"job_id": job_id, **result}

    def run(self, input_path: Path) -> dict[str, Any]:
        self._require_open_store()
        input_path = Path(input_path)
        job_id = self._new_job(input_path)
        return self._execute(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        self._require_open_store()
        try:
            return self.store.get_job(job_id)
        except KeyError as exc:
            raise VideoEditorError(ErrorCategory.STATE, str(exc)) from exc

    def resume(self, job_id: str) -> dict[str, Any]:
        self._require_open_store()
        self.status(job_id)
        return self._execute(job_id)


def error_exit_code(error: VideoEditorError) -> int:
    """Map stable error category to documented CLI exit code."""
    return EXIT_CODES.get(error.category, 1)
