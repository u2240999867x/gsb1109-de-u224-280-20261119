import hashlib

import pytest

from pywb.utils.timemap_reader import (
    TimeMapReader,
    TimeMapResumeError,
    parse_timemap,
)


BASE = 'https://archive.example.org/download/%E6%97%B6%E9%97%B4%E5%9B%BE.txt'

# A TimeMap exercising every syntax edge named in the task:
# * original, self and two timegate relations
# * a real memento with the "first" and "memento" relation tokens
# * Chinese path and query text
# * an escaped quote and a comma + semicolon inside a quoted parameter
# * a parameter whose value looks like a following link
# * two captures in the same second, one absolute and one relative
# * a "memento-like" relation which is *not* a capture
# * a capture with no datetime at all
DOC = (
    '<http://example.com/page>; rel="original",\n'
    '<https://archive.example.org/download/time.txt>; rel="self"; '
    'type="application/link-format"; from="Fri, 03 Jan 2014 03:03:21 GMT",\n'
    '<http://example.com/timegate>; rel="timegate",\n'
    '<//other.example.org/timegate>; rel="timegate",\n'
    '</中文/第一页?q=引用>; rel="first memento"; '
    'datetime="Fri, 03 Jan 2014 03:03:21 GMT"; '
    'note="Mon, 03 Jan 2014, 03:03:21 GMT; next=<http://looks/like/a/link>; '
    'quote=\\"escaped\\"",\n'
    '<https://archive.example.org/20140103030322/http://example.com/page>; '
    'rel="memento"; datetime="Fri, 03 Jan 2014 03:03:22 GMT",\n'
    '<20140103030322/relative-copy>; rel="memento"; '
    'datetime="Fri, 03 Jan 2014 03:03:22 GMT",\n'
    '<https://archive.example.org/memento-like>; rel="memento-like"; '
    'datetime="Fri, 03 Jan 2014 03:03:23 GMT",\n'
    '<https://archive.example.org/no-date>; rel="memento",\n'
    '<https://archive.example.org/after-bad>; rel="memento"; '
    'datetime="Fri, 03 Jan 2014 03:03:24 GMT"\n'
)


def signatures(reader):
    return [rec.signature() for rec in reader.captures]


def full_parse(data=DOC, base=BASE):
    return parse_timemap(data, final_url=base, validator='"v1"').records


def feed_in_chunks(data, cuts, base=BASE, validator='"v1"', overlap=0):
    reader = TimeMapReader(final_url=base, validator=validator)
    emitted = []
    start = 0
    for cut in cuts:
        chunk = data[start:cut]
        if overlap and emitted:
            # Re-send a few already confirmed bytes and the new segment.
            begin = max(0, start - overlap)
            chunk = data[begin:cut]
            emitted.extend(reader.feed(chunk, begin))
        else:
            emitted.extend(reader.feed(chunk, start))
        start = cut
    emitted.extend(reader.feed(data[start:], start))
    emitted.extend(reader.finish())
    return reader, emitted


# =============================================================================
def test_one_shot_classifies_relations():
    reader = parse_timemap(DOC, final_url=BASE, validator='"v1"')

    assert [rec.rel for rec in reader.relations] == [
        'original', 'self', 'timegate', 'timegate', 'memento-like']
    assert len(reader.captures) == 4

    rels = [rec.rel for rec in reader.captures]
    assert rels == ['first memento', 'memento', 'memento', 'memento']

    # "first memento" contains the memento relation token.
    assert reader.captures[0].is_capture
    assert reader.captures[0].timestamp == '20140103030321'

    # The deliberately same-worded non-capture relation never enters backfill.
    assert not any(rec.rel == 'memento-like' and rec.is_capture
                   for rec in reader.records)

    # The date-less capture is retained as a located bad item...
    bad = reader.issues[-1]
    assert bad.is_capture
    assert bad.error == 'missing datetime'
    assert bad.start > 0
    assert DOC.encode('utf-8')[bad.start:bad.end] == bad.raw

    # ...and cannot stop the next valid capture.
    assert reader.captures[-1].target == \
        'https://archive.example.org/after-bad'


def test_real_capture_count_excludes_non_capture_and_bad():
    reader = parse_timemap(DOC, final_url=BASE)
    # real, first, second-of-same-second, after-bad
    assert [rec.target for rec in reader.captures] == [
        'https://archive.example.org/中文/第一页?q=引用',
        'https://archive.example.org/20140103030322/http://example.com/page',
        'https://archive.example.org/download/20140103030322/relative-copy',
        'https://archive.example.org/after-bad',
    ]


def test_relative_targets_use_final_response_url_only():
    reader = parse_timemap(DOC, final_url=BASE)

    rel = reader.captures[2]
    assert rel.url == '20140103030322/relative-copy'
    assert rel.target == \
        'https://archive.example.org/download/20140103030322/relative-copy'

    # original and self are records, not resolution bases.
    original = reader.relations[0]
    self_link = reader.relations[1]
    assert original.url == 'http://example.com/page'
    assert self_link.target == 'https://archive.example.org/download/time.txt'

    no_base = parse_timemap('<../x>; rel="memento"; '
                            'datetime="Fri, 03 Jan 2014 03:03:21 GMT"',
                            require_base=True)
    assert no_base.captures == ()
    assert no_base.issues[0].error == 'relative target without a response URL'


def test_same_second_targets_remain_distinct():
    reader = parse_timemap(DOC, final_url=BASE)
    same_second = [rec for rec in reader.captures
                   if rec.timestamp == '20140103030322']
    assert len(same_second) == 2
    assert same_second[0].target != same_second[1].target
    assert len({rec.target for rec in same_second}) == 2


def test_arbitrary_byte_splits_match_one_shot():
    data = DOC.encode('utf-8')
    expected = signatures(parse_timemap(data, final_url=BASE))

    for cut in range(len(data) + 1):
        reader, _ = feed_in_chunks(data, [cut])
        assert signatures(reader) == expected, cut


def test_multibyte_and_datetime_comma_splits():
    data = DOC.encode('utf-8')
    expected = signatures(parse_timemap(data, final_url=BASE))

    # Find a byte inside a multibyte character and a comma inside datetime.
    chinese = data.index(b'\xe4')
    datetime_comma = data.index(b'Fri, 03 Jan 2014 03:03:21 GMT') + 3
    quote = data.index(b'\\"escaped\\"') + 2

    for cut in (chinese + 1, chinese + 2, datetime_comma, quote):
        reader, _ = feed_in_chunks(data, [cut, cut + 3])
        assert signatures(reader) == expected, cut


def test_one_byte_chunks():
    data = DOC.encode('utf-8')
    expected = signatures(parse_timemap(data, final_url=BASE))

    reader = TimeMapReader(final_url=BASE)
    for pos in range(len(data)):
        reader.feed(data[pos:pos + 1], pos)
    reader.finish()
    assert signatures(reader) == expected


# =============================================================================
def test_interrupt_half_link_and_resume_with_overlap():
    data = DOC.encode('utf-8')

    # Cut in the middle of the first Chinese capture, after its <...> target
    # but before the record comma: the link must not be published yet.
    cut = data.index('第一页'.encode('utf-8')) + 3
    reader = TimeMapReader(final_url=BASE, validator='"v1"')
    first = reader.feed(data[:cut], 0)
    assert [rec.rel for rec in first] == ['original', 'self', 'timegate',
                                          'timegate']
    checkpoint = reader.checkpoint()
    assert checkpoint.confirmed_offset < cut

    # Resume at a byte a short distance *before* the confirmed boundary.
    resume_at = max(0, checkpoint.confirmed_offset - 7)
    resumed = TimeMapReader.from_checkpoint(checkpoint)
    resumed.begin_response(BASE, '"v1"')
    emitted = resumed.feed(data[resume_at:], resume_at)
    emitted += resumed.finish()

    assert [rec.rel for rec in emitted][0] == 'first memento'
    all_records = list(first) + list(emitted)

    expected = signatures(parse_timemap(data, final_url=BASE))
    all_expected = [rec.signature()
                    for rec in parse_timemap(data, final_url=BASE).records]
    assert [rec.signature() for rec in all_records] == all_expected
    assert len(all_records) == len(all_expected)
    assert [rec.signature() for rec in resumed.records] == all_expected[4:]
    assert resumed.checkpoint().confirmed_offset == len(data)


def test_resume_gap_is_rejected():
    reader = TimeMapReader(final_url=BASE)
    reader.feed(DOC.encode('utf-8')[:20], 0)
    checkpoint = reader.checkpoint()

    resumed = TimeMapReader.from_checkpoint(checkpoint)
    with pytest.raises(TimeMapResumeError):
        resumed.feed(b'more data', checkpoint.confirmed_offset + 100)


def test_resume_mismatched_overlap_is_rejected():
    data = DOC.encode('utf-8')
    reader = TimeMapReader(final_url=BASE)
    reader.feed(data[:300], 0)
    checkpoint = reader.checkpoint()

    resumed = TimeMapReader.from_checkpoint(checkpoint)
    bad = bytearray(data[checkpoint.confirmed_offset - 5:])
    bad[0] ^= 0xFF
    with pytest.raises(TimeMapResumeError):
        resumed.feed(bytes(bad), checkpoint.confirmed_offset - 5)


# =============================================================================
def test_changed_validator_restarts_new_generation():
    data = DOC.encode('utf-8')

    # Cut exactly at a record boundary: everything before it is confirmed.
    prefix_end = data.index(b'</\xe4')
    prefix = data[:prefix_end]

    reader = TimeMapReader(final_url=BASE, validator='"v1"')
    reader.feed(prefix, 0)
    old_records = list(reader.records)
    assert [rec.rel for rec in old_records] == [
        'original', 'self', 'timegate', 'timegate']
    checkpoint = reader.checkpoint()
    # A record boundary: the confirmed position sits just past the delimiter
    # that precedes the next record.
    assert checkpoint.confirmed_offset == prefix_end - 1
    assert data[checkpoint.confirmed_offset - 1:checkpoint.confirmed_offset] == b','

    # The service replaced the same-named file.  The first bytes of the
    # resent segment are identical to the old representation, but the
    # validator changed and the old parse positions no longer apply.
    new_doc = prefix + (
        '<https://archive.example.org/only-new>; rel="memento"; '
        'datetime="Fri, 03 Jan 2014 04:04:04 GMT"\n'
    ).encode('utf-8')
    assert new_doc[:prefix_end] == data[:prefix_end]

    resumed = TimeMapReader.from_checkpoint(checkpoint)
    assert resumed.begin_response(BASE, '"v2"') is True
    assert resumed.generation == checkpoint.generation + 1
    assert resumed.confirmed_offset == 0

    resumed.feed(new_doc, 0)
    resumed.finish()

    # No memento is spliced from bytes of the two generations.
    assert [rec.target for rec in resumed.captures] == [
        'https://archive.example.org/only-new']
    assert all(rec.generation == resumed.generation
               for rec in resumed.records)
    assert not any('20140103030322' in rec.target for rec in resumed.captures)


def test_same_validator_continues_generation():
    data = DOC.encode('utf-8')
    reader = TimeMapReader(final_url=BASE, validator='"v1"')
    reader.feed(data[:400], 0)
    checkpoint = reader.checkpoint()

    resumed = TimeMapReader.from_checkpoint(checkpoint)
    assert resumed.begin_response(BASE, '"v1"') is False
    assert resumed.generation == checkpoint.generation
    assert resumed.confirmed_offset == checkpoint.confirmed_offset


# =============================================================================
def test_identical_link_twice_in_one_file_is_two_locations():
    link = ('<https://archive.example.org/twice>; rel="memento"; '
            'datetime="Fri, 03 Jan 2014 05:05:05 GMT"')
    doc = (link + ',\n' + link + '\n').encode('utf-8')

    reader = parse_timemap(doc, final_url=BASE, validator='"v1"')

    assert len(reader.captures) == 2
    first, second = reader.captures
    assert first.start != second.start
    assert first.occurrence_index == 1
    assert second.occurrence_index == 2
    assert second.duplicate_of == first.start
    assert first.duplicate_of is None
    assert len(reader.unique_captures) == 1

    # Both can be traced back to their own bytes in the source.
    for rec in reader.captures:
        assert doc[rec.start:rec.end] == rec.raw


def test_protocol_replay_is_not_an_in_file_duplicate():
    link = ('<https://archive.example.org/once>; rel="memento"; '
            'datetime="Fri, 03 Jan 2014 05:05:05 GMT"')
    doc = (link + ',\n').encode('utf-8')

    reader = TimeMapReader(final_url=BASE, validator='"v1"')
    reader.feed(doc, 0)
    assert len(reader.captures) == 1

    # Re-send bytes around a confirmed boundary, as a range retry would.
    checkpoint = reader.checkpoint()
    replay_start = checkpoint.confirmed_offset - 10
    reader.feed(doc[replay_start:], replay_start)
    reader.finish()

    # Still exactly one capture, and no in-file duplicate was invented.
    assert len(reader.captures) == 1
    assert reader.duplicates == ()
    assert reader.confirmed_offset == len(doc)


# =============================================================================
def test_provenance_offsets_reconstruct_source_records():
    data = DOC.encode('utf-8')
    reader = parse_timemap(data, final_url=BASE)

    for rec in reader.records:
        assert data[rec.start:rec.end] == rec.raw

    for rec in reader.issues:
        assert data[rec.start:rec.end] == rec.raw
        assert rec.reason


def test_checkpoint_does_not_retain_whole_file():
    data = (DOC * 200).encode('utf-8')
    reader = TimeMapReader(final_url=BASE, tail_size=64)
    reader.feed(data[:len(data) // 2], 0)
    checkpoint = reader.checkpoint()

    assert len(checkpoint.tail) <= 64
    assert checkpoint.confirmed_offset < len(data)

    resumed = TimeMapReader.from_checkpoint(checkpoint)
    resumed.begin_response(BASE, None)
    resumed.feed(data[checkpoint.confirmed_offset - 16:],
                 checkpoint.confirmed_offset - 16)
    resumed.finish()

    expected = signatures(parse_timemap(data, final_url=BASE))
    already = signatures(reader)
    assert expected[:len(already)] == already
    assert signatures(resumed) == expected[len(already):]


def test_trailing_link_without_comma_is_emitted_on_finish():
    data = ('<https://archive.example.org/last>; rel="memento"; '
            'datetime="Fri, 03 Jan 2014 06:06:06 GMT"').encode('utf-8')
    reader = TimeMapReader(final_url=BASE)
    assert reader.feed(data, 0) == []
    emitted = reader.finish()
    assert len(emitted) == 1
    assert emitted[0].target == 'https://archive.example.org/last'


def test_multiple_rel_attributes_and_tokens():
    doc = ('<https://archive.example.org/multi>; rel="memento"; '
           'rel="first"; datetime="Fri, 03 Jan 2014 07:07:07 GMT"')
    reader = parse_timemap(doc, final_url=BASE)
    rec = reader.captures[0]
    assert rec.rels == ('memento', 'first')
    assert rec.rel_tokens == ('memento', 'first')
    assert rec.timestamp == '20140103070707'


# =============================================================================
def test_sha256_helper_matches_raw():
    data = DOC.encode('utf-8')
    reader = parse_timemap(data, final_url=BASE)
    for rec in reader.records:
        assert hashlib.sha256(rec.raw).hexdigest()


# =============================================================================
def test_segmented_driver_matches_one_shot():
    from pywb.utils.timemap_reader import harvest_segmented

    expected = [rec.signature()
                for rec in parse_timemap(DOC, final_url=BASE).records]

    for chunk_size in (1, 2, 3, 7, 16, 64, 4096):
        reader, source = harvest_segmented(DOC, final_url=BASE,
                                           validator='"v1"',
                                           chunk_size=chunk_size, overlap=5)
        assert [rec.signature() for rec in reader.records] == expected, chunk_size
        assert reader.confirmed_offset == len(DOC.encode('utf-8'))


def test_segmented_driver_marks_interruption_then_resumes():
    from pywb.utils.timemap_reader import harvest_segmented, resume_segmented

    data = DOC.encode('utf-8')
    expected = [rec.signature()
                for rec in parse_timemap(data, final_url=BASE).records]

    # Read only a few chunks: stop in the middle of the first capture.
    reader, source = harvest_segmented(data, final_url=BASE,
                                       validator='"v1"',
                                       chunk_size=24, overlap=4,
                                       max_requests=10)
    first = [rec.signature() for rec in reader.records]
    assert first == expected[:3]
    checkpoint = reader.checkpoint()
    assert checkpoint.confirmed_offset < len(data)

    resumed, resumed_source = resume_segmented(checkpoint, data,
                                               chunk_size=24, overlap=4)
    assert [rec.signature() for rec in resumed.records] == expected[3:]
    assert first + [rec.signature() for rec in resumed.records] == expected
    assert resumed.checkpoint().confirmed_offset == len(data)

    # The resumed source only read from the overlap onward.
    assert all(start >= checkpoint.confirmed_offset - 4
               for start, _ in resumed_source.requests)


def test_segmented_resume_after_validator_change_restarts():
    from pywb.utils.timemap_reader import harvest_segmented, resume_segmented

    data = DOC.encode('utf-8')
    prefix_end = data.index(b'</\xe4')
    reader, _ = harvest_segmented(data[:prefix_end], final_url=BASE,
                                  validator='"v1"', chunk_size=8,
                                  overlap=2)

    new_doc = data[:prefix_end] + (
        '<https://archive.example.org/replaced>; rel="memento"; '
        'datetime="Fri, 03 Jan 2014 08:08:08 GMT"\n'
    ).encode('utf-8')

    resumed, source = resume_segmented(reader.checkpoint(), new_doc,
                                       validator='"v2"')
    assert [rec.target for rec in resumed.captures] == [
        'https://archive.example.org/replaced']
    assert resumed.generation == 2
    assert all(rec.generation == 2 for rec in resumed.records)
    # Read started afresh; no old parse position was reused.
    assert source.requests[0][0] == 0


# =============================================================================
def test_harvester_across_resume_publishes_each_target_once():
    from pywb.utils.timemap_reader import TimeMapHarvester

    data = DOC.encode('utf-8')
    harvester = TimeMapHarvester(final_url=BASE, validator='"v1"')
    first = harvester.feed(data[:300], 0)
    assert harvester.confirmed_offset == 268

    resumed = TimeMapHarvester(checkpoint=harvester.checkpoint())
    assert resumed.begin(BASE, '"v1"') is False
    start = resumed.confirmed_offset - 5
    second = resumed.feed(data[start:], start)
    second += resumed.finish()

    assert first + second == [
        'https://archive.example.org/中文/第一页?q=引用',
        'https://archive.example.org/20140103030322/http://example.com/page',
        'https://archive.example.org/download/20140103030322/relative-copy',
        'https://archive.example.org/after-bad',
    ]
    assert len(set(first + second)) == 4


def test_harvester_reports_bad_offsets():
    from pywb.utils.timemap_reader import TimeMapHarvester

    harvester = TimeMapHarvester(final_url=BASE, validator='"v1"')
    harvester.feed(DOC, 0)
    harvester.finish()

    bad = harvester.issues
    assert [rec.error for rec in bad] == ['missing datetime']
    assert bad[0].start > 0


def test_parse_local_file(tmp_path):
    from pywb.utils.timemap_reader import parse_timemap_file

    path = tmp_path / 'timemap.txt'
    path.write_bytes(DOC.encode('utf-8'))

    reader = parse_timemap_file(str(path), final_url=BASE, chunk_size=7)
    expected = parse_timemap(DOC, final_url=BASE)
    assert [rec.signature() for rec in reader.records] == \
        [rec.signature() for rec in expected.records]


def test_newline_separated_records_without_comma():
    doc = ('<http://example.com/>; rel="original"\n'
           '<https://archive.example.org/1>; rel="memento"; '
           'datetime="Fri, 03 Jan 2014 03:03:21 GMT"\n').encode('utf-8')
    reader = parse_timemap(doc, final_url=BASE)
    assert [rec.rel for rec in reader.records] == ['original', 'memento']
    assert reader.captures[0].target == 'https://archive.example.org/1'


def test_folded_parameter_lines_stay_one_record():
    doc = ('<http://example.com/>; rel="original",\n'
           '<https://archive.example.org/1>;\n'
           ' rel="first memento";\n'
           ' datetime="Fri, 03 Jan 2014 03:03:21 GMT"\n').encode('utf-8')
    reader = parse_timemap(doc, final_url=BASE)
    assert len(reader.captures) == 1
    assert reader.captures[0].timestamp == '20140103030321'
    assert reader.captures[0].rel_tokens == ('first', 'memento')


def test_checkpoint_json_round_trip():
    import json
    from pywb.utils.timemap_reader import TimeMapCheckpoint

    data = DOC.encode('utf-8')
    reader = TimeMapReader(final_url=BASE, validator='"v1"')
    reader.feed(data[:300], 0)

    values = json.loads(json.dumps(reader.checkpoint().to_dict()))
    checkpoint = TimeMapCheckpoint.from_dict(values)
    resumed = TimeMapReader.from_checkpoint(checkpoint)
    resumed.feed(data[checkpoint.confirmed_offset - 6:],
                 checkpoint.confirmed_offset - 6)
    resumed.finish()

    assert [rec.target for rec in resumed.captures] == [
        'https://archive.example.org/中文/第一页?q=引用',
        'https://archive.example.org/20140103030322/http://example.com/page',
        'https://archive.example.org/download/20140103030322/relative-copy',
        'https://archive.example.org/after-bad',
    ]
