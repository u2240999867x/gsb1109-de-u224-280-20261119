"""Scheduling plugins shipped with Snakemake."""

from __future__ import annotations


def register_builtin_schedulers() -> None:
    """Register Snakemake's own scheduler plugins with the plugin registry.

    The external interface package only knows about ``greedy`` and ``ilp``.
    The optional ``deadline`` scheduler lives in Snakemake itself, so it has to
    be registered explicitly.  Registration is idempotent.
    """
    from snakemake_interface_scheduler_plugins.registry import SchedulerPluginRegistry

    from snakemake.scheduling import deadline

    SchedulerPluginRegistry().register_plugin("deadline", deadline)
