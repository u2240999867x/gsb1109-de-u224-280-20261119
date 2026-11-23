from snakemake.exceptions import JobWithdrawalException
from snakemake.io import flag, is_flagged


def update(value):
    """
    A flag for an output file that shall be updated instead of overwritten.
    """
    return flag(value, "update")


def before_update(value):
    """
    Flag an input file to be used as is in storage/on-disk before being updated
    in a later rule.
    This flag leads to the input file being considered as not being created by any other
    job.
    """
    return flag(value, "before_update")


def withdraw(reason: str | None = None):
    """Withdraw the current job from the DAG.

    This is meant to be raised from an input function when a dynamically
    discovered item no longer exists in the current revision of the discovery
    result (e.g. a sample that has been removed from a manifest). The job is
    removed instead of being executed or reported as a missing input.
    """
    raise JobWithdrawalException(reason)


def register_in_globals(_globals):
    _globals.update(
        {
            "update": update,
            "before_update": before_update,
            "withdraw": withdraw,
        }
    )
