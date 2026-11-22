"""Acceptance tests for the offline archive review tooling.

The scenario, built from minimal plain-text WARC volumes:

* one URI, one UTC second, five distinct representations across two volumes;
* two byte-identical independent copies share one selection id;
* same body with a different Content-Type gets its own selection id;
* two genuinely different responses whose CDX digest and WARC-Record-ID
  fields are filled in identically stay separate, because the volumes read
  out different bytes;
* two index claims are unreadable (missing volume, offset past end of file);
* the archives are then repacked into a new volume with new offsets, one old
  coordinate is reused by another response, and the two index sources swap
  order.

Everything runs offline: replay only opens coordinates registered for the
selection id or proven by a migration ledger.
"""

import os
from io import BytesIO

import pytest

from pywb.audit import (
    EvidenceMissing,
    Manifest,
    MigrationLedger,
    Replayer,
    VolumeCatalogue,
    apply_relocations,
    build_manifest,
    plan_relocations,
    read_evidence,
    verify_manifest_offline,
)
from pywb.audit.test.fixtures import (
    C_CLAIMED_DIGEST,
    C_CLAIMED_RECORD_ID,
    TIMESTAMP,
    URI,
    VOLUME_A,
    VOLUME_B,
    build_initial_archive,
    volume_resolver,
)
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


@pytest.fixture()
def archive(tmp_path):
    return build_initial_archive(str(tmp_path))


def _index_paths(archive, swapped=False):
    if swapped:
        return [archive['index_b'], archive['index_a']]
    return [archive['index_a'], archive['index_b']]


def _build(archive, swapped=False):
    return build_manifest(
        _index_paths(archive, swapped),
        volume_resolver(archive['warcs']),
        uri=URI)


# ---------------------------------------------------------------------------
# first manifest: the three difference classes
# ---------------------------------------------------------------------------

class TestFirstManifest:
    def test_five_verified_representations_and_two_unreadable(self, archive):
        manifest = _build(archive)
        verified = manifest.verified_entries()
        unverifiable = manifest.unverifiable_entries()

        assert len(manifest.entries) == 7
        assert len(verified) == 5
        assert len(unverifiable) == 2

    def test_identical_copies_share_one_identifier_and_two_candidates(
            self, archive):
        manifest = _build(archive)
        html_entries = [
            entry for entry in manifest.verified_entries()
            if entry['mime'] == 'text/html'
            and entry['payload_digest'] == archive['digests']['a']]
        assert len(html_entries) == 1
        entry = html_entries[0]
        assert len(entry['candidates']) == 2
        volumes = {c['volume'] for c in entry['candidates']}
        assert volumes == {archive['volume_a'], archive['volume_b']}
        # independent copies: distinct WARC-Record-IDs, identical representation
        record_ids = {c['warc_record_id'] for c in entry['candidates']}
        assert len(record_ids) == 2
        assert len({c['representation_digest'] for c in entry['candidates']}) == 1
        assert all(c['verified'] for c in entry['candidates'])

    def test_same_body_different_content_type_stays_separate(self, archive):
        manifest = _build(archive)
        entries = [
            entry for entry in manifest.verified_entries()
            if entry['payload_digest'] == archive['digests']['b']]
        assert len(entries) == 2
        mimes = {entry['mime'] for entry in entries}
        assert mimes == {'text/html', 'application/xhtml+xml'}
        # same body digest, but the representations are not interchangeable
        assert entries[0]['payload_digest'] == entries[1]['payload_digest']
        assert (entries[0]['representation_digest']
                != entries[1]['representation_digest'])
        assert entries[0]['header_digest'] != entries[1]['header_digest']
        assert len({entry['selection_id'] for entry in entries}) == 2

    def test_identical_cdx_claims_but_different_bytes_stay_separate(
            self, archive):
        manifest = _build(archive)
        c_entries = [
            entry for entry in manifest.verified_entries()
            if entry['payload_digest'] in (
                archive['digests']['c1'], archive['digests']['c2'])]
        assert len(c_entries) == 2
        digests = {entry['payload_digest'] for entry in c_entries}
        assert digests == {archive['digests']['c1'],
                           archive['digests']['c2']}
        for entry in c_entries:
            candidate = entry['candidates'][0]
            assert candidate['claimed_digest'] == C_CLAIMED_DIGEST
            assert candidate['claimed_warc_record_id'] == C_CLAIMED_RECORD_ID
            # the volume contradicts both index claims, yet is fully readable
            mismatch = ' '.join(candidate['index_claim_mismatch'])
            assert 'contradicted by bytes read' in mismatch
            assert candidate['warc_record_id'] != C_CLAIMED_RECORD_ID
            assert candidate['verified'] is True

    def test_unreadable_index_claims_are_listed_not_guessed(self, archive):
        manifest = _build(archive)
        for entry in manifest.unverifiable_entries():
            assert entry['verified'] is False
            assert entry['representation_digest'] is None
            assert entry['reason']
            candidate = entry['candidates'][0]
            assert candidate['verified'] is False
        reasons = ' '.join(
            entry['reason'] for entry in manifest.unverifiable_entries())
        assert 'volume not readable' in reasons
        assert 'no response record at offset' in reasons

    def test_existing_file_is_not_substitute_for_read_evidence(
            self, archive):
        # create an empty file where the missing volume was claimed to be
        missing_path = os.path.join(archive['warcs'], 'missing-volume.warc')
        open(missing_path, 'wb').close()
        assert os.path.isfile(missing_path)

        manifest = _build(archive)
        entry = next(
            entry for entry in manifest.unverifiable_entries()
            if entry['candidates'][0]['volume'] == missing_path)
        assert entry['verified'] is False
        assert 'no response record' in entry['reason']

    def test_selection_ids_stable_when_index_sources_swap_order(
            self, archive):
        first = _build(archive)
        swapped = _build(archive, swapped=True)

        first_ids = {
            (entry['selection_id'], entry['representation_digest'])
            for entry in first.verified_entries()}
        swapped_ids = {
            (entry['selection_id'], entry['representation_digest'])
            for entry in swapped.verified_entries()}
        assert first_ids == swapped_ids

    def test_manifest_roundtrip(self, archive, tmp_path):
        manifest = _build(archive)
        path = str(tmp_path / 'manifest.json')
        manifest.save(path)
        reloaded = Manifest.load(path)

        assert [entry['selection_id'] for entry in reloaded.entries] == \
               [entry['selection_id'] for entry in manifest.entries]
        assert reloaded.get(manifest.entries[0]['selection_id'])[
            'representation_digest'] == \
            manifest.entries[0]['representation_digest']

    def test_offline_entry_by_entry_verification(self, archive):
        manifest = _build(archive)
        report = verify_manifest_offline(manifest)
        assert len(report) == len(manifest.entries)

        verified_checks = [
            check for item in report for check in item['candidate_checks']]
        outcomes = {check['outcome'] for check in verified_checks}
        assert outcomes <= {'matches', 'unreadable'}
        assert {item['selection_id'] for item in report if item['verified']} \
            == {entry['selection_id'] for entry in manifest.verified_entries()}


# ---------------------------------------------------------------------------
# replay: only registered or migration-proven coordinates, no URL re-scan
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_returns_actual_headers_and_body(self, archive):
        manifest = _build(archive)
        replayer = Replayer(manifest)

        b_html = next(entry for entry in manifest.verified_entries()
                      if entry['mime'] == 'text/html'
                      and entry['payload_digest'] == archive['digests']['b'])
        result = replayer.replay(b_html['selection_id'])
        assert result['status'] == '200 OK'
        assert ('Content-Type', 'text/html') in result['headers']
        assert result['body'] == archive['bodies']['b']
        assert result['representation_digest'] == \
            b_html['representation_digest']
        assert result['coordinate']['origin'] == 'registered'

        b_xhtml = next(entry for entry in manifest.verified_entries()
                       if entry['mime'] == 'application/xhtml+xml')
        xhtml_result = replayer.replay(b_xhtml['selection_id'])
        assert ('Content-Type', 'application/xhtml+xml') in \
            xhtml_result['headers']
        assert xhtml_result['body'] == archive['bodies']['b']

    def test_replay_only_opens_registered_coordinates(self, archive,
                                                      monkeypatch):
        manifest = _build(archive)
        replayer = Replayer(manifest)
        opened = []

        import pywb.audit.replay as replay_module

        def spy_read(volume, offset, length=None):
            opened.append((volume, offset))
            return read_evidence(volume, offset, length)

        monkeypatch.setattr(replay_module, 'read_evidence', spy_read)
        entry = manifest.verified_entries()[0]
        replayer.replay(entry['selection_id'])

        registered = {
            (candidate['volume'], candidate['offset'])
            for candidate in entry['candidates']}
        assert set(opened) <= registered

    def test_replay_unknown_and_unverifiable_fail_without_fallback(
            self, archive):
        manifest = _build(archive)
        replayer = Replayer(manifest)

        with pytest.raises(EvidenceMissing):
            replayer.replay('sel-does-not-exist')

        unreadable = manifest.unverifiable_entries()[0]
        with pytest.raises(EvidenceMissing) as exc_info:
            replayer.replay(unreadable['selection_id'])
        assert exc_info.value.candidates

    def test_replay_refuses_offset_reused_by_another_response(
            self, archive):
        # index-a only: the selected representation has exactly one
        # registered coordinate (volume-a at offset 0)
        manifest = build_manifest(
            [archive['index_a']], volume_resolver(archive['warcs']),
            uri=URI)
        entry_a = next(
            entry for entry in manifest.verified_entries()
            if entry['payload_digest'] == archive['digests']['a'])
        assert len(entry_a['candidates']) == 1
        old_coordinate = entry_a['candidates'][0]
        assert old_coordinate['volume'] == archive['volume_a']
        old_offset = old_coordinate['offset']

        # repack in place: another response now occupies the old offset
        self._overwrite_with_different_response(
            archive['volume_a'], archive['bodies']['c2'])

        evidence = read_evidence(archive['volume_a'], old_offset)
        assert evidence['representation_digest'] != \
            entry_a['representation_digest']

        replayer = Replayer(manifest)
        with pytest.raises(EvidenceMissing) as exc_info:
            replayer.replay(entry_a['selection_id'])
        attempts = exc_info.value.candidates
        outcomes = {attempt['outcome'] for attempt in attempts}
        assert 'representation-mismatch' in outcomes
        # the other response must never be returned as the selected one
        for attempt in attempts:
            assert attempt.get('actual_digest') != \
                entry_a['representation_digest']

    @staticmethod
    def _overwrite_with_different_response(path, body):
        with open(path, 'wb') as stream:
            writer = WARCWriter(stream, gzip=False)
            http_headers = StatusAndHeaders(
                '200 OK',
                [('Content-Type', 'text/plain'),
                 ('Content-Length', str(len(body)))],
                protocol='HTTP/1.1')
            record = writer.create_warc_record(
                URI, 'response', payload=BytesIO(body),
                http_headers=http_headers,
                warc_headers_dict={
                    'WARC-Record-ID':
                        '<urn:uuid:c2222222-0000-0000-0000-000000000002>',
                    'WARC-Date': '2026-11-23T00:00:00Z'})
            writer.write_record(record)


# ---------------------------------------------------------------------------
# repack: new volume, new offsets, mixed stage, proven vs missing evidence
# ---------------------------------------------------------------------------

def _write_repacked_volume(path, records):
    """Write records (from scan_volume evidence) into one new WARC, reordered."""
    with open(path, 'wb') as stream:
        writer = WARCWriter(stream, gzip=False)
        for record in records:
            http_headers = StatusAndHeaders(
                record['status'], list(record['headers']),
                protocol='HTTP/1.1')
            warc = writer.create_warc_record(
                record['target_uri'], 'response',
                payload=BytesIO(record['body']),
                http_headers=http_headers,
                warc_headers_dict={
                    'WARC-Record-ID': record['warc_record_id'],
                    'WARC-Date': record['warc_date']})
            writer.write_record(warc)


class TestRepackMigration:
    def _repack(self, archive, tmp_path):
        from pywb.audit.evidence import scan_volume
        records = []
        for volume in (archive['volume_a'], archive['volume_b']):
            records.extend(scan_volume(volume))
        # one record per distinct representation, deliberately reordered so
        # every offset differs from the original coordinates
        seen = set()
        unique = []
        for record in records:
            if record['representation_digest'] in seen:
                continue
            seen.add(record['representation_digest'])
            unique.append(record)
        reordered = list(reversed(unique))

        new_volume = str(tmp_path / 'volume-c.warc')
        _write_repacked_volume(new_volume, reordered)
        return new_volume, VolumeCatalogue.scan(new_volume)

    def test_relocation_verified_by_digest_and_old_offset_reused(
            self, archive, tmp_path):
        manifest = _build(archive)
        new_volume, catalogue = self._repack(archive, tmp_path)

        # every new offset differs from the old coordinates
        old_offsets = {
            (candidate['volume'], candidate['offset'])
            for entry in manifest.verified_entries()
            for candidate in entry['candidates']}
        new_offsets = {(record['volume'], record['offset'])
                       for record in catalogue.records}
        assert not (old_offsets & new_offsets)

        ledger = plan_relocations(manifest, [catalogue], stage='relocated')
        assert ledger.stage == 'relocated'
        assert all(record['verified']
                   for record in ledger.records.values()
                   if record['representation_digest'])

        # the old first offset is reused by a different response after the
        # old volume is replaced in place
        entry_a = next(
            entry for entry in manifest.verified_entries()
            if entry['payload_digest'] == archive['digests']['a'])
        old_offset = entry_a['candidates'][0]['offset']
        TestReplay._overwrite_with_different_response(
            archive['volume_a'], archive['bodies']['c2'])
        assert read_evidence(archive['volume_a'], old_offset)[
            'representation_digest'] != entry_a['representation_digest']

        rebuilt = apply_relocations(manifest, ledger)
        moved = rebuilt.get(entry_a['selection_id'])
        assert moved['representation_digest'] == \
            entry_a['representation_digest']
        assert moved['candidates'][0]['volume'] == new_volume
        assert moved['candidates'][0]['migrated_from']

        # old id still opens the relocated copy and re-verifies the bytes
        replayer = Replayer(rebuilt, ledger)
        result = replayer.replay(entry_a['selection_id'])
        assert result['body'] == archive['bodies']['a']
        assert result['representation_digest'] == \
            entry_a['representation_digest']
        assert result['coordinate']['volume'] == new_volume

    def test_new_manifest_keeps_representations_and_uncheckable_separate(
            self, archive, tmp_path):
        manifest = _build(archive)
        _, catalogue = self._repack(archive, tmp_path)
        ledger = plan_relocations(manifest, [catalogue])
        rebuilt = apply_relocations(manifest, ledger)

        assert len(rebuilt.verified_entries()) == 5
        assert len(rebuilt.unverifiable_entries()) == 2
        before = {
            (entry['selection_id'], entry['representation_digest'])
            for entry in manifest.entries}
        after = {
            (entry['selection_id'], entry['representation_digest'])
            for entry in rebuilt.entries}
        assert before == after

    def test_mixed_stage_old_index_plus_new_catalogue_still_verifies(
            self, archive, tmp_path):
        manifest = _build(archive)
        _, catalogue = self._repack(archive, tmp_path)
        ledger = plan_relocations(manifest, [catalogue], stage='mixed')
        assert ledger.stage == 'mixed'

        entry = manifest.verified_entries()[0]
        coordinate = ledger.verified_coordinate(entry['selection_id'])
        assert coordinate is not None
        evidence = read_evidence(coordinate[0], coordinate[1])
        assert evidence['representation_digest'] == \
            entry['representation_digest']

        # replay during the switch may use old index entries plus the new
        # volume catalogue; the ledger is exactly that evidence channel
        replayer = Replayer(manifest, ledger)
        result = replayer.replay(entry['selection_id'])
        assert result['representation_digest'] == \
            entry['representation_digest']

    def test_mixed_stage_without_proof_is_retryable_evidence_missing(
            self, archive, tmp_path):
        # index-a only so representation A has one registered coordinate,
        # which the repack will invalidate by reusing its offset
        manifest = build_manifest(
            [archive['index_a']], volume_resolver(archive['warcs']),
            uri=URI)
        entry_a = next(
            entry for entry in manifest.verified_entries()
            if entry['payload_digest'] == archive['digests']['a'])

        # replacement catalogue that does not contain representation A
        replacement = str(tmp_path / 'replacement-a.warc')
        with open(replacement, 'wb') as stream:
            writer = WARCWriter(stream, gzip=False)
            http_headers = StatusAndHeaders(
                '200 OK',
                [('Content-Type', 'text/plain'),
                 ('Content-Length', str(len(archive['bodies']['c2'])))],
                protocol='HTTP/1.1')
            record = writer.create_warc_record(
                URI, 'response',
                payload=BytesIO(archive['bodies']['c2']),
                http_headers=http_headers,
                warc_headers_dict={
                    'WARC-Record-ID':
                        '<urn:uuid:c2222222-0000-0000-0000-000000000002>',
                    'WARC-Date': '2026-11-23T00:00:00Z'})
            writer.write_record(record)

        catalogue = VolumeCatalogue.scan(replacement)
        ledger = plan_relocations(manifest, [catalogue], stage='mixed')
        record = ledger.get(entry_a['selection_id'])
        assert record['verified'] is False
        assert record['reason']
        assert ledger.verified_coordinate(entry_a['selection_id']) is None

        # old coordinate reused by the other response: replay must refuse,
        # not return the other representation and not pick a default
        TestReplay._overwrite_with_different_response(
            archive['volume_a'], archive['bodies']['c2'])
        replayer = Replayer(manifest, ledger)
        with pytest.raises(EvidenceMissing) as exc_info:
            replayer.replay(entry_a['selection_id'])
        attempts = exc_info.value.candidates
        assert all(attempt['outcome'] != 'matches' for attempt in attempts)
        assert all(
            attempt.get('actual_digest') != entry_a['representation_digest']
            for attempt in attempts if 'actual_digest' in attempt)

    def test_missing_catalogue_volume_is_retryable(self, archive):
        manifest = _build(archive)
        with pytest.raises(EvidenceMissing):
            VolumeCatalogue.scan(
                os.path.join(archive['warcs'], 'never-written.warc'))

    def test_ledger_roundtrip(self, archive, tmp_path):
        manifest = _build(archive)
        _, catalogue = self._repack(archive, tmp_path)
        ledger = plan_relocations(manifest, [catalogue])
        path = str(tmp_path / 'ledger.json')
        ledger.save(path)
        reloaded = MigrationLedger.load(path)
        entry = manifest.verified_entries()[0]
        assert reloaded.verified_coordinate(
            entry['selection_id']) == ledger.verified_coordinate(
            entry['selection_id'])

    def test_actual_returned_headers_and_body_after_repack(
            self, archive, tmp_path):
        manifest = _build(archive)
        _, catalogue = self._repack(archive, tmp_path)
        ledger = plan_relocations(manifest, [catalogue])
        rebuilt = apply_relocations(manifest, ledger)
        replayer = Replayer(rebuilt, ledger)

        xhtml = next(entry for entry in rebuilt.verified_entries()
                     if entry['mime'] == 'application/xhtml+xml')
        result = replayer.replay(xhtml['selection_id'])
        assert ('Content-Type', 'application/xhtml+xml') in result['headers']
        assert result['body'] == archive['bodies']['b']
        assert result['warc_record_id'] == \
            xhtml['candidates'][0]['claimed_warc_record_id']


# ---------------------------------------------------------------------------
# format and end-to-end checks
# ---------------------------------------------------------------------------

class TestFormatsAndEndToEnd:
    def test_gzipped_warc_volume_offsets_and_evidence(self, tmp_path):
        path = str(tmp_path / 'gzipped.warc.gz')
        with open(path, 'wb') as stream:
            writer = WARCWriter(stream, gzip=True)
            for token, content_type, body in [
                    ('11111111-0000-0000-0000-000000000001',
                     'text/plain', b'gzip body one'),
                    ('22222222-0000-0000-0000-000000000002',
                     'text/html', b'<p>gzip body two</p>')]:
                http_headers = StatusAndHeaders(
                    '200 OK',
                    [('Content-Type', content_type),
                     ('Content-Length', str(len(body)))],
                    protocol='HTTP/1.1')
                writer.write_record(writer.create_warc_record(
                    URI, 'response', payload=BytesIO(body),
                    http_headers=http_headers,
                    warc_headers_dict={
                        'WARC-Record-ID': '<urn:uuid:{0}>'.format(token),
                        'WARC-Date': '2026-11-23T00:00:00Z'}))

        records = list(VolumeCatalogue.scan(path).records)
        assert len(records) == 2
        assert records[0]['offset'] != records[1]['offset']

        for record in records:
            evidence = read_evidence(path, record['offset'])
            assert evidence['representation_digest'] == \
                record['representation_digest']
            assert evidence['body'] == record['body']

    def test_swap_order_and_repack_end_to_end(self, archive, tmp_path):
        # first manifest with the index sources in one order, then swapped:
        # the same five selection ids must come out
        first = _build(archive)
        swapped = _build(archive, swapped=True)
        first_ids = [entry['selection_id']
                     for entry in first.verified_entries()]
        swapped_ids = {entry['selection_id']
                       for entry in swapped.verified_entries()}
        assert set(first_ids) == swapped_ids

        # repack into a new volume at new offsets
        repack_case = TestRepackMigration()
        new_volume, catalogue = repack_case._repack(archive, tmp_path)
        ledger = plan_relocations(swapped, [catalogue], stage='relocated')
        rebuilt = apply_relocations(swapped, ledger)

        replayer = Replayer(rebuilt, ledger)
        for entry in rebuilt.verified_entries():
            result = replayer.replay(entry['selection_id'])
            assert result['representation_digest'] == \
                entry['representation_digest']
            assert result['coordinate']['volume'] == new_volume

        # every same-second representation is still listed separately and
        # the uncheckable claims are still present
        assert len(rebuilt.verified_entries()) == 5
        assert len(rebuilt.unverifiable_entries()) == 2

    def test_replay_never_resolves_by_url_scan(self, archive, monkeypatch):
        manifest = _build(archive)
        replayer = Replayer(manifest)

        called = {'scan': False}

        def fail_scan(path):
            called['scan'] = True
            raise AssertionError(
                'replay must not scan volumes or all captures of a URL')

        monkeypatch.setattr(
            'pywb.audit.replay.scan_volume', fail_scan, raising=False)

        for entry in manifest.verified_entries():
            result = replayer.replay(entry['selection_id'])
            assert result['selection_id'] == entry['selection_id']
        assert called['scan'] is False
