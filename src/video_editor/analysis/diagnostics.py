"""Bounded validation metadata without provider values or dynamic keys."""

from video_editor.analysis.models import BroadScanResponse, CandidateRefinementResponse

VALIDATION_FIELDS = (
    set(BroadScanResponse.model_fields)
    | set(CandidateRefinementResponse.model_fields)
    | {
        "time",
        "x",
        "y",
        "w",
        "h",
        "priority",
        "scene_id",
        "summary",
        "actions",
        "setting",
        "scenic_interest",
        "human_interaction",
        "story_milestone",
        "technical_problems",
        "vertical_suitability",
        "confidence",
        "category",
        "reason",
    }
)
_VALIDATION_TYPES = {
    "value_error",
    "missing",
    "extra_forbidden",
    "tuple_type",
    "list_type",
    "model_type",
    "model_attributes_type",
    "string_type",
    "string_too_short",
    "float_type",
    "float_parsing",
    "int_type",
    "int_parsing",
    "bool_type",
    "finite_number",
    "greater_than",
    "greater_than_equal",
    "less_than",
    "less_than_equal",
    "literal_error",
    "decimal_type",
    "decimal_parsing",
    "json_invalid",
    "json_type",
    "unknown",
}


def validation_errors(entries: object) -> list[dict[str, object]]:
    """Retain only allowlisted type codes and bounded structural locations."""
    if not isinstance(entries, (list, tuple)):
        return []
    sanitized: list[dict[str, object]] = []
    for entry in entries[:16]:
        if not isinstance(entry, dict):
            continue
        error_type = entry.get("type")
        if not isinstance(error_type, str) or error_type not in _VALIDATION_TYPES:
            error_type = "unknown"
        raw_location = entry.get("loc")
        location: list[str | int] = []
        if isinstance(raw_location, (list, tuple)):
            for component in raw_location[:8]:
                if (
                    isinstance(component, str)
                    and component in VALIDATION_FIELDS
                    or type(component) is int
                    and 0 <= component <= 1_000_000
                ):
                    location.append(component)
                else:
                    location.append("unknown")
        sanitized.append({"type": error_type, "loc": location})
    return sanitized
