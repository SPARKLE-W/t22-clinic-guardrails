import csv
import hashlib
import io
import unittest
from test_load_capture import load


class CsvStreamValidationTests(unittest.TestCase):
    def capture(self, body, count, kind='clinical_export', expected=None, overrides=None):
        row = {'kind': kind, 'status': 200, 'expected_count': count if expected is None else expected}
        headers = {'Content-Length': str(len(body)), 'X-Byte-Count': str(len(body)),
                   'X-Content-SHA256': hashlib.sha256(body).hexdigest(), 'X-Record-Count': str(count)}
        headers.update(overrides or {})
        return row, body, headers, None

    def encoded(self, rows):
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\r\n').writerows(rows)
        return stream.getvalue().encode('utf-8')

    def test_complete_csv_validates_without_bulk_body_decode(self):
        class NoBulkDecode(bytes):
            def decode(self, *args, **kwargs):
                raise AssertionError('full response Unicode copy')
        body = NoBulkDecode(self.encoded([['record_ref', 'note'], ['one', '中文'], ['two', 'second']]))
        result = load.validate_capture(self.capture(body, 2))
        self.assertTrue(result['ok'], result)
        self.assertEqual(2, result['rows'])
        self.assertTrue(result['sha256_match'])

    def test_quoted_newlines_blank_rows_and_split_utf8_are_counted_completely(self):
        body = self.encoded([['record_ref', 'note'], [], ['one', 'x' * 8190 + '中文,\r\n引号"'],
                             [], ['two', '尾部'], []])
        for kind in ('clinical_export', 'detail_export'):
            with self.subTest(kind=kind):
                result = load.validate_capture(self.capture(body, 2, kind))
                self.assertTrue(result['ok'], result)
                self.assertEqual(2, result['rows'])

    def test_summary_values_remain_checked(self):
        body = self.encoded([['rule_ref', 'hit_count', 'alert_count', 'unhandled_count', 'note'],
                             ['SR-02', 3, 3, 3, '中文,\r\nquoted"']])
        self.assertTrue(load.validate_capture(self.capture(body, 3, 'summary_export'))['ok'])
        wrong = self.encoded([['rule_ref', 'hit_count', 'alert_count', 'unhandled_count'], ['SR-02', 3, 2, 3]])
        self.assertFalse(load.validate_capture(self.capture(wrong, 3, 'summary_export'))['ok'])

    def test_late_invalid_utf8_and_legacy_bare_cr_fail(self):
        for body, code in [(b'record_ref\r\n' + b'valid\r\n' * 3000 + b'\xff\r\n', 'UnicodeDecodeError'),
                           (b'record_ref\rvalue\r', 'Error')]:
            with self.subTest(code=code):
                result = load.validate_capture(self.capture(body, 3001))
                self.assertFalse(result['ok'])
                self.assertEqual(code, result['code'])

    def test_length_sha_count_and_late_rows_are_not_skipped(self):
        body = self.encoded([['record_ref'], ['one'], ['two']])
        for overrides in ({'Content-Length': str(len(body) - 1)}, {'X-Byte-Count': '-1'},
                          {'X-Content-SHA256': '0' * 64}, {'X-Record-Count': '1'}):
            with self.subTest(overrides=overrides):
                result = load.validate_capture(self.capture(body, 2, overrides=overrides))
                self.assertFalse(result['ok'])
        self.assertFalse(load.validate_capture(self.capture(body, 1))['ok'])
