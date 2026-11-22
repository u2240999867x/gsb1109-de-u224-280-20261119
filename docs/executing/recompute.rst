.. _recompute-planning:

Planning recomputations for alternate configurations
====================================================

When a workflow is already loaded (for example in a long-running service), it
is often useful to preview what a configuration change would cause. Snakemake
provides an in-memory planner for this purpose. The planner reuses the loaded
:class:`~snakemake.workflow.Workflow`, its :class:`~snakemake.dag.DAG`, and the
existing :class:`~snakemake.jobs.Job` objects; it does not re-parse the
Snakefile and it does not execute any job.

Basic usage
-----------

.. code-block:: python

    with SnakemakeApi(OutputSettings(verbose=False)) as smk:
        workflow_api = smk.workflow(
            resource_settings=ResourceSettings(cores=1),
            config_settings=ConfigSettings(config=old_config),
            snakefile="Snakefile",
            workdir=workdir,
        )
        dag_api = workflow_api.dag()
        workflow = workflow_api._workflow
        workflow._prepare_dag(
            forceall=False, ignore_incomplete=False, lock_warn_only=True
        )
        workflow._build_dag()

        planner = workflow.recompute_planner()
        old_plan = planner.plan(old_config)
        new_plan = planner.plan(new_config)

``planner.plan()`` accepts a mapping, a config file path, or a
:class:`~snakemake.settings.types.ConfigSettings` instance. Calling it again
with the same configuration returns the cached plan object, while a previously
returned plan remains an immutable snapshot.

Plans are also reachable through the existing objects:

.. code-block:: python

    plan = dag_api.plan_recompute(new_config)
    entry = workflow.dag.plan_recompute(new_config).for_job(job)
    decision = job.recompute_decision(new_config)

Decisions and evidence
----------------------

Each job in a plan receives one of the following decisions:

``recompute``
    The job's own params or inputs are no longer proven by the persisted
    metadata for the planned configuration.

``propagated``
    The job itself is unchanged, but an upstream job will be recomputed.

``reuse``
    The persisted metadata proves that the job's effective parameters and
    input set are unchanged. This includes cases where a configuration value
    changed but a parameter function normalized it to the same effective
    value.

``insufficient_evidence``
    The decision depends on a checkpoint whose old output cannot prove the new
    configuration. The job can only be planned after a real checkpoint run has
    produced matching metadata.

The planner evaluates real parameter callbacks and caches the result according
to the config keys the callback actually read. Callbacks that cannot observe
``config`` are not re-executed on every preview.

Checkpoints
-----------

When a planned configuration changes a checkpoint's route, the old checkpoint
list must not be used to certify the new route. The checkpoint is reported as a
candidate, and its downstream region is marked ``insufficient_evidence``.
After the checkpoint has actually run, calling ``plan()`` again on the same
planner converges that region onto the new plan while preserving unaffected
jobs and their proven parameters.
