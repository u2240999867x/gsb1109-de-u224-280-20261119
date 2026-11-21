from json_stream.base import StreamingJSONBase, TokenType
from json_stream.iterators import ensure_file
from json_stream.select_tokenizer import default_tokenizer


def load(fp_or_iterable, persistent=False, tokenizer=default_tokenizer,
         report=None, **tokenizer_kwargs):
    try:
        return next(load_many(fp_or_iterable, persistent, tokenizer,
                              report=report, **tokenizer_kwargs))
    except StopIteration:
        raise ValueError('No JSON value found') from None


def load_many(fp_or_iterable, persistent=False, tokenizer=default_tokenizer,
              report=None, **tokenizer_kwargs):
    if report is not None and persistent:
        raise ValueError("Quality reporting is only available in transient mode")
    fp = ensure_file(fp_or_iterable)
    token_stream = tokenizer(fp, **tokenizer_kwargs)
    for token_type, token in token_stream:
        if token_type == TokenType.OPERATOR:
            if report is not None:
                data = report._root(token, token_stream)
            else:
                data = StreamingJSONBase.factory(token, token_stream, persistent)
            yield data
            data.read_all()
        else:
            yield token
