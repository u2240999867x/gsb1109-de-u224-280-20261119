import os

from json_stream.base import StreamingJSONObject, StreamingJSONList, StreamingJSONBase
from json_stream.iterators import ensure_file
from json_stream.resumable import visit_many_checkpointed
from json_stream.select_tokenizer import default_tokenizer
from json_stream.tokenizer import TokenType


def _visit(obj, visitor, path):
    k = None
    if isinstance(obj, StreamingJSONObject):
        for k, v in obj.items():
            _visit(v, visitor, path + (k,))
        if k is None:
            visitor({}, path)
    elif isinstance(obj, StreamingJSONList):
        for k, v in enumerate(obj):
            _visit(v, visitor, path + (k,))
        if k is None:
            visitor([], path)
    else:
        visitor(obj, path)


def visit_many(
    fp_or_iterator,
    visitor,
    tokenizer=default_tokenizer,
    *,
    checkpoint=None,
    reporter=None,
    on_checkpoint=None,
    save_checkpoint=None,
    verify='digest',
    source_identity=None,
    reconfirm=False,
    resumable=False,
    **tokenizer_kwargs
):
    """Visit every top-level JSON root in a concatenated stream.

    Without ``checkpoint``/``reporter``/``on_checkpoint``/``save_checkpoint``
    and with ``resumable=False`` this keeps the original streaming behaviour
    and accepts any iterable.

    When a saveable document-level credential is requested, the visit is
    delegated to :mod:`json_stream.resumable`, which requires a re-openable
    raw binary archive (a path or a seekable binary file) so that the exact
    byte position of each fully verified root can be recorded.  Passing a
    filesystem path also selects this reader, because it re-opens the raw
    bytes itself.
    """
    if resumable or any(
        x is not None for x in (checkpoint, reporter, on_checkpoint, save_checkpoint)
    ):
        return visit_many_checkpointed(
            fp_or_iterator,
            visitor,
            checkpoint=checkpoint,
            reporter=reporter,
            on_checkpoint=on_checkpoint,
            save_checkpoint=save_checkpoint,
            tokenizer=tokenizer,
            verify=verify,
            source_identity=source_identity,
            reconfirm=reconfirm,
            **tokenizer_kwargs
        )
    if isinstance(fp_or_iterator, (str, os.PathLike)):
        return visit_many_checkpointed(
            fp_or_iterator,
            visitor,
            tokenizer=tokenizer,
            verify=verify,
            source_identity=source_identity,
            **tokenizer_kwargs
        )
    return _visit_many(fp_or_iterator, visitor, tokenizer, **tokenizer_kwargs)


def _visit_many(fp_or_iterator, visitor, tokenizer, **tokenizer_kwargs):
    fp = ensure_file(fp_or_iterator)
    token_stream = tokenizer(fp, **tokenizer_kwargs)
    for token_type, token in token_stream:
        if token_type == TokenType.OPERATOR:
            obj = StreamingJSONBase.factory(token, token_stream, persistent=False)
            _visit(obj, visitor, ())
            obj.read_all()
        else:
            _visit(token, visitor, ())
        yield


def visit(fp_or_iterator, visitor, tokenizer=default_tokenizer, **tokenizer_kwargs):
    try:
        next(visit_many(fp_or_iterator, visitor, tokenizer, **tokenizer_kwargs))
    except StopIteration:
        raise ValueError('No JSON value found') from None
