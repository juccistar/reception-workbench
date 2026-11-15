import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'benchmark-040.py'
spec = importlib.util.spec_from_file_location('benchmark_040', SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class BenchmarkTests(unittest.TestCase):
    def test_fixture_pagination_and_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = benchmark.Repository(Path(directory) / 'test.sqlite3', 'synthetic')
            try:
                benchmark.populate(repo, 3, 240)
                for c in range(3):
                    self.assertEqual(benchmark.verify(repo, f'conversation-{c:04d}'), 80)
                messages = repo.list('message', conversation_id='conversation-0000')
                self.assertTrue(any(m['timestamp_ms'] is None for m in messages))
                times = [m['timestamp_ms'] for m in messages]
                self.assertLess(len(set(times)), len(times))
                self.assertEqual(len({m['case_id'] for m in messages}), 2)
            finally:
                repo.close()

    def test_nearest_rank_p95_and_parsed_rows(self):
        result = benchmark.summary(list(range(1, 201)), 80)
        self.assertEqual(result['p95_ms'], 190)
        self.assertEqual(result['median_ms'], 100.5)
        self.assertEqual(result['returned_rows_per_call'], 50)
        self.assertEqual(result['parsed_rows_per_call'], 80)


if __name__ == '__main__':
    unittest.main()
