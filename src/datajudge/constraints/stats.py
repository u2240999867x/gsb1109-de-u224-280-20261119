from __future__ import annotations

import math
import warnings

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from .base import Constraint, TestResult

# Below this sample size the approximate p-value is unreliable, so a KS test
# must not pass on the strength of the test statistic alone.
DEFAULT_MIN_SAMPLES = 35


class KolmogorovSmirnov2Sample(Constraint):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        significance_level: float = 0.05,
        name: str | None = None,
        cache_size=None,
        *,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ):
        if min_samples < 1:
            raise ValueError("min_samples has to be at least 1.")
        self._significance_level = significance_level
        self._min_samples = min_samples
        super().__init__(ref, ref2=ref2, name=name, cache_size=cache_size)

    @staticmethod
    def _approximate_p_value(
        d: float,
        n_samples: int,
        m_samples: int,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ) -> float | None:
        """Calculate the approximate p-value.

        The computation is according to
        'A procedure to find exact critical values of Kolmogorov-Smirnov Test', Silvia Fachinetti, 2009

        Note: For environments with `scipy` installed, this method will return a quasi-exact p-value.
        """
        # The normal approximation is not reliable for small samples. In that
        # case no p-value is reported, and callers must rely on the explicit
        # sample-size gate instead of treating a missing p-value as a pass.
        samples = min(n_samples, m_samples)
        if samples < min_samples:
            warnings.warn(
                "Approximating the p-value is not accurate enough for sample size "
                f"< {min_samples}"
            )
            return None

        # if scipy is installed, accurately calculate the p_value using the full distribution
        try:
            from scipy.stats.distributions import kstwo

            approx_p = kstwo.sf(
                d, round((n_samples * m_samples) / (n_samples + m_samples))
            )
        except ModuleNotFoundError:
            d_alpha = d * math.sqrt(samples)
            approx_p = 2 * math.exp(-(d_alpha**2))

        # clamp value to [0, 1]
        return 1.0 if approx_p > 1.0 else 0.0 if approx_p < 0.0 else approx_p

    @staticmethod
    def _check_acceptance(
        d_statistic: float, n_samples: int, m_samples: int, accepted_level: float
    ) -> bool:
        """
        Check whether the null hypothesis can be rejected for an accepted significance level.

        `d_statistic is the test statistic of interest, and `n_samples` and `m_samples`
        correspond to the respective sample sizes.

        For more information, check out the `Wikipedia entry <https://w.wiki/5May>`_.
        """

        def c(alpha: float):
            return math.sqrt(-math.log(alpha / 2.0 + 1e-10) * 0.5)

        threshold = c(accepted_level) * math.sqrt(
            (n_samples + m_samples) / (n_samples * m_samples)
        )
        return d_statistic <= threshold

    @staticmethod
    def _calculate_statistic(
        engine: sa.engine.Engine,
        ref1: DataReference,
        ref2: DataReference,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ) -> tuple[float, float | None, int, int, list[sa.Select]]:
        # retrieve test statistic d, as well as sample sizes m and n
        d_statistic, ks_selections = db_access.get_ks_2sample(
            engine,
            ref1,
            ref2,
        )

        n_samples, n_selections = db_access.get_row_count(engine, ref1)
        m_samples, m_selections = db_access.get_row_count(engine, ref2)

        # calculate approximate p-value
        p_value = KolmogorovSmirnov2Sample._approximate_p_value(
            d_statistic, n_samples, m_samples, min_samples
        )

        selections = n_selections + m_selections + ks_selections
        return d_statistic, p_value, n_samples, m_samples, selections

    def _insufficient_sample_message(
        self, d_statistic: float, n_samples: int, m_samples: int
    ) -> str:
        return (
            "Insufficient sample size for the 2-sample Kolmogorov-Smirnov test: "
            f"got {n_samples} and {m_samples} rows, but at least "
            f"{self._min_samples} per sample are required "
            f"(observed d={d_statistic}). "
            "The test cannot be passed on the p-value alone, so it is inconclusive "
            "and therefore treated as a failure."
        )

    def test(self, engine: sa.engine.Engine) -> TestResult:
        if self._ref2 is None:
            raise ValueError("KolmogorovSmirnov2Sample requires ref2.")
        (
            d_statistic,
            p_value,
            n_samples,
            m_samples,
            selections,
        ) = self._calculate_statistic(
            engine,
            self._ref,
            self._ref2,
            self._min_samples,
        )

        queries = None
        if selections:
            queries = [
                str(selection.compile(engine, compile_kwargs={"literal_binds": True}))
                for selection in selections
            ]

        if min(n_samples, m_samples) < self._min_samples:
            return TestResult.failure(
                self._insufficient_sample_message(d_statistic, n_samples, m_samples),
                self.get_description(),
                queries,
                sample_sizes=(n_samples, m_samples),
            )

        result = self._check_acceptance(
            d_statistic, n_samples, m_samples, self._significance_level
        )

        assertion_text = (
            f"Null hypothesis (H0) for the 2-sample Kolmogorov-Smirnov test was rejected, i.e., "
            f"the two samples ({self._ref} and {self._target_prefix}) "
            f"do not originate from the same distribution. "
            f"The test results are d={d_statistic}, n_samples={n_samples}, "
            f"m_samples={m_samples}"
        )
        if p_value is not None:
            assertion_text += f" and {p_value=}"
        assertion_text += "."

        if not result:
            return TestResult.failure(
                assertion_text,
                self.get_description(),
                queries,
                sample_sizes=(n_samples, m_samples),
            )

        return TestResult.success((n_samples, m_samples))
