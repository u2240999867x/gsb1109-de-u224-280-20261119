from json_stream.base import StreamingJSONBase, TokenType
from json_stream.iterators import ensure_file
from json_stream.select_tokenizer import default_tokenizer


def load(fp_or_iterable, persistent=False, tokenizer=default_tokenizer, **tokenizer_kwargs):
    sequence = _take_sequence_flag(tokenizer_kwargs)
    if sequence:
        from json_stream.sequence import load_sequence

        try:
            return next(load_sequence(
                fp_or_iterable,
                persistent=persistent,
                tokenizer=tokenizer,
                **tokenizer_kwargs
            ))
        except StopIteration:
            raise ValueError('No JSON value found') from None
    try:
        return next(load_many(fp_or_iterable, persistent, tokenizer, **tokenizer_kwargs))
    except StopIteration:
        raise ValueError('No JSON value found') from None


def _take_sequence_flag(tokenizer_kwargs):
    sequence = tokenizer_kwargs.pop('sequence', False)
    sequence = tokenizer_kwargs.pop('rfc7464', sequence)
    framing = tokenizer_kwargs.pop('framing', None)
    if framing is not None:
        normalized = str(framing).lower().replace('-', '').replace('_', '')
        if normalized in ('rfc7464', 'jsontextsequence', 'sequence', 'textsequence'):
            sequence = True
        elif normalized in ('json', 'none', 'plain'):
            sequence = False
        else:
            raise ValueError(f'Unknown JSON framing: {framing!r}')
    return sequence


def load_many(fp_or_iterable, persistent=False, tokenizer=default_tokenizer, **tokenizer_kwargs):
    if _take_sequence_flag(tokenizer_kwargs):
        from json_stream.sequence import load_sequence

        yield from load_sequence(
            fp_or_iterable,
            persistent=persistent,
            tokenizer=tokenizer,
            **tokenizer_kwargs
        )
        return
    fp = ensure_file(fp_or_iterable)
    token_stream = tokenizer(fp, **tokenizer_kwargs)
    for token_type, token in token_stream:
        if token_type == TokenType.OPERATOR:
            data = StreamingJSONBase.factory(token, token_stream, persistent)
            yield data
            data.read_all()
        else:
            yield token
