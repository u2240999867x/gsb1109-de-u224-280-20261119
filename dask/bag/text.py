from __future__ import annotations

import io
from functools import partial
from typing import Literal

from fsspec.core import open_files
from tlz import concat

from dask.bag.core import empty_bag, from_delayed
from dask.bytes import read_bytes
from dask.delayed import delayed
from dask.utils import parse_bytes, system_encoding

delayed = delayed(pure=True)


def read_text(
    urlpath,
    blocksize=None,
    compression="infer",
    encoding=system_encoding,
    errors="strict",
    linedelimiter=None,
    collection=True,
    storage_options=None,
    files_per_partition=None,
    include_path=False,
    on_missing: Literal["error", "empty"] = "error",
    label=None,
):
    """Read lines from text files

    Parameters
    ----------
    urlpath : string or list
        Absolute or relative filepath(s). Prefix with a protocol like ``s3://``
        to read from alternative filesystems. To read from multiple files you
        can pass a globstring or a list of paths, with the caveat that they
        must all have the same protocol.
    blocksize: None, int, or str
        Size (in bytes) to cut up larger files.  Streams by default.
        Can be ``None`` for streaming, an integer number of bytes, or a string
        like "128MiB"
    compression: string
        Compression format like 'gzip' or 'xz'.  Defaults to 'infer'
    encoding: string
    errors: string
    linedelimiter: string or None
    collection: bool, optional
        Return dask.bag if True, or list of delayed values if false
    storage_options: dict
        Extra options that make sense to a particular storage connection, e.g.
        host, port, username, password, etc.
    files_per_partition: None or int
        If set, group input files into partitions of the requested size,
        instead of one partition per file. Mutually exclusive with blocksize.
    include_path: bool
        Whether or not to include the path in the bag.
        If true, elements are tuples of (line, path).
        Default is False.
    on_missing : {"error", "empty"}, optional
        What to do when ``urlpath`` resolves to no files. The default
        ``"error"`` raises (preserving historical behavior). ``"empty"``
        returns an empty bag with zero partitions instead, which can be
        concatenated with other sources via
        :func:`dask.bag.concat_with_origins` and shows up as an empty source
        rather than failing the whole run. Only used when ``collection`` is
        True.
    label : hashable, optional
        Source label to attach to the returned empty bag when
        ``on_missing="empty"`` and no files are found. Ignored otherwise.

    Examples
    --------
    >>> b = read_text('myfiles.1.txt')  # doctest: +SKIP
    >>> b = read_text('myfiles.*.txt')  # doctest: +SKIP
    >>> b = read_text('myfiles.*.txt.gz')  # doctest: +SKIP
    >>> b = read_text('s3://bucket/myfiles.*.txt')  # doctest: +SKIP
    >>> b = read_text('s3://key:secret@bucket/myfiles.*.txt')  # doctest: +SKIP
    >>> b = read_text('hdfs://namenode.example.com/myfiles.*.txt')  # doctest: +SKIP

    Parallelize a large file by providing the number of uncompressed bytes to
    load into each partition.

    >>> b = read_text('largefile.txt', blocksize='10MB')  # doctest: +SKIP

    Get file paths of the bag by setting include_path=True

    >>> b = read_text('myfiles.*.txt', include_path=True) # doctest: +SKIP
    >>> b.take(1) # doctest: +SKIP
    (('first line of the first file', '/home/dask/myfiles.0.txt'),)

    A source that has no files for the day can be read as an empty bag instead
    of raising:

    >>> b = read_text('today.*.json', on_missing='empty', label='today')  # doctest: +SKIP
    >>> b.npartitions  # doctest: +SKIP
    0

    Returns
    -------
    dask.bag.Bag or list
        dask.bag.Bag if collection is True or list of Delayed lists otherwise.

    See Also
    --------
    from_sequence: Build bag from Python sequence
    dask.bag.empty_bag : Build a labeled empty bag directly
    """
    if on_missing not in ("error", "empty"):
        raise ValueError(
            f"on_missing must be 'error' or 'empty', got {on_missing!r}"
        )
    if blocksize is not None and files_per_partition is not None:
        raise ValueError("Only one of blocksize or files_per_partition can be set")
    if isinstance(blocksize, str):
        blocksize = parse_bytes(blocksize)

    if blocksize is None:
        if linedelimiter in [None, "", "\n", "\r", "\r\n"]:
            newline = linedelimiter
            linedelimiter = None
        else:
            newline = ""
        files = open_files(
            urlpath,
            mode="rt",
            encoding=encoding,
            errors=errors,
            compression=compression,
            newline=newline,
            **(storage_options or {}),
        )
        if files_per_partition is None:
            blocks = [
                delayed(list)(
                    delayed(
                        partial(file_to_blocks, include_path, delimiter=linedelimiter)
                    )(fil)
                )
                for fil in files
            ]
        else:
            blocks = []
            for start in range(0, len(files), files_per_partition):
                block_files = files[start : (start + files_per_partition)]
                block_lines = delayed(concat)(
                    delayed(map)(
                        partial(file_to_blocks, include_path, delimiter=linedelimiter),
                        block_files,
                    )
                )
                blocks.append(block_lines)
    else:
        # special case for linedelimiter=None: we will need to split on an actual bytestring
        # and the line reader will then use "universal" mode. Just as well that \r\n and \n
        # will both work (thankfully \r for MacOS is no longer a thing)
        try:
            o = read_bytes(
                urlpath,
                delimiter=linedelimiter.encode()
                if linedelimiter is not None
                else b"\n",
                blocksize=blocksize,
                sample=False,
                compression=compression,
                include_path=include_path,
                **(storage_options or {}),
            )
        except OSError as e:
            if on_missing == "empty" and str(e).endswith("resolved to no files"):
                return _return_missing(collection, label)
            raise
        raw_blocks = o[1]
        blocks = [
            delayed(decode)(b, encoding, errors, linedelimiter)
            for b in concat(raw_blocks)
        ]
        if include_path:
            paths = list(
                concat([[path] * len(raw_blocks[i]) for i, path in enumerate(o[2])])
            )
            blocks = [
                delayed(attach_path)(entry, path) for entry, path in zip(blocks, paths)
            ]

    if not blocks:
        if on_missing == "empty":
            return _return_missing(collection, label)
        raise ValueError("No files found", urlpath)

    if collection:
        blocks = from_delayed(blocks)

    return blocks


def _return_missing(collection: bool, label):
    """Handle a source with no files according to ``on_missing``."""
    if collection:
        return empty_bag(label)
    return []


def file_to_blocks(include_path, lazy_file, delimiter=None):
    # blocksize is None branch
    with lazy_file as f:
        if delimiter is not None:
            text = f.read()
            if not text:
                return []
            parts = text.split(delimiter)
            yield from (
                (line, lazy_file.path) if include_path else line
                for line in [line + delimiter for line in parts[:-1]] + parts[-1:]
            )
        else:
            for line in f:
                yield (line, lazy_file.path) if include_path else line


def attach_path(block, path):
    for p in block:
        yield (p, path)


def decode(block, encoding, errors, line_delimiter):
    # blocksize is not None branch
    text = block.decode(encoding, errors)
    if line_delimiter in [None, "", "\n", "\r", "\r\n"]:
        lines = io.StringIO(text, newline=line_delimiter)
        return list(lines)
    else:
        if not text:
            return []
        parts = text.split(line_delimiter)
        out = [t + line_delimiter for t in parts[:-1]] + (
            parts[-1:] if not text.endswith(line_delimiter) else []
        )
        return out
