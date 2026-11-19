import unittest

from csvkit.hyperloglog import HyperLogLog


class TestHyperLogLog(unittest.TestCase):

    def test_estimate_is_accurate(self):
        for cardinality in (10, 100, 1000, 10000, 100000):
            with self.subTest(cardinality=cardinality):
                estimator = HyperLogLog()

                for i in range(cardinality):
                    estimator.add(f'value-{i}')

                self.assertLessEqual(abs(estimator.count() - cardinality) / cardinality, 0.05)

    def test_estimate_is_independent_of_chunk_size(self):
        values = [f'value-{i}' for i in range(20000)]

        expected = HyperLogLog()
        for value in values:
            expected.add(value)

        for chunk_size in (1, 2, 13, 500, 20000):
            with self.subTest(chunk_size=chunk_size):
                estimator = HyperLogLog()

                for i in range(0, len(values), chunk_size):
                    chunk = HyperLogLog()

                    for value in values[i:i + chunk_size]:
                        chunk.add(value)

                    estimator.merge(chunk)

                self.assertEqual(estimator.registers, expected.registers)
                self.assertEqual(estimator.count(), expected.count())

    def test_estimate_is_independent_of_merge_order(self):
        values = [f'value-{i}' for i in range(5000)]

        expected = HyperLogLog()
        for value in values:
            expected.add(value)

        chunks = [HyperLogLog() for _ in range(10)]

        for i, value in enumerate(values):
            chunks[i % len(chunks)].add(value)

        estimator = HyperLogLog()
        for chunk in reversed(chunks):
            estimator.merge(chunk)

        self.assertEqual(estimator.registers, expected.registers)

    def test_estimate_is_deterministic_across_instances(self):
        first = HyperLogLog()
        second = HyperLogLog()

        for value in ('a', 'b', 'c', 'a'):
            first.add(value)
            second.add(value)

        self.assertEqual(first.registers, second.registers)

    def test_small_cardinalities(self):
        estimator = HyperLogLog()

        for value in ('a', 'a', 'b', 'c'):
            estimator.add(value)

        self.assertEqual(estimator.count(), 3)

    def test_merge_rejects_different_precision(self):
        with self.assertRaises(ValueError):
            HyperLogLog(precision=10).merge(HyperLogLog(precision=12))

    def test_invalid_precision(self):
        for precision in (3, 19):
            with self.subTest(precision=precision):
                with self.assertRaises(ValueError):
                    HyperLogLog(precision=precision)

    def test_equal_numbers_with_different_types(self):
        # 1 and True are distinct values, as in agate.
        estimator = HyperLogLog()
        estimator.add(True)
        estimator.add(1)

        self.assertEqual(estimator.count(), 2)
