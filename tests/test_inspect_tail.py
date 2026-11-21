import contextlib
import io
import struct

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport

from parquet_tools.commands.inspect import _execute_detail, _execute_simple
from parquet_tools.gen_py.parquet.ttypes import FileMetaData
from parquet_tools.parquet.credential import CredentialMismatch, InspectionCredential
from parquet_tools.parquet.tail import inspect_tail
from parquet_tools.testing.fake_s3 import FakeS3

CREATED_BY_A = 'parquet-cpp-arrow version 14.0.2'
CREATED_BY_B = 'parquet-cpp-arrow version 99.9.9'
LARGE_TAIL = 96 * 1024


def _parquet_bytes(rows: int = 3) -> bytes:
    df = pd.DataFrame({
        'one': list(range(rows)),
        'two': [f'value-{i}' for i in range(rows)],
        'three': [bool(i % 2) for i in range(rows)],
    })
    table = pa.Table.from_pandas(df)
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def _footer(data: bytes):
    length = struct.unpack('<i', data[-8:-4])[0]
    return len(data) - 8 - length, length


def _with_large_footer(data: bytes, created_by: str, marker: bytes) -> bytes:
    """Rebuild a valid file whose footer has a large tail after created_by."""
    footer_start, footer_length = _footer(data)
    footer = data[footer_start:footer_start + footer_length]
    metadata = FileMetaData()
    metadata.read(TCompactProtocol.TCompactProtocol(TTransport.TMemoryBuffer(footer)))
    metadata.created_by = created_by
    metadata.footer_signing_key_metadata = marker * (LARGE_TAIL // len(marker))
    out = TTransport.TMemoryBuffer()
    metadata.write(TCompactProtocol.TCompactProtocol(out))
    new_footer = out.getvalue()
    return (
        data[:footer_start]
        + new_footer
        + struct.pack('<i', len(new_footer))
        + b'PAR1'
    )


def _local_reports(data: bytes, tmp_path):
    path = tmp_path / 'source.parquet'
    path.write_bytes(data)
    simple = io.StringIO()
    detail = io.StringIO()
    with contextlib.redirect_stdout(simple):
        _execute_simple(str(path))
    with contextlib.redirect_stdout(detail):
        _execute_detail(str(path))
    return simple.getvalue().rstrip('\n'), detail.getvalue().rstrip('\n')


def _ranges(state: FakeS3):
    return state.read_intervals()


def _assert_footer_only(state: FakeS3, data: bytes):
    footer_start, _ = _footer(data)
    intervals = _ranges(state)
    for start, end in intervals:
        assert start >= footer_start, f'read data pages: bytes={start}-{end}'
        assert start < len(data) - 7
    # Never ask for the whole object.
    assert (0, len(data)) not in intervals


def test_simple_and_detail_match_local_without_data_pages(tmp_path):
    data = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    state = FakeS3()
    state.put('bucket', 'archive.parquet', data)
    expected_simple, expected_detail = _local_reports(data, tmp_path)

    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    assert simple.report.rstrip('\n') == expected_simple
    assert simple.detail_available is False
    # The simple report stops before the large footer tail.
    assert simple.credential.footer_complete() is False
    _assert_footer_only(state, data)

    state.clear_requests()
    detail = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert detail.detail_available is True
    assert detail.report.rstrip('\n') == expected_detail
    _assert_footer_only(state, data)


def test_credential_round_trip_reuses_only_verified_ranges(tmp_path):
    data = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    state = FakeS3()
    state.put('bucket', 'archive.parquet', data)
    path = str(tmp_path / 'credential.json')

    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    simple.credential.save(path)
    state.clear_requests()

    loaded = InspectionCredential.load(path)
    detail = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=loaded,
    )
    assert detail.detail_available is True
    assert detail.report.rstrip('\n') == _local_reports(data, tmp_path)[1]
    # The detail phase starts at the verified prefix, not the footer start.
    footer_start, _ = _footer(data)
    first_start = _ranges(state)[0][0]
    assert first_start == footer_start + simple.credential.known_prefix_length
    _assert_footer_only(state, data)


def _same_shape_versions(tmp_path):
    original = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    replacement = _with_large_footer(_parquet_bytes(), CREATED_BY_B, b'B')
    assert len(original) == len(replacement)
    assert _footer(original)[1] == _footer(replacement)[1]
    return original, replacement


def test_same_length_replacement_with_truncated_original_response(tmp_path):
    original, replacement = _same_shape_versions(tmp_path)
    state = FakeS3()
    version = state.put('bucket', 'archive.parquet', original)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    assert simple.detail_available is False
    before = simple.credential.known_prefix_length

    detail_start = _footer(original)[0] + before
    state.add_fault(
        version_id=version.version_id, start=detail_start,
        mode='truncate_once',
    )
    first = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert first.detail_available is False
    assert 'detail unavailable' in first.report
    assert first.report.startswith(simple.report.rstrip('\n'))
    verified = first.credential.known_prefix_length
    assert verified > before

    # Replace the object with a same-size, same-footer-length new version.
    new_version = state.replace('bucket', 'archive.parquet', replacement)
    assert new_version.version_id != version.version_id

    state.clear_requests()
    resumed = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert resumed.detail_available is True
    assert resumed.report.rstrip('\n') == _local_reports(original, tmp_path)[1]
    assert CREATED_BY_B not in resumed.report
    assert resumed.identity.version_id == version.version_id
    # Resume continued from the verified prefix and read only the original
    # version, never the replacement's version id.
    assert _ranges(state)[0][0] == _footer(original)[0] + verified
    assert all(
        request.version_id == version.version_id
        for request in state.get_requests()
    )


def test_reliable_conditional_read_blocks_after_replacement(tmp_path):
    original, replacement = _same_shape_versions(tmp_path)
    state = FakeS3()
    version = state.put('bucket', 'archive.parquet', original)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')

    state.replace('bucket', 'archive.parquet', replacement)
    # Historic version ranges are unavailable, conditional reads work.
    state.version_range_supported = False
    state.conditional_supported = True
    state.clear_requests()

    blocked = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert blocked.detail_available is False
    assert blocked.report.startswith(simple.report.rstrip('\n'))
    assert 'detail unavailable' in blocked.report
    assert 'original_etag' in blocked.report
    assert CREATED_BY_B not in blocked.report
    # The original simple report and its version attribution were preserved.
    assert blocked.credential.identity.version_id == version.version_id
    assert 'num_rows: 3' in blocked.credential.simple_report
    assert blocked.credential.footer_complete() is False
    # It did try a conditional read of the original bytes.
    assert any(request.if_match for request in state.get_requests())


def test_reliable_conditional_read_completes_when_version_is_unchanged(tmp_path):
    data = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    state = FakeS3()
    state.put('bucket', 'archive.parquet', data)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    state.version_range_supported = False
    state.conditional_supported = True
    state.clear_requests()

    detail = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert detail.detail_available is True
    assert detail.report.rstrip('\n') == _local_reports(data, tmp_path)[1]
    reads = [r for r in state.get_requests() if r.method == 'GET']
    assert reads
    # The historic-version attempt is refused; the successful read must be
    # the If-Match conditional read (no unproven bytes were used).
    accepted = [r for r in reads if r.status == 206]
    assert accepted
    assert all(request.if_match for request in accepted)
    assert all(request.version_id is None for request in accepted)


def test_listed_version_without_range_is_not_treated_as_filled(tmp_path):
    original, replacement = _same_shape_versions(tmp_path)
    state = FakeS3()
    version = state.put('bucket', 'archive.parquet', original)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    state.replace('bucket', 'archive.parquet', replacement)
    state.version_range_supported = False
    state.conditional_supported = False
    state.clear_requests()

    listed = state.list_versions('bucket', 'archive.parquet')
    assert any(v['VersionId'] == version.version_id for v in listed)

    result = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert result.detail_available is False
    assert 'detail unavailable' in result.report
    assert result.credential.footer_complete() is False
    assert result.credential.missing_intervals()


def test_same_length_body_at_wrong_offset_is_rejected(tmp_path):
    data = _with_large_footer(_parquet_bytes(rows=6), CREATED_BY_A, b'A')
    state = FakeS3()
    version = state.put('bucket', 'archive.parquet', data)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    state.clear_requests()

    # Correct headers and length, but the body is shifted.  Only the
    # structural footer check can reject this.
    state.add_fault(version_id=version.version_id, mode='wrong_body', shift=7)
    result = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert result.detail_available is False
    assert 'detail unavailable' in result.report
    assert result.credential.footer_complete() is False

    state.faults.clear()
    state.clear_requests()
    retried = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert retried.detail_available is True
    assert retried.report.rstrip('\n') == _local_reports(data, tmp_path)[1]


def test_unchanged_short_read_resumes_without_refetching(tmp_path):
    data = _with_large_footer(_parquet_bytes(rows=4), CREATED_BY_A, b'A')
    state = FakeS3()
    state.put('bucket', 'archive.parquet', data)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    footer_start, _ = _footer(data)
    before = simple.credential.known_prefix_length
    first_start = footer_start + before
    _, footer_length = _footer(data)
    state.clear_requests()

    state.add_fault(
        start=first_start,
        end=footer_start + footer_length - 1,
        mode='truncate_once',
    )
    first = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
        range_chunk_size=64 * 1024,
    )
    assert first.detail_available is False
    assert 'detail unavailable' in first.report
    verified = first.credential.known_prefix_length
    assert verified > before

    state.clear_requests()
    resumed = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=first.credential,
        range_chunk_size=64 * 1024,
    )
    assert resumed.detail_available is True
    assert resumed.report.rstrip('\n') == _local_reports(data, tmp_path)[1]
    assert _ranges(state)[0][0] == footer_start + verified


def test_credential_cannot_be_used_for_another_object_or_endpoint(tmp_path):
    data = _parquet_bytes()
    state = FakeS3()
    state.put('bucket', 'one.parquet', data)
    state.put('bucket', 'two.parquet', data)
    simple = inspect_tail(state.client(), 'bucket', 'one.parquet')

    with pytest.raises(CredentialMismatch):
        inspect_tail(state.client(), 'bucket', 'two.parquet', credential=simple.credential)

    other_endpoint = FakeS3(endpoint='fake://other')
    other_endpoint.put('bucket', 'one.parquet', data)
    with pytest.raises(CredentialMismatch):
        inspect_tail(other_endpoint.client(), 'bucket', 'one.parquet', credential=simple.credential)


def test_local_and_explicit_full_download_paths_still_work(tmp_path):
    data = _parquet_bytes()
    expected, _ = _local_reports(data, tmp_path)
    state = FakeS3()
    state.put('bucket', 'archive.parquet', data)

    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    assert simple.report.rstrip('\n') == expected


def test_range_capability_not_assumed_from_listing(tmp_path):
    original, replacement = _same_shape_versions(tmp_path)
    state = FakeS3()
    state.put('bucket', 'archive.parquet', original)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    state.replace('bucket', 'archive.parquet', replacement)
    state.range_supported = False
    state.clear_requests()

    result = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert result.detail_available is False
    assert 'detail unavailable' in result.report
    assert result.credential.footer_complete() is False
    assert CREATED_BY_B not in result.report


def test_non_versioned_service_resumes_only_with_conditional_reads(tmp_path):
    data = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    state = FakeS3(versioned=False)
    state.put('bucket', 'archive.parquet', data)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')
    assert simple.identity.version_id is None
    state.clear_requests()

    detail = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert detail.detail_available is True
    assert detail.report.rstrip('\n') == _local_reports(data, tmp_path)[1]
    reads = [r for r in state.get_requests() if r.method == 'GET']
    assert reads
    # With no VersionId, resumption must prove the bytes with If-Match.
    assert all(request.if_match == simple.identity.etag for request in reads)
    assert all(request.version_id is None for request in reads)


def test_non_versioned_service_without_conditions_marks_detail_unavailable(tmp_path):
    data = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    state = FakeS3(versioned=False)
    state.put('bucket', 'archive.parquet', data)
    state.conditional_supported = False
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')

    detail = inspect_tail(
        state.client(), 'bucket', 'archive.parquet',
        detail=True, credential=simple.credential,
    )
    assert detail.detail_available is False
    assert detail.report.startswith(simple.report.rstrip('\n'))
    assert 'detail unavailable' in detail.report
    assert detail.credential.footer_complete() is False


def test_tampered_credential_bytes_are_rejected(tmp_path):
    import base64

    data = _with_large_footer(_parquet_bytes(), CREATED_BY_A, b'A')
    state = FakeS3()
    state.put('bucket', 'archive.parquet', data)
    simple = inspect_tail(state.client(), 'bucket', 'archive.parquet')

    raw = simple.credential.to_dict()
    payload = bytearray(base64.b64decode(raw['ranges'][0]['data']))
    payload[0] ^= 0xFF
    raw['ranges'][0]['data'] = base64.b64encode(bytes(payload)).decode()
    tampered = InspectionCredential.from_dict(raw)

    from parquet_tools.parquet.tail import InspectionError
    with pytest.raises(InspectionError):
        inspect_tail(
            state.client(), 'bucket', 'archive.parquet',
            detail=True, credential=tampered,
        )
