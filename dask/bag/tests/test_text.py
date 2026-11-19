from __future__ import annotations

from functools import partial

import pytest
from fsspec.compression import compr
from tlz import concat

from dask import compute, config
from dask.bag.text import read_text
from dask.bytes import utils
from dask.utils import filetexts

compute = partial(compute, scheduler="sync")


files = {
    ".test.accounts.1.json": (
        '{"amount": 100, "name": "Alice"}\n'
        '{"amount": 200, "name": "Bob"}\n'
        '{"amount": 300, "name": "Charlie"}\n'
        '{"amount": 400, "name": "Dennis"}\n'
    ),
    ".test.accounts.2.json": (
        '{"amount": 500, "name": "Alice"}\n'
        '{"amount": 600, "name": "Bob"}\n'
        '{"amount": 700, "name": "Charlie"}\n'
        '{"amount": 800, "name": "Dennis"}\n'
    ),
}


expected = "".join([files[v] for v in sorted(files)])

fmt_bs = [(fmt, None) for fmt in compr] + [(None, "10 B")]

encodings = ["ascii", "utf-8"]  # + ['utf-16', 'utf-16-le', 'utf-16-be']
fmt_bs_enc_path = [
    (fmt, bs, encoding, include_path)
    for fmt, bs in fmt_bs
    for encoding in encodings
    for include_path in (True, False)
]


@pytest.mark.parametrize("fmt,bs,encoding,include_path", fmt_bs_enc_path)
def test_read_text(fmt, bs, encoding, include_path):
    if fmt not in utils.compress:
        pytest.skip(f"compress function not provided for {fmt}")
    compress = utils.compress[fmt]
    files2 = {k: compress(v.encode(encoding)) for k, v in files.items()}
    with filetexts(files2, mode="b"):
        b = read_text(
            ".test.accounts.*.json", compression=fmt, blocksize=bs, encoding=encoding
        )
        (L,) = compute(b)
        assert "".join(L) == expected

        o = read_text(
            sorted(files),
            compression=fmt,
            blocksize=bs,
            encoding=encoding,
            include_path=include_path,
        )
        b = o.pluck(0) if include_path else o
        (L,) = compute(b)
        assert "".join(L) == expected
        if include_path:
            (paths,) = compute(o.pluck(1))
            expected_paths = list(
                concat([[k] * v.count("\n") for k, v in files.items()])
            )
            assert len(paths) == len(expected_paths)
            for path, expected_path in zip(paths, expected_paths):
                assert path.endswith(expected_path)

        blocks = read_text(
            ".test.accounts.*.json",
            compression=fmt,
            blocksize=bs,
            encoding=encoding,
            collection=False,
        )
        L = compute(*blocks)
        assert "".join(line for block in L for line in block) == expected


def test_read_text_unicode_no_collection(tmp_path):
    data = b"abcd\xc3\xa9"
    fn = tmp_path / "data.txt"
    with open(fn, "wb") as f:
        f.write(b"\n".join([data, data]))

    f = read_text(fn, collection=False)

    result = f[0].compute()
    assert len(result) == 2


def test_files_per_partition():
    files3 = {f"{n:02}.txt": "line from {:02}" for n in range(20)}
    with filetexts(files3):
        # single-threaded scheduler to ensure the warning happens in the
        # same thread as the pytest.warns
        with config.set({"scheduler": "single-threaded"}):
            with pytest.warns(UserWarning):
                b = read_text("*.txt", files_per_partition=10)
                l = len(b.take(100, npartitions=1))

            assert l == 10, "10 files should be grouped into one partition"

            assert b.count().compute() == 20, "All 20 lines should be read"

            with pytest.warns(UserWarning):
                b = read_text("*.txt", files_per_partition=10, include_path=True)
                p = b.take(100, npartitions=1)

            p_paths = tuple(zip(*p))[1]
            p_unique_paths = set(p_paths)
            assert len(p_unique_paths) == 10

            b_paths = tuple(zip(*b.compute()))[1]
            b_unique_paths = set(b_paths)
            assert len(b_unique_paths) == 20


def test_errors():
    with filetexts({".test.foo": b"Jos\xe9\nAlice"}, mode="b"):
        with pytest.raises(UnicodeDecodeError):
            read_text(".test.foo", encoding="ascii").compute()

        result = read_text(".test.foo", encoding="ascii", errors="ignore")
        result = result.compute(scheduler="sync")
        assert result == ["Jos\n", "Alice"]


def test_complex_delimiter():
    longstr = "abc\ndef\n123\n$$$$\ndog\ncat\nfish\n\n\r\n$$$$hello"
    with filetexts({".test.delim.txt": longstr}):
        assert read_text(".test.delim.txt", linedelimiter="$$$$").count().compute() == 3
        assert (
            read_text(".test.delim.txt", linedelimiter="$$$$", blocksize=2)
            .count()
            .compute()
            == 3
        )
        vals = read_text(".test.delim.txt", linedelimiter="$$$$").compute()
        assert vals[-1] == "hello"
        assert vals[0].endswith("$$$$")
        vals = read_text(".test.delim.txt", linedelimiter="$$$$", blocksize=2).compute()
        assert vals[-1] == "hello"
        assert vals[0].endswith("$$$$")


def test_read_text_missing_raises_by_default(tmp_path):
    urlpath = str(tmp_path / "missing.*.txt")
    with pytest.raises(ValueError, match="No files found"):
        read_text(urlpath)
    with pytest.raises(OSError, match="resolved to no files"):
        read_text(urlpath, blocksize="10MB")


def test_read_text_missing_returns_empty_bag(tmp_path):
    import dask.bag as db

    urlpath = str(tmp_path / "missing.*.txt")
    bag = read_text(urlpath, on_missing="empty", label="late-source")
    assert isinstance(bag, db.Bag)
    assert bag.npartitions == 0
    assert bag.source_label == "late-source"
    assert bag.compute(scheduler="sync") == []

    # The blocksize branch (read_bytes) reports missing files as OSError.
    bag_blocked = read_text(
        urlpath, blocksize="10MB", on_missing="empty", label="late-source"
    )
    assert bag_blocked.npartitions == 0
    assert bag_blocked.source_label == "late-source"


def test_read_text_missing_no_collection(tmp_path):
    urlpath = str(tmp_path / "missing.*.txt")
    assert read_text(urlpath, collection=False, on_missing="empty") == []


def test_read_text_on_missing_invalid(tmp_path):
    with pytest.raises(ValueError, match="on_missing"):
        read_text(str(tmp_path / "*.txt"), on_missing="bogus")


def test_read_text_on_missing_empty_keeps_present_files(tmp_path):
    fn = tmp_path / "present.txt"
    fn.write_text("a\nb\n")
    bag = read_text(str(fn), on_missing="empty", label="present")
    assert bag.npartitions == 1
    assert bag.compute(scheduler="sync") == ["a\n", "b\n"]
    assert bag.source_label is None


def test_read_text_missing_sources_concat_with_origins(tmp_path):
    import dask.bag as db

    present_path = tmp_path / "present.txt"
    present_path.write_text("x\ny\n")

    sources = {
        "missing-streaming": read_text(
            str(tmp_path / "missing-1.*.txt"),
            on_missing="empty",
            label="missing-streaming",
        ),
        "present": read_text(str(present_path)),
        "missing-blocked": read_text(
            str(tmp_path / "missing-2.*.txt"),
            blocksize="10MB",
            on_missing="empty",
            label="missing-blocked",
        ),
    }
    bag, origins, empty_sources = db.concat_with_origins(sources)

    assert bag.compute(scheduler="sync") == ["x\n", "y\n"]
    assert empty_sources == [
        ("missing-streaming", 0),
        ("missing-blocked", 2),
    ]
    assert {o.source for o in origins} == {"present"}
    assert [(o.source_index, o.source_partition) for o in origins] == [(1, 0)]

