from __future__ import annotations

from copy import deepcopy

from toolz.itertoolz import getter

from dask.typing import no_default


def barrier(*args):
    return None


def getitem(x, key):
    """Like :func:`operator.getitem`, but allows setting key using partial
    ``partial(chunk.getitem, key=key)
    """
    return x[key]


def _copy_initial(initial):
    """Return a new accumulator seed for a single key.

    A callable ``initial`` is treated as a factory, matching
    :func:`toolz.reduceby`.  Anything else is copied so that keys never share
    a mutable accumulator.
    """
    return initial() if callable(initial) else deepcopy(initial)


def reduceby(key, binop, seq, init=no_default):
    """Reduce ``seq`` by ``key``, giving every key its own accumulator seed.

    This is :func:`toolz.reduceby`, except that a non-callable ``init`` is
    copied once per key.  ``toolz.reduceby`` reuses the same object for every
    key, which makes all keys alias one accumulator when ``init`` is mutable
    and ``binop`` mutates it.  With a hot key that shared accumulator grows
    with the whole dataset, and the hot key's values leak into every other
    key.
    """
    if not callable(key):
        key = getter(key)

    has_init = init is not no_default
    result = {}
    for item in seq:
        k = key(item)
        if k not in result:
            if has_init:
                result[k] = binop(_copy_initial(init), item)
            else:
                result[k] = item
        else:
            result[k] = binop(result[k], item)
    return result


def foldby_merge(dicts, combine, initial=no_default):
    """Merge per-partition ``{key: accumulator}`` dicts in order.

    Unlike :func:`toolz.merge_with`, this combines values as it iterates
    instead of collecting one list per key first.  Iteration order is the
    order of ``dicts`` and then insertion order within each dict, so combine
    is applied left-to-right exactly like a sequential reduction.

    If ``initial`` is given, every key gets its own seed, created with
    :func:`_copy_initial`.
    """
    result = {}
    for d in dicts:
        for k, v in d.items():
            if k in result:
                result[k] = combine(result[k], v)
            elif initial is no_default:
                result[k] = v
            else:
                result[k] = combine(_copy_initial(initial), v)
    return result


def foldby_combine2(combine, acc, x):
    return combine(acc, x[1])


def groupby_tasks_group_hash(x, hash, grouper):
    return hash(grouper(x)), x


def var_chunk(seq):
    squares, total, n = 0.0, 0.0, 0
    for x in seq:
        squares += x**2
        total += x
        n += 1
    return squares, total, n


def var_aggregate(x, ddof):
    squares, totals, counts = list(zip(*x))
    x2, x, n = float(sum(squares)), float(sum(totals)), sum(counts)
    result = (x2 / n) - (x / n) ** 2
    return result * n / (n - ddof)
