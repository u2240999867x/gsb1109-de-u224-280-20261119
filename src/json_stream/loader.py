from json_stream.base import StreamingJSONBase, TokenType
from json_stream.iterators import ensure_file
from json_stream.retain import Retain, create_retaining_root
from json_stream.select_tokenizer import default_tokenizer


def load(fp_or_iterable, persistent=False, tokenizer=default_tokenizer, retain=None,
         **tokenizer_kwargs):
    try:
        return next(load_many(fp_or_iterable, persistent, tokenizer, retain=retain,
                              **tokenizer_kwargs))
    except StopIteration:
        raise ValueError('No JSON value found') from None


def load_many(fp_or_iterable, persistent=False, tokenizer=default_tokenizer, retain=None,
              **tokenizer_kwargs):
    if retain is not None and persistent:
        raise ValueError("retain cannot be combined with persistent=True")
    if retain is not None and not isinstance(retain, Retain):
        raise TypeError("retain must be a Retain instance")
    fp = ensure_file(fp_or_iterable)
    token_stream = tokenizer(fp, **tokenizer_kwargs)
    for token_type, token in token_stream:
        if token_type == TokenType.OPERATOR:
            if retain is not None:
                data = create_retaining_root(token, token_stream, retain)
            else:
                data = StreamingJSONBase.factory(token, token_stream, persistent)
            yield data
            data.read_all()
        else:
            yield token
