from __future__ import annotations

import math

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from .base import Constraint, TestResult

MIN_SAMPLE_SIZE = 35


class KolmogorovSmirnov2Sample(Constraint):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        significance_level: float = 0.05,
        name: str | None = None,
        cache_size=None,
        min_sample_size: int = MIN_SAMPLE_SIZE,
    ):
        if min_sample_size < 1:
            raise ValueError(
                f"The minimum usable sample size has to be a positive integer, "
                f"not {min_sample_size}."
            )
        self._significance_level = significance_level
        self._min_sample_size = min_sample_size
        super().__init__(ref, ref2=ref2, name=name, cache_size=cache_size)

    @staticmethod
    def _approximate_p_value(d: float, n_samples: int, m_samples: int) -> float:
        """Calculate the approximate p-value.

        The computation is according to
        'A procedure to find exact critical values of Kolmogorov-Smirnov Test', Silvia Fachinetti, 2009

        Note: For environments with `scipy` installed, this method will return a quasi-exact p-value.

        Only call this method when both samples contain at least ``MIN_SAMPLE_SIZE``
        observed values; the approximation is not accurate for smaller samples.
        """
        # if scipy is installed, accurately calculate the p_value using the full distribution
        try:
            from scipy.stats.distributions import kstwo

            approx_p = kstwo.sf(
                d, round((n_samples * m_samples) / (n_samples + m_samples))
            )
        except ModuleNotFoundError:
            d_alpha = d * math.sqrt(min(n_samples, m_samples))
            approx_p = 2 * math.exp(-(d_alpha**2))

        # clamp value to [0, 1]
        return 1.0 if approx_p > 1.0 else 0.0 if approx_p < 0.0 else approx_p

    def _has_sufficient_samples(self, n_samples: int, m_samples: int) -> bool:
        """Check whether both sides provide enough observed values for a decision."""
        return min(n_samples, m_samples) >= self._min_sample_size

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
    ) -> tuple[float | None, float | None, int, int, int, int, list[sa.Select]]:
        # retrieve the observed and total sample sizes first; the statistic can
        # only be computed when both samples contain an observed value
        n_samples, n_total, n_selections = db_access.get_observed_row_count(
            engine, ref1
        )
        m_samples, m_total, m_selections = db_access.get_observed_row_count(
            engine, ref2
        )

        if n_samples == 0 or m_samples == 0:
            d_statistic: float | None = None
            ks_selections: list[sa.Select] = []
        else:
            d_statistic, ks_selections = db_access.get_ks_2sample(
                engine,
                ref1,
                ref2,
            )

        # calculate approximate p-value only when a statistic exists and enough
        # observed values are available
        if d_statistic is not None and min(n_samples, m_samples) >= MIN_SAMPLE_SIZE:
            p_value = KolmogorovSmirnov2Sample._approximate_p_value(
                d_statistic, n_samples, m_samples
            )
        else:
            p_value = None

        selections = n_selections + m_selections + ks_selections
        return (
            d_statistic,
            p_value,
            n_samples,
            m_samples,
            n_total,
            m_total,
            selections,
        )

    def test(self, engine: sa.engine.Engine) -> TestResult:
        if self._ref2 is None:
            raise ValueError("KolmogorovSmirnov2Sample requires ref2.")
        (
            d_statistic,
            p_value,
            n_samples,
            m_samples,
            n_total,
            m_total,
            selections,
        ) = self._calculate_statistic(
            engine,
            self._ref,
            self._ref2,
        )
        n_unobserved = n_total - n_samples
        m_unobserved = m_total - m_samples

        queries = None
        if selections:
            queries = [
                str(selection.compile(engine, compile_kwargs={"literal_binds": True}))
                for selection in selections
            ]

        if not self._has_sufficient_samples(n_samples, m_samples):
            assertion_text = (
                f"Insufficient evidence for the 2-sample Kolmogorov-Smirnov test: both "
                f"samples ({self._ref} and {self._ref2}) need at least "
                f"{self._min_sample_size} observed values (the minimum usable sample size "
                f"per side), but only {n_samples} and {m_samples} observed values were "
                f"found, with {n_unobserved} and {m_unobserved} unobserved (NULL) values "
                f"respectively."
            )
            if d_statistic is not None:
                assertion_text += (
                    f" The test statistic is d={d_statistic}; no decision is taken "
                    f"based on a p-value."
                )
            return TestResult.failure(
                assertion_text,
                self.get_description(),
                queries,
            )

        if d_statistic is None:
            return TestResult.failure(
                "The 2-sample Kolmogorov-Smirnov test statistic could not be "
                "computed because at least one sample contains no observed "
                "(non-NULL) values.",
                self.get_description(),
                queries,
            )
        result = self._check_acceptance(
            d_statistic, n_samples, m_samples, self._significance_level
        )

        assertion_text = (
            f"Null hypothesis (H0) for the 2-sample Kolmogorov-Smirnov test was rejected, i.e., "
            f"the two samples ({self._ref} and {self._ref2}) "
            f"do not originate from the same distribution. "
            f"The test results are d={d_statistic}"
        )
        if p_value is not None:
            assertion_text += f" and {p_value=}"
        assertion_text += (
            f", based on {n_samples} and {m_samples} observed values"
            f" ({n_unobserved} and {m_unobserved} unobserved (NULL) values)."
        )

        if not result:
            return TestResult.failure(
                assertion_text,
                self.get_description(),
                queries,
            )

        return TestResult.success()
