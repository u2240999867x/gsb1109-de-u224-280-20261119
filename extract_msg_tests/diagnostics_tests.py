__all__ = [
    'DiagnosticsTests',
]


import hashlib
import pathlib
import tempfile
import unittest

from extract_msg.diagnostics import (
    diagnose_directory, format_markdown, generate_fixtures, manifest_for
)


def _report(diagnosis, name):
    return next(report for report in diagnosis['reports'] if report['name'] == name)


def _comparison(diagnosis, scenario):
    return next(
        comparison for comparison in diagnosis['comparisons']
        if comparison['scenario'] == scenario
    )


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.samples = pathlib.Path(cls._tmp.name) / 'samples'
        cls.manifest = generate_fixtures(cls.samples)
        cls.diagnosis = diagnose_directory(cls.samples)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def testManifestIsStable(self):
        manifest = manifest_for(self.samples)
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(len(manifest['fixtures']), 15)
        names = {entry['name'] for entry in manifest['fixtures']}
        self.assertIn('regular/missing-native__rtf-no-html.msg', names)
        self.assertIn('signed/empty-native__rtf-no-html.msg', names)
        second = pathlib.Path(self._tmp.name) / 'second'
        generate_fixtures(second)
        for entry in manifest['fixtures']:
            data = (second / entry['path']).read_bytes()
            self.assertEqual(entry['sha256'], hashlib.sha256(data).hexdigest())

    def testMissingAndEmptyDifferOnRegularPath(self):
        missing = _report(self.diagnosis, 'regular/missing-native__rtf-no-html.msg')
        empty = _report(self.diagnosis, 'regular/empty-native__rtf-no-html.msg')

        self.assertEqual(missing['native_html']['state'], 'missing')
        self.assertEqual(empty['native_html']['state'], 'empty')
        self.assertTrue(missing['path']['rtf_html_attempted'])
        self.assertFalse(empty['path']['rtf_html_attempted'])
        self.assertTrue(missing['path']['plain_fallback_used'])
        self.assertTrue(empty['path']['plain_fallback_used'])

        # The final HTML is the same recovered plain text, but the cause and
        # branch history are different.
        self.assertEqual(missing['html_body']['sha256'], empty['html_body']['sha256'])
        self.assertIn('NATIVE_HTML_MISSING', missing['triage_codes'])
        self.assertIn('NATIVE_HTML_EMPTY', empty['triage_codes'])

    def testEmptyNativeHtmlShortCircuitsSignedPath(self):
        report = _report(self.diagnosis, 'signed/empty-native__rtf-no-html.msg')
        self.assertEqual(report['native_html']['state'], 'empty')
        self.assertEqual(report['html_body']['length'], 0)
        self.assertFalse(report['path']['rtf_html_attempted'])
        self.assertFalse(report['path']['plain_fallback_attempted'])
        self.assertTrue(report['path']['empty_native_short_circuited'])
        self.assertIn('NO_HTML_OUTPUT', report['triage_codes'])

    def testSignedMissingNativeDoesNotUsePlainFallback(self):
        report = _report(self.diagnosis, 'signed/missing-native__rtf-no-html.msg')
        self.assertTrue(report['path']['rtf_html_attempted'])
        self.assertEqual(report['path']['rtf_html_result'], 'no-html')
        self.assertTrue(report['path']['plain_text_available'])
        self.assertFalse(report['path']['plain_fallback_used'])
        self.assertEqual(report['path']['html_source'], 'none')
        self.assertEqual(report['html_body']['length'], 0)
        self.assertIn('PLAIN_TEXT_NOT_USED', report['triage_codes'])

    def testRtfPathHasThreeDistinctOutcomes(self):
        rtf_html = _report(self.diagnosis, 'regular/missing-native__rtf-html.msg')
        rtf_none = _report(self.diagnosis, 'regular/missing-native__rtf-no-html.msg')
        no_rtf = _report(self.diagnosis, 'regular/missing-native__no-rtf.msg')

        self.assertEqual(rtf_html['path']['rtf_html_result'], 'html')
        self.assertEqual(rtf_html['path']['html_source'], 'rtf-html')
        self.assertEqual(rtf_none['path']['rtf_html_result'], 'no-html')
        self.assertEqual(no_rtf['path']['rtf_html_result'], 'not-attempted')
        self.assertIn('RTF_HTML_USED', rtf_html['triage_codes'])
        self.assertIn('RTF_HTML_UNAVAILABLE', rtf_none['triage_codes'])
        self.assertIn('RTF_MISSING', no_rtf['triage_codes'])

    def testPlainFallbackHtmlDiffersByPath(self):
        regular = _report(self.diagnosis, 'regular/missing-native__no-rtf.msg')
        signed = _report(self.diagnosis, 'signed/missing-native__no-rtf.msg')
        self.assertEqual(regular['path']['html_source'], 'plain-text')
        self.assertEqual(signed['path']['html_source'], 'plain-text')
        self.assertTrue(regular['html_body']['preview'].endswith('</body></html>'))
        self.assertTrue(signed['html_body']['preview'].endswith('</body></head>'))
        self.assertNotEqual(regular['html_body']['sha256'], signed['html_body']['sha256'])

    def testFirstDivergenceProvesFork(self):
        empty = _comparison(self.diagnosis, 'empty-native__rtf-no-html.msg')
        self.assertEqual(empty['first_divergence']['stage'], 'plain_fallback_attempted')
        self.assertEqual(empty['first_divergence']['regular'], True)
        self.assertEqual(empty['first_divergence']['signed'], False)

        no_rtf = _comparison(self.diagnosis, 'missing-native__no-rtf.msg')
        self.assertEqual(no_rtf['first_divergence']['stage'], 'html_length')

        same = _comparison(self.diagnosis, 'missing-native__rtf-html.msg')
        self.assertFalse(same['diverges'])

    def testEncodingGuessRecordsObtainedBytesAndWrongResult(self):
        report = _report(self.diagnosis, 'regular/native-html-mojibake.msg')
        decoding = report['decoding']
        self.assertTrue(decoding['strict_utf8']['ok'])
        self.assertEqual(decoding['chosen_encoding'], 'windows-1252')
        self.assertEqual(decoding['text_preview'], 'ä¸­æ–‡æ­£æ–‡')
        self.assertIn('ENCODING_GUESS_MISMATCH', report['triage_codes'])
        self.assertEqual(
            decoding['first_decode_anomaly']['kind'], 'wrong_encoding_guess',
        )
        self.assertIsNotNone(decoding['raw_sha256'])
        self.assertNotEqual(decoding['raw_hex_sample'], None)

    def testReplacementCharactersAreNotMissingBody(self):
        report = _report(self.diagnosis, 'regular/native-html-replacement.msg')
        decoding = report['decoding']
        self.assertEqual(report['native_html']['state'], 'bytes')
        self.assertTrue(decoding['contains_replacement_characters'])
        self.assertEqual(decoding['first_decoding_exception']['byte_offset'], 53)
        self.assertEqual(decoding['first_decoding_exception']['bytes'], 'e9')
        self.assertEqual(
            decoding['first_decode_anomaly']['kind'], 'unicode_decode_error',
        )
        self.assertIn('REPLACEMENT_CHARACTERS', report['triage_codes'])
        self.assertNotIn('NATIVE_HTML_MISSING', report['triage_codes'])
        self.assertNotIn('REPLACEMENT_CHARACTERS', _report(
            self.diagnosis, 'regular/native-html-ok.msg',
        )['triage_codes'])

    def testSignedMimeHtmlBranchIsVisible(self):
        report = _report(
            self.diagnosis,
            'signed/missing-native__rtf-no-html__mime-html.msg',
        )
        self.assertEqual(report['path']['html_source'], 'signed-mime-html')
        self.assertEqual(report['html_body']['length'], 44)
        self.assertFalse(report['path']['rtf_html_attempted'])
        self.assertIn('SIGNED_MIME_HTML_USED', report['triage_codes'])

    def testDiagnosisDoesNotModifySamples(self):
        before = {
            entry['path']: entry['sha256'] for entry in self.manifest['fixtures']
        }
        diagnose_directory(self.samples)
        for relative, expected in before.items():
            data = (self.samples / relative).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)

    def testMarkdownReportIsReviewable(self):
        text = format_markdown(self.diagnosis)
        self.assertIn('regular/missing-native__rtf-no-html.msg', text)
        self.assertIn('plain_fallback_attempted', text)
        self.assertIn('NATIVE_HTML_EMPTY', text)
