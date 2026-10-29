"""Broad-v3 metadata stays inside original Decimal bounds at nanosecond precision."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_broad_v3_timestamp_contract import _payload, _request

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.errors import VideoEditorError

D = Decimal


def _client(start, end):
    client = Mock()
    client.files.get.return_value = SimpleNamespace(
        name="files/offline",
        uri="gs://offline/proxy",
        mime_type="video/mp4",
        state="ACTIVE",
        expiration_time=None,
    )
    client.models.generate_content.return_value = SimpleNamespace(
        parsed=_payload(start, end),
        usage_metadata=None,
        response_id="offline",
    )
    return client


def _scan(client, start, end):
    return GeminiAdapter(client).broad_scan(
        SimpleNamespace(
            name="files/offline",
            uri="gs://offline/proxy",
            mime_type="video/mp4",
            manifest_id="manifest-1",
        ),
        SimpleNamespace(
            chunk_id=_request().chunk_id, proxy_start=D(start), proxy_end=D(end)
        ),
        _request(),
        prompt_version="broad-v3",
    )


@pytest.mark.parametrize(
    "start,end,serialized_start,serialized_end",
    [
        ("600", "628.2240000009", "600", "628.224"),
        ("600.0000000001", "628.224", "600.000000001", "628.224"),
        ("600.0000000005", "628.2240000005", "600.000000001", "628.224"),
        ("0", "720", "0", "720"),
    ],
)
def test_broad_v3_serializes_inward_and_accepts_advertised_bounds(
    start,
    end,
    serialized_start,
    serialized_end,
):
    client = _client(serialized_start, serialized_end)
    response = _scan(client, start, end).response

    sent = client.models.generate_content.call_args.kwargs
    metadata = sent["contents"][0].video_metadata
    assert metadata.start_offset == f"{serialized_start}s"
    assert metadata.end_offset == f"{serialized_end}s"
    assert (
        f"serialized interval: [{serialized_start}, {serialized_end}] seconds"
        in sent["contents"][1]
    )
    assert response.candidates[0].start >= D(start)
    assert response.candidates[0].end <= D(end)
    assert client.models.generate_content.call_count == 1


@pytest.mark.parametrize(
    "start,end", [("600.0000000001", "600.0000000009"), ("600", "600.0000000001")]
)
def test_collapsed_inward_interval_fails_before_file_activation(start, end):
    client = _client("600", "601")

    with pytest.raises(VideoEditorError) as caught:
        _scan(client, start, end)

    assert caught.value.code == "provider_invalid_request"
    client.files.get.assert_not_called()
    client.models.generate_content.assert_not_called()


def test_broad_v3_prompt_never_permits_rounded_endpoint_outside_raw_bound():
    client = _client("600", "628.224000001")

    with pytest.raises(VideoEditorError) as caught:
        _scan(client, "600", "628.2240000009")

    assert caught.value.code == "provider_response_invalid"
    sent = client.models.generate_content.call_args.kwargs
    assert "serialized interval: [600, 628.224] seconds" in sent["contents"][1]
    assert client.models.generate_content.call_count == 2
