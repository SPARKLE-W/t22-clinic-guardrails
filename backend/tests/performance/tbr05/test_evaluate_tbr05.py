import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('06_evaluate_tbr05.py')
SPEC = importlib.util.spec_from_file_location('tbr05_evaluator', SCRIPT)
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


def evidence():
    summary = {'concurrency': 360, 'warmup_seconds': 300, 'formal_seconds': 600,
               'only_group': None, 'actual_duration_seconds': 900}
    kinds = evaluator.KINDS
    requests = []
    tiers = ('1-20', '50-200', '1000-4000')
    for index in range(8000):
        minute = index // 800
        within_minute = index % 800
        ended = 300 + minute * 60 + .001 + within_minute * 59.998 / 799
        started = ended - .1
        kind = kinds[index % len(kinds)]
        row = {'request_id': index, 'kind': kind, 'started': started,
               'ended': ended, 'status': 200, 'ok': True}
        if kind in evaluator.EXPORT_KINDS:
            if kind == 'summary_export':
                expected_count, rows, tier = 12, 1, 'summary'
            else:
                tier = tiers[(index // len(kinds)) % len(tiers)]
                expected_count = rows = {'1-20': 3, '50-200': 100, '1000-4000': 2000}[tier]
            row.update({'tier': tier, 'expected_count': expected_count,
                        'record_count': expected_count, 'rows': rows,
                        'bytes': 256, 'content_length': 256,
                        'header_byte_count': 256, 'declared_sha256': 'a' * 64,
                        'sha256_match': True})
        requests.append(row)
    inflight = [{'seconds': 300 + index * .5, 'in_flight': 360}
                for index in range(1200)]
    return summary, requests, inflight


class Tbr05EvaluatorTests(unittest.TestCase):
    def test_accepts_complete_protocol_evidence_and_reports_export_tiers(self):
        report = evaluator.evaluate(*evidence())
        self.assertTrue(report['passed'], report['failures'])
        self.assertEqual(10, len(report['minutes']))
        self.assertTrue(report['inflight']['exact_360_proven'])
        self.assertIn('1000-4000', report['exports_by_scope_and_size']['clinical_export'])

    def test_export_tier_p95_is_reported_but_frozen_gate_is_per_export_class(self):
        summary, requests, inflight = evidence()
        slow_tier = 0
        for row in requests:
            if row['kind'] == 'clinical_export' and row['tier'] == '1-20' and slow_tier < 30:
                row['ended'] = row['started'] + .5
                slow_tier += 1
        report = evaluator.evaluate(summary, requests, inflight)
        self.assertGreater(report['exports_by_scope_and_size']['clinical_export']['1-20']['p95_ms'], 280)
        self.assertTrue(report['passed'], report['failures'])

    def test_rejects_incomplete_export_header_evidence(self):
        summary, requests, inflight = evidence()
        export = next(row for row in requests if row['kind'] == 'clinical_export')
        export['content_length'] = None
        report = evaluator.evaluate(summary, requests, inflight)
        self.assertFalse(report['passed'])
        self.assertIn('export_incomplete:4:clinical_export', report['failures'])

    def test_rejects_missing_continuous_360_inflight_sample(self):
        summary, requests, inflight = evidence()
        inflight[400]['in_flight'] = 359
        report = evaluator.evaluate(summary, requests, inflight)
        self.assertFalse(report['inflight']['exact_360_proven'])
        self.assertIn('360_continuous_inflight_not_proven', report['failures'])

    def test_rejects_per_class_p95_above_limit(self):
        summary, requests, inflight = evidence()
        slow_queries = [row for row in requests if row['kind'] == 'query'][:100]
        for row in slow_queries:
            row['ended'] = row['started'] + .6
        report = evaluator.evaluate(summary, requests, inflight)
        self.assertFalse(report['passed'])
        self.assertIn('p95_over_limit_or_missing:query', report['failures'])


if __name__ == '__main__':
    unittest.main()
