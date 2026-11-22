.. _caching:

========================
Between workflow caching
========================

Within certain data analysis fields, there are certain intermediate results that reoccur in exactly the same way in many analysis.
For example, in bioinformatics, reference genomes or annotations are downloaded, and read mapping indexes are built.
Since such steps are independent of the actual data or measurements that are analyzed, but still computationally or timely expensive to conduct, it has been common practice to externalize their computation and assume the presence of the resulting files before execution of a workflow.

From version 5.8.0 on, Snakemake offers a way to keep those steps inside the actual analysis without requiring from redundant computations.
By hashing all steps, parameters, software stacks (in terms of conda environments or containers), and raw input required up to a certain job via a `Merkle tree <https://en.wikipedia.org/wiki/Merkle_tree>`_, Snakemake is able to recognize **before** the computation whether a certain result is already available in a central cache on the same system.
**Note that this is explicitly intended for caching results between workflows! There is no need to use this feature to avoid redundant computations within a workflow. Snakemake does this already out of the box.**

Such caching has to be explicitly activated per rule, which can be done via the command line interface.
For example,

.. code-block:: console

    $ export SNAKEMAKE_OUTPUT_CACHE=/mnt/snakemake-cache/
    $ snakemake --cache download_data create_index

would instruct Snakemake to cache and reuse the results of the rules ``download_data`` and ``create_index``.
The environment variable definition that happens in the first line (defining the location of the cache) should of course be done only once and system wide in reality.
When Snakemake is executed without a shared filesystem (e.g., in the cloud, see :ref:`tutorial-cloud`), the environment variable has to point to a location compatible with the given remote provider (e.g. an S3 or Google Storage bucket).
In any case, the provided location should be shared between all workflows of your group, institute or computing environment, in order to benefit from the reuse of previously obtained intermediate results.

Alternatively, rules can be marked as eligible for caching via the ``cache`` directive:

.. code-block:: python

    rule download_data:
        output:
            "results/data/worldcitiespop.csv"
        cache: True # allowed values: "all", "omit-software", True
        shell:
            "curl -L https://burntsushi.net/stuff/worldcitiespop.csv > {output}"

Here, the given value defines what information shall be considered for calculating the hash value.
With ``"all"`` or ``True``, all relevant rule information is used as outlined above (this is the recommended default).
With ``"omit-software"``, the software stack is not considered, which is useful if the software stack is not relevant for the result (e.g., if the rule is only a data download).

For workflows defining cache rules like this, it is enough to invoke Snakemake with

.. code-block:: console

    $ snakemake --cache

without explicit rulenames listed.

Note that rules with multiple output files are only eligible for caching if they use :ref:`multiext output files <snakefiles-multiext>` or if all output files are named.
Also note that the rules need to retrieve all their parameters via the ``params`` directive (except input files).
It is not allowed to directly use ``wildcards``, ``config`` or any global variable in the shell command or script, because these are not captured in the hash (otherwise, reuse would be unnecessarily limited).

Input roles and cache keys
--------------------------

The provenance hash is derived from the contents of the primary inputs, never
from their paths: moving an input file to another directory while keeping its
bytes and its role unchanged yields the same cache key. When inputs are given
explicit names (e.g. ``input: master=..., exclusions=...``), each input is
hashed together with its role name. Swapping two differently named inputs
therefore changes the key even if both file contents stay the same, while
unnamed inputs keep the historical order-independent behavior. Changing any
``params`` value (such as a validity threshold) likewise changes the key and
forces recomputation.

Atomic grouped releases
-----------------------

All outputs of a cacheable rule are treated as one immutable *generation*
under the provenance hash. A generation becomes visible only after every
output has been staged into a private directory and a release manifest has
been committed atomically; readers therefore always observe a complete
generation, never a half-written mix of outputs. Each cache key stores its
generations under ``generations/<hash>/<release id>/`` with a ``RELEASE``
manifest, while the historical flat ``<hash><ext>`` entries are kept as
symlinks into the currently released generation.

Working directories pin the generation they first observe in
``.snakemake/cache_pins/``. When a repair publishes a new generation for an
existing key, already running readers keep using the complete generation they
started with (including every grouped output), while newly started readers
obtain the new generation. A publisher that is interrupted after writing only
part of a generation never commits a manifest, so the partial generation
remains a cache miss and the previous complete generation stays readable.
Publishing takes a short per-key lock only while swapping metadata; copying
output data does not block readers of an already complete generation.

Setting ``SNAKEMAKE_CACHE_WAIT_FILE`` to a path makes a publisher pause after
placing the first output of a new generation until that path appears. This is
a test/debug hook for reproducing interrupted or slow repairs.

A forced run (``--forcerun``/``--forceall``) bypasses cache retrieval and
recomputes the affected rules, but still publishes the fresh results back to
the cache as a new generation.

Also note that Snakemake will store everything in the cache as readable and writeable for **all users** on the system (except in the remote case, where permissions are not enforced and depend on your storage configuration).
Hence, caching is not intended for private data, just for steps that deal with publicly available resources.

Finally, be aware that the implementation should be considered **experimental** until this note is removed.
