from __future__ import annotations


"""
Read-only body-path diagnostics for the MSG archive conversion workflow.

The functions in this module intentionally use the public ``openMsg`` entry
point and the normal body properties (``body``, ``htmlBody`` and
``htmlBodyPrepared``).  They add observation only: no stream is rewritten, no
extraction setting is changed, and the original archive is never altered.
"""


__all__ = [
    'compare_reports',
    'diagnose_directory',
    'diagnose_fixture',
    'format_markdown',
]


import hashlib
import html as html_module
import logging
import pathlib

from typing import Any, Dict, List, Optional

import bs4

from .. import __version__
from ..enums import DeencapType
from ..msg_classes import MessageSignedBase
from ..open_msg import openMsg


_NATIVE_HTML = '__substg1.0_10130102'
_PLAIN = '__substg1.0_1000'
_RTF = '__substg1.0_10090102'

_TRACE_LOGGER = 'extract_msg.msg_classes.message_base'


def _sha256(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def _hex_sample(data: Optional[bytes], length: int = 80) -> Optional[str]:
    if data is None:
        return None
    sample = data[:length]
    suffix = '' if len(data) <= length else '...'
    return sample.hex() + suffix


def _preview(value: Any, length: int = 220) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'backslashreplace')
    text = str(value)
    return text if len(text) <= length else text[:length] + '...'


def _strict_decode(data: bytes, encoding: str) -> Dict[str, Any]:
    try:
        return {
            'encoding': encoding,
            'ok': True,
            'text': data.decode(encoding),
            'error': None,
        }
    except (UnicodeDecodeError, LookupError) as error:
        result = {
            'encoding': encoding,
            'ok': False,
            'text': None,
            'error': f'{type(error).__name__}: {error}',
        }
        if isinstance(error, UnicodeDecodeError):
            result['error_start'] = error.start
            result['error_end'] = error.end
            result['error_reason'] = error.reason
            result['error_bytes'] = data[error.start:error.end].hex()
        return result


def _first_character_difference(left: Optional[str], right: Optional[str]) -> Optional[Dict[str, Any]]:
    if left is None or right is None:
        return None
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return {
                'index': index,
                'left': left[max(0, index - 16):index + 16],
                'right': right[max(0, index - 16):index + 16],
            }
    if len(left) != len(right):
        return {
            'index': limit,
            'left': left[max(0, limit - 16):limit + 16],
            'right': right[max(0, limit - 16):limit + 16],
        }
    return None


def _parse_native_html(raw: bytes, parser_log: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Parse native HTML using the same constructor the save path uses first.

    ``injectHtmlHeader`` calls ``validateHtml`` before a prepared body exists,
    so the initial parse is ``BeautifulSoup(..., 'html.parser')`` with no
    forced encoding.  The resulting sniffed/declared encoding is reported so a
    guess can be compared with the bytes actually obtained.
    """
    soup = bs4.BeautifulSoup(raw, 'html.parser')
    text = soup.get_text()
    chosen = soup.original_encoding or soup.declared_html_encoding
    utf8 = _strict_decode(raw, 'utf-8')
    chosen_decode = _strict_decode(raw, chosen) if chosen else None
    replacement_count = text.count('\ufffd')
    replacement_index = text.find('\ufffd')

    result: Dict[str, Any] = {
        'raw_size': len(raw),
        'raw_sha256': _sha256(raw),
        'raw_hex_sample': _hex_sample(raw),
        'sniffed_encoding': soup.original_encoding,
        'declared_encoding': soup.declared_html_encoding,
        'chosen_encoding': chosen,
        'text_preview': _preview(text),
        'text': text,
        'text_length': len(text),
        'replacement_character_count': replacement_count,
        'first_replacement_index': None if replacement_index < 0 else replacement_index,
        'contains_replacement_characters': replacement_count > 0,
        'strict_utf8': {k: v for k, v in utf8.items() if k != 'text'},
        'strict_chosen': (
            {k: v for k, v in chosen_decode.items() if k != 'text'}
            if chosen_decode else None
        ),
        'first_decoding_exception': None,
        'parser_warnings': list(parser_log or ()),
        'issues': [],
    }

    for warning in result['parser_warnings']:
        if 'REPLACEMENT CHARACTER' in warning:
            result['issues'].append({
                'kind': 'parser_replacement_warning',
                'detail': warning,
            })
            break

    if not utf8['ok']:
        result['first_decoding_exception'] = {
            'stage': 'strict-utf8',
            'byte_offset': utf8.get('error_start'),
            'bytes': utf8.get('error_bytes'),
            'reason': utf8.get('error_reason'),
            'error': utf8['error'],
        }

    if replacement_count:
        result['issues'].append({
            'kind': 'replacement_characters',
            'detail': (
                f'{replacement_count} U+FFFD replacement character(s) visible '
                'after parsing the bytes that were obtained.'
            ),
            'raw_bytes_at_error': (
                utf8.get('error_bytes')
                if not utf8['ok'] else None
            ),
        })

    if (not utf8['ok'] and chosen and chosen.lower() not in ('utf-8', 'utf8')
            and chosen_decode and chosen_decode['ok']):
        result['issues'].append({
            'kind': 'encoding_guess_on_invalid_utf8',
            'detail': (
                f'UTF-8 is invalid at byte {utf8.get("error_start")}; the parser '
                f'guessed {chosen} instead.'
            ),
        })

    if (utf8['ok'] and chosen and chosen.lower() not in ('utf-8', 'utf8')
            and any(ord(char) > 127 for char in utf8['text'])):
        expected_text = bs4.BeautifulSoup(raw, 'html.parser', from_encoding='utf-8').get_text()
        difference = _first_character_difference(text, expected_text)
        issue = {
            'kind': 'encoding_guess_mismatch',
            'detail': (
                f'The bytes are strict UTF-8 but the parser chose {chosen}. '
                'The displayed text differs from the UTF-8 interpretation.'
            ),
            'utf8_text_preview': _preview(expected_text),
            'first_character_difference': difference,
        }
        if difference is not None and chosen_decode and chosen_decode['ok']:
            target = next(
                (char for char in expected_text if ord(char) > 127), None,
            )
            if target is not None:
                offset = raw.find(target.encode('utf-8'))
                if offset >= 0:
                    issue['first_difference_raw_byte_offset'] = offset
                    issue['first_difference_raw_bytes'] = raw[offset:offset + 12].hex()
        result['issues'].append(issue)
        if chosen_decode and chosen_decode['ok']:
            result['chosen_roundtrip_equals_raw'] = (
                chosen_decode['text'].encode(chosen) == raw
            )

    result['first_decode_anomaly'] = _first_decode_anomaly(result)
    return result


def _first_decode_anomaly(decoding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return the earliest observable decode problem, with a char and byte
    location where one can be determined.
    """
    if decoding.get('first_decoding_exception'):
        exception = decoding['first_decoding_exception']
        return {
            'stage': 'strict-decode',
            'kind': 'unicode_decode_error',
            'encoding': 'utf-8',
            'character_index': None,
            'raw_byte_offset': exception.get('byte_offset'),
            'raw_bytes': exception.get('bytes'),
            'detail': exception.get('error'),
        }

    if decoding.get('first_replacement_index') is not None:
        return {
            'stage': 'parser-output',
            'kind': 'replacement_character',
            'character_index': decoding['first_replacement_index'],
            'raw_byte_offset': None,
            'detail': 'The parser emitted U+FFFD for bytes it could not decode.',
        }

    for issue in decoding.get('issues', ()):
        if issue['kind'] == 'encoding_guess_mismatch':
            difference = issue.get('first_character_difference') or {}
            return {
                'stage': 'parser-encoding-choice',
                'kind': 'wrong_encoding_guess',
                'encoding': decoding.get('chosen_encoding'),
                'character_index': difference.get('index'),
                'raw_byte_offset': issue.get('first_difference_raw_byte_offset'),
                'raw_bytes': issue.get('first_difference_raw_bytes'),
                'detail': issue['detail'],
            }

    return None


def _parse_saved_html(saved: Optional[bytes], prepared_text: Optional[str] = None,
                      export_encoding: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not saved:
        return None
    soup = bs4.BeautifulSoup(saved, 'html.parser')
    strict_utf8 = _strict_decode(saved, 'utf-8')
    utf8_text = None
    if strict_utf8['ok']:
        utf8_text = bs4.BeautifulSoup(strict_utf8['text'], 'html.parser').get_text()

    without_header = bs4.BeautifulSoup(saved, 'html.parser')
    injected = without_header.find(id = 'injectedHeader')
    if injected is not None:
        injected.decompose()
    body_text = without_header.get_text()

    result = {
        'encoding': soup.original_encoding or soup.declared_html_encoding,
        'export_encoding': export_encoding,
        'text_preview': _preview(soup.get_text()),
        'text_length': len(soup.get_text()),
        'body_text_preview': _preview(body_text),
        'replacement_character_count': soup.get_text().count('\ufffd'),
        'strict_utf8_text_preview': _preview(utf8_text),
        'strict_utf8_ok': strict_utf8['ok'],
        'differs_from_utf8': utf8_text is not None and utf8_text != soup.get_text(),
    }
    if prepared_text:
        result['prepared_body_preserved'] = prepared_text.strip() in body_text
        if not result['prepared_body_preserved']:
            result['first_difference_from_prepared'] = {
                'prepared': _preview(prepared_text),
                'saved_body': _preview(body_text),
            }
    return result


def _parse_prepared_html(prepared: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """
    Interpret the prepared HTML both as the library emits it (UTF-8 bytes) and
    as a browser would when following its ``meta`` charset declaration.
    """
    if not prepared:
        return None
    strict_utf8 = _strict_decode(prepared, 'utf-8')
    intended_text = None
    if strict_utf8['ok']:
        intended_text = bs4.BeautifulSoup(strict_utf8['text'], 'html.parser').get_text()
    soup = bs4.BeautifulSoup(prepared, 'html.parser')
    declared_text = soup.get_text()
    return {
        'declared_encoding': soup.declared_html_encoding,
        'sniffed_encoding': soup.original_encoding,
        'text': declared_text,
        'text_preview': _preview(declared_text),
        'text_length': len(declared_text),
        'replacement_character_count': declared_text.count('\ufffd'),
        'utf8_text': intended_text,
        'utf8_text_preview': _preview(intended_text),
        'first_character_difference': _first_character_difference(declared_text, intended_text),
    }


class _DeencapTrace:
    """
    Temporarily wraps ``MessageBase.deencapsulateBody`` to record calls.

    The wrapped method is the original; return values and exceptions are
    preserved exactly.
    """

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.phase = 'init'
        self._original = None

    def __enter__(self):
        from ..msg_classes.message_base import MessageBase
        self._original = MessageBase.deencapsulateBody

        def wrapper(instance, rtfBody, bodyType):
            call: Dict[str, Any] = {
                'phase': self.phase,
                'body_type': DeencapType(bodyType).name,
                'input_size': len(rtfBody) if rtfBody else 0,
            }
            try:
                result = self._original(instance, rtfBody, bodyType)
                call['result_type'] = type(result).__name__
                call['result_size'] = len(result) if result is not None else 0
                call['result_present'] = bool(result)
                return result
            except BaseException as error:
                call['error'] = f'{type(error).__name__}: {error}'
                raise
            finally:
                self.calls.append(call)

        MessageBase.deencapsulateBody = wrapper
        return self

    def __exit__(self, *args):
        from ..msg_classes.message_base import MessageBase
        if self._original is not None:
            MessageBase.deencapsulateBody = self._original

    def find(self, body_type: str, phase: Optional[str] = None):
        return [
            call for call in self.calls
            if call['body_type'] == body_type
            and (phase is None or call['phase'] == phase)
        ]


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: List[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


class _ParserLogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: List[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _plain_fallback_html(msg, body: Optional[str]) -> Dict[str, Optional[bytes]]:
    if not body:
        return {'regular': None, 'signed': None}
    corrected = html_module.escape(body).replace('\r', '').replace('\n', '<br />')
    return {
        'regular': f'<html><body>{corrected}</body></html>'.encode('ascii', 'xmlcharrefreplace'),
        'signed': f'<html><body>{corrected}</body></head>'.encode('utf-8'),
    }


def _body_source(msg, signed: bool, signed_body: Optional[str], trace: _DeencapTrace) -> str:
    if msg.getStringStream(_PLAIN) is not None:
        return 'top-level-plain'
    if signed and signed_body:
        return 'signed-mime-plain'
    if trace.find('PLAIN'):
        return 'rtf'
    return 'none'


def _html_source(
    raw: Optional[bytes],
    html: Optional[bytes],
    signed: bool,
    signed_html: Optional[bytes],
    rtf_html: Optional[bytes],
    plain_variants: Dict[str, Optional[bytes]],
) -> str:
    if html is None:
        return 'none'
    if html == b'':
        return 'empty-native' if raw == b'' else 'empty'
    if signed and signed_html and html == signed_html:
        return 'signed-mime-html'
    if raw is not None and raw != b'' and html == raw:
        return 'native-html'
    if rtf_html and html == rtf_html:
        return 'rtf-html'
    if html == plain_variants['signed' if signed else 'regular']:
        return 'plain-text'
    return 'other'


def _rtf_details(msg) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        'stream_exists': msg.exists(_RTF),
        'stream_size': len(msg.rtfBody) if msg.rtfBody else 0,
        'deencapsulated': False,
        'content_type': None,
        'html_size': 0,
        'text_size': 0,
        'error': None,
    }
    try:
        deencapsulated = msg.deencapsulatedRtf
    except BaseException as error:
        details['error'] = f'{type(error).__name__}: {error}'
        return details
    if deencapsulated is not None:
        details['deencapsulated'] = True
        details['content_type'] = deencapsulated.content_type
        details['html_size'] = len(deencapsulated.html or b'') if deencapsulated.content_type == 'html' else 0
        details['text_size'] = len(deencapsulated.text or '') if deencapsulated.content_type == 'text' else 0
    return details


def _rtf_html_value(msg) -> Optional[bytes]:
    try:
        deencapsulated = msg.deencapsulatedRtf
    except BaseException:
        return None
    if deencapsulated is not None and deencapsulated.content_type == 'html':
        return deencapsulated.html
    return None


def diagnose_fixture(path) -> Dict[str, Any]:
    path = pathlib.Path(path)
    log_capture = _LogCapture()
    logger = logging.getLogger(_TRACE_LOGGER)
    logger.addHandler(log_capture)
    old_level = logger.level
    logger.setLevel(logging.INFO)

    parser_capture = _ParserLogCapture()
    parser_logger = logging.getLogger('bs4.dammit')
    parser_logger.addHandler(parser_capture)
    old_parser_level = parser_logger.level
    parser_logger.setLevel(logging.WARNING)

    trace = _DeencapTrace()
    report: Dict[str, Any] = {
        'fixture': str(path),
        'name': path.name,
        'error': None,
    }

    try:
        with trace:
            with openMsg(path, delayAttachments=True, strict=False) as msg:
                signed = isinstance(msg, MessageSignedBase)
                report['message_type'] = 'signed' if signed else 'regular'
                report['instance_class'] = type(msg).__name__
                report['class_type'] = msg.classType
                report['detected_bodies'] = str(msg.detectedBodies)

                raw_exists = msg.exists(_NATIVE_HTML)
                raw = msg.getStream(_NATIVE_HTML)
                native_state = (
                    'missing' if raw is None
                    else 'empty' if raw == b''
                    else 'bytes'
                )
                report['native_html'] = {
                    'stream': _NATIVE_HTML,
                    'exists': raw_exists,
                    'state': native_state,
                    'size': None if raw is None else len(raw),
                    'sha256': _sha256(raw),
                    'hex_sample': _hex_sample(raw),
                }

                report['rtf'] = _rtf_details(msg)
                body = msg.body
                signed_body = msg.signedBody if signed else None
                report['body'] = {
                    'source': _body_source(msg, signed, signed_body, trace),
                    'length': len(body or ''),
                    'preview': _preview(body),
                    'sha256': _sha256(body.encode('utf-8')) if body is not None else None,
                }

                signed_html = msg.signedHtmlBody if signed else None
                report['signed_mime'] = (
                    {
                        'html_bytes': None if signed_html is None else len(signed_html),
                        'html_sha256': _sha256(signed_html),
                        'plain_preview': _preview(signed_body),
                    }
                    if signed else None
                )

                trace.phase = 'html'
                html_error = None
                try:
                    html_body = msg.htmlBody
                except BaseException as error:
                    html_body = None
                    html_error = f'{type(error).__name__}: {error}'
                report['html_body'] = {
                    'present': html_body is not None,
                    'length': 0 if html_body is None else len(html_body),
                    'sha256': _sha256(html_body),
                    'preview': _preview(html_body),
                    'error': html_error,
                }

                prepared_error = None
                try:
                    prepared = msg.htmlBodyPrepared
                except BaseException as error:
                    prepared = None
                    prepared_error = f'{type(error).__name__}: {error}'
                report['html_body_prepared'] = {
                    'present': prepared is not None,
                    'length': 0 if prepared is None else len(prepared),
                    'sha256': _sha256(prepared),
                    'error': prepared_error,
                }

                save_error = None
                try:
                    saved = msg.getSaveHtmlBody(preparedHtml=True)
                except BaseException as error:
                    saved = None
                    save_error = f'{type(error).__name__}: {error}'
                report['save_html_prepared'] = {
                    'present': saved is not None,
                    'length': 0 if saved is None else len(saved),
                    'sha256': _sha256(saved),
                    'error': save_error,
                }
                report['html_encoding_after_prepare'] = getattr(msg, '_htmlEncoding', None)

                report['rtf_deencapsulation_calls'] = trace.calls
                report['log_markers'] = log_capture.records
                report['decoding'] = (
                    _parse_native_html(raw, parser_capture.records)
                    if raw not in (None, b'') else None
                )
                prepared_decode = _parse_prepared_html(prepared)
                prepared_text = prepared_decode['text'] if prepared_decode else None
                report['saved_html'] = _parse_saved_html(
                    saved, prepared_text,
                    report['html_encoding_after_prepare'],
                )
                if (report['decoding'] and prepared_decode and raw not in (None, b'')
                        and prepared_decode['text'] != report['decoding']['text']):
                    prepared_decode['first_difference_from_obtained_bytes'] = (
                        _first_character_difference(
                            report['decoding']['text'], prepared_decode['text'],
                        )
                    )
                report['prepared_html_decode'] = prepared_decode

                html_trace = trace.find('HTML', 'html')
                rtf_attempted = bool(html_trace)
                if html_trace:
                    last = html_trace[-1]
                    if last.get('error'):
                        rtf_result = 'error'
                    elif last.get('result_present'):
                        rtf_result = 'html'
                    else:
                        rtf_result = 'no-html'
                else:
                    rtf_result = 'not-attempted'

                rtf_html = _rtf_html_value(msg)
                plain_variants = _plain_fallback_html(msg, body)
                html_source = _html_source(
                    raw, html_body, signed, signed_html, rtf_html, plain_variants,
                )
                plain_used = html_source == 'plain-text'
                plain_attempted = plain_used or any(
                    'plain text body' in marker for marker in log_capture.records
                )

                report['path'] = {
                    'native_html': native_state,
                    'rtf_stream': report['rtf']['stream_exists'],
                    'rtf_html_attempted': rtf_attempted,
                    'rtf_html_result': rtf_result,
                    'plain_text_available': body is not None,
                    'plain_fallback_attempted': plain_attempted,
                    'plain_fallback_used': plain_used,
                    'html_source': html_source,
                    'empty_native_short_circuited': (
                        native_state == 'empty' and not rtf_attempted
                        and not plain_used
                    ),
                }

        report['triage_codes'] = _triage_codes(report)
        report['first_anomaly'] = _first_anomaly(report)
        report['anomalies'] = _anomalies(report)
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        report['triage_codes'] = ['DIAGNOSTIC_FAILED']
        report['first_anomaly'] = {
            'stage': 'open',
            'kind': 'diagnostic_failed',
            'detail': report['error'],
        }
        report['anomalies'] = [report['first_anomaly']]
        report.setdefault('log_markers', log_capture.records)
        report.setdefault('rtf_deencapsulation_calls', trace.calls)
    finally:
        logger.removeHandler(log_capture)
        logger.setLevel(old_level)
        parser_logger.removeHandler(parser_capture)
        parser_logger.setLevel(old_parser_level)

    report['extract_msg_version'] = __version__
    return report


def _triage_codes(report: Dict[str, Any]) -> List[str]:
    if report.get('error'):
        return ['DIAGNOSTIC_FAILED']

    codes: List[str] = []
    native = report['native_html']
    rtf = report['rtf']
    path = report['path']
    decoding = report.get('decoding')

    if native['state'] == 'missing':
        codes.append('NATIVE_HTML_MISSING')
    elif native['state'] == 'empty':
        codes.append('NATIVE_HTML_EMPTY')

    if native['state'] == 'bytes' and decoding:
        for issue in decoding['issues']:
            codes.append(issue['kind'].upper())

    native_short_circuited = path['html_source'] in ('native-html', 'empty-native')
    if not rtf['stream_exists'] and not native_short_circuited:
        codes.append('RTF_MISSING')
    elif path['rtf_html_attempted'] and path['rtf_html_result'] == 'no-html':
        codes.append('RTF_HTML_UNAVAILABLE')

    if path['html_source'] == 'rtf-html':
        codes.append('RTF_HTML_USED')
    elif path['html_source'] == 'signed-mime-html':
        codes.append('SIGNED_MIME_HTML_USED')
    if path['plain_fallback_used']:
        codes.append('PLAIN_TEXT_FALLBACK')
    if path['plain_text_available'] and not path['plain_fallback_used'] and path['html_source'] in ('none', 'empty'):
        codes.append('PLAIN_TEXT_NOT_USED')
    if report['html_body']['present'] is False or report['html_body']['length'] == 0:
        codes.append('NO_HTML_OUTPUT')
    if decoding and decoding.get('strict_utf8', {}).get('ok') is False:
        codes.append('HTML_STRICT_DECODE_FAILED')
    if (report.get('prepared_html_decode') or {}).get('first_difference_from_obtained_bytes'):
        codes.append('PREPARE_CHANGED_TEXT')
    if report.get('saved_html', {}) and report['saved_html'].get('prepared_body_preserved') is False:
        codes.append('EXPORT_REENCODED_TEXT')
    if report.get('save_html_prepared', {}).get('error'):
        codes.append('HTML_SAVE_FAILED')

    if not codes:
        codes.append('OK')
    return codes


def _anomalies(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    if report.get('error'):
        return [{
            'stage': 'open',
            'kind': 'diagnostic_failed',
            'detail': report['error'],
        }]

    anomalies: List[Dict[str, Any]] = []
    native = report['native_html']
    if native['state'] == 'missing':
        anomalies.append({
            'stage': 'stream_lookup',
            'kind': 'missing_stream',
            'detail': f'{_NATIVE_HTML} does not exist.',
        })
    elif native['state'] == 'empty':
        anomalies.append({
            'stage': 'stream_lookup',
            'kind': 'empty_stream',
            'detail': f'{_NATIVE_HTML} exists but is zero bytes.',
        })

    decoding = report.get('decoding')
    if decoding:
        for issue in decoding['issues']:
            anomalies.append({
                'stage': 'html_decode',
                'kind': issue['kind'],
                'detail': issue['detail'],
            })

    rtf = report['rtf']
    body_path = report['path']
    native_short_circuited = body_path['html_source'] in ('native-html', 'empty-native')
    if body_path['rtf_html_attempted'] and not rtf['html_size']:
        anomalies.append({
            'stage': 'rtf_deencapsulation',
            'kind': 'no_html',
            'detail': (
                'RTF stream was present but did not yield encapsulated HTML'
                + (f' ({rtf["error"]})' if rtf['error'] else '.')
            ),
        })
    elif not rtf['stream_exists'] and not native_short_circuited:
        anomalies.append({
            'stage': 'rtf_lookup',
            'kind': 'missing_stream',
            'detail': f'{_RTF} does not exist.',
        })

    if (body_path['plain_text_available'] and not body_path['plain_fallback_used']
            and body_path['html_source'] in ('none', 'empty')):
        anomalies.append({
            'stage': 'plain_fallback',
            'kind': 'not_used',
            'detail': (
                'A plain-text body is available, but the HTML path did not '
                'fall back to it.'
            ),
        })

    if report['html_body']['error']:
        anomalies.append({
            'stage': 'html_body',
            'kind': 'read_failed',
            'detail': report['html_body']['error'],
        })
    if report['html_body_prepared']['error']:
        anomalies.append({
            'stage': 'html_prepare',
            'kind': 'prepare_failed',
            'detail': report['html_body_prepared']['error'],
        })
    if report['save_html_prepared']['error']:
        anomalies.append({
            'stage': 'html_save',
            'kind': 'save_failed',
            'detail': report['save_html_prepared']['error'],
        })
    prepared_decode = report.get('prepared_html_decode')
    if prepared_decode and prepared_decode.get('first_difference_from_obtained_bytes'):
        anomalies.append({
            'stage': 'html_prepare',
            'kind': 'prepared_text_changed',
            'detail': (
                'The prepared body text differs from the text parsed from the '
                'obtained native HTML bytes; the prepare step changed the '
                'visible text.'
            ),
            'first_character_difference': (
                prepared_decode['first_difference_from_obtained_bytes']
            ),
        })
    saved_html = report.get('saved_html')
    if saved_html and saved_html.get('prepared_body_preserved') is False:
        anomalies.append({
            'stage': 'html_export',
            'kind': 'prepared_text_not_preserved',
            'detail': (
                'The body text in the saved HTML differs from the prepared HTML '
                'body; the export re-parses already-encoded bytes and changes '
                'the visible text again.'
            ),
            'first_character_difference': (
                saved_html.get('first_difference_from_prepared')
            ),
        })
    if report['html_body']['length'] == 0:
        anomalies.append({
            'stage': 'html_output',
            'kind': 'no_html_output',
            'detail': 'The HTML body value is empty and the export writes zero bytes.',
        })
    return anomalies


def _first_anomaly(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    anomalies = _anomalies(report)
    return anomalies[0] if anomalies else None


def diagnose_directory(directory) -> Dict[str, Any]:
    directory = pathlib.Path(directory)
    if directory.is_file():
        report = diagnose_fixture(directory)
        report['sample_id'] = directory.name
        report['name'] = directory.name
        return {
            'diagnostic_version': 1,
            'extract_msg_version': __version__,
            'directory': str(directory.parent),
            'reports': [report],
            'comparisons': [],
        }
    reports = []
    for path in sorted(directory.rglob('*.msg')):
        report = diagnose_fixture(path)
        report['sample_id'] = path.relative_to(directory).as_posix()
        report['name'] = report['sample_id']
        reports.append(report)
    comparisons = compare_reports(reports)
    return {
        'diagnostic_version': 1,
        'extract_msg_version': __version__,
        'directory': str(directory),
        'reports': reports,
        'comparisons': comparisons,
    }


_COMPARISON_STAGES = (
    'native_html',
    'rtf_html_attempted',
    'rtf_html_result',
    'plain_fallback_attempted',
    'plain_fallback_used',
    'html_source',
    'html_length',
    'html_sha256',
    'save_length',
)


def _comparison_value(report: Dict[str, Any], stage: str):
    if stage == 'native_html':
        return report['native_html']['state']
    if stage == 'save_length':
        return report['save_html_prepared']['length']
    if stage in ('html_length', 'html_sha256'):
        return report['html_body'][stage.split('_', 1)[1]]
    return report['path'][stage]


def _scenario_key(name: str) -> str:
    return name.split('/', 1)[1] if '/' in name else name


def compare_reports(reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pair regular and signed fixtures that describe the same scenario.

    Reports the actual output and the first stage where the two paths differ,
    which is the evidence needed to prove a branch fork rather than infer one
    from a single final HTML value.
    """
    by_key: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for report in reports:
        if report.get('error'):
            continue
        key = _scenario_key(report['name'])
        by_key.setdefault(key, {})[report['message_type']] = report

    comparisons = []
    for key, pair in sorted(by_key.items()):
        regular = pair.get('regular')
        signed = pair.get('signed')
        if not regular or not signed:
            continue

        first_divergence = None
        for stage in _COMPARISON_STAGES:
            left = _comparison_value(regular, stage)
            right = _comparison_value(signed, stage)
            if left != right:
                first_divergence = {
                    'stage': stage,
                    'regular': left,
                    'signed': right,
                }
                break

        comparisons.append({
            'scenario': key,
            'regular_fixture': regular['name'],
            'signed_fixture': signed['name'],
            'regular_html_source': regular['path']['html_source'],
            'signed_html_source': signed['path']['html_source'],
            'regular_html_length': regular['html_body']['length'],
            'signed_html_length': signed['html_body']['length'],
            'regular_html_sha256': regular['html_body']['sha256'],
            'signed_html_sha256': signed['html_body']['sha256'],
            'regular_save_length': regular['save_html_prepared']['length'],
            'signed_save_length': signed['save_html_prepared']['length'],
            'regular_triage': regular['triage_codes'],
            'signed_triage': signed['triage_codes'],
            'diverges': first_divergence is not None,
            'first_divergence': first_divergence,
        })
    return comparisons


def _cell(value: Any, limit: int = 70) -> str:
    text = '' if value is None else str(value)
    text = text.replace('|', '\\|').replace('\n', ' ')
    return text if len(text) <= limit else text[:limit] + '...'


def format_markdown(diagnosis: Dict[str, Any]) -> str:
    lines: List[str] = [
        '# Body-path diagnostic',
        '',
        f'- extract_msg version: `{diagnosis["extract_msg_version"]}`',
        f'- sample directory: `{diagnosis["directory"]}`',
        '',
        '## Per-fixture results',
        '',
        '| Fixture | Native HTML | RTF | HTML source | HTML bytes | Save bytes | Triage | First anomaly |',
        '| --- | --- | --- | --- | ---: | ---: | --- | --- |',
    ]
    for report in diagnosis['reports']:
        anomaly = report.get('first_anomaly') or {}
        lines.append('| ' + ' | '.join((
            _cell(report['name']),
            _cell(report['native_html']['state'] if 'native_html' in report else 'error'),
            _cell(
                'missing' if 'rtf' not in report else (
                    f'{report["rtf"]["stream_size"]}B/{report["rtf"]["content_type"] or "no-html"}'
                )
            ),
            _cell(report.get('path', {}).get('html_source', 'error')),
            _cell(report.get('html_body', {}).get('length', 0)),
            _cell(report.get('save_html_prepared', {}).get('length', 0)),
            _cell(', '.join(report.get('triage_codes', []))),
            _cell(
                f'{anomaly.get("stage")}:{anomaly.get("kind")}'
                if anomaly else 'none', 80,
            ),
        )) + ' |')

    lines.extend((
        '',
        '## Regular vs signed forks',
        '',
        '| Scenario | Regular | Signed | First divergence | Evidence |',
        '| --- | --- | --- | --- | --- |',
    ))
    for comparison in diagnosis['comparisons']:
        divergence = comparison['first_divergence']
        if divergence:
            evidence = (
                f'`{divergence["stage"]}`: regular={divergence["regular"]!r}, '
                f'signed={divergence["signed"]!r}'
            )
            divergence_text = divergence['stage']
        else:
            evidence = 'same branch and output'
            divergence_text = 'none'
        lines.append('| ' + ' | '.join((
            _cell(comparison['scenario']),
            _cell(
                f'{comparison["regular_html_source"]} {comparison["regular_html_length"]}B'
            ),
            _cell(
                f'{comparison["signed_html_source"]} {comparison["signed_html_length"]}B'
            ),
            _cell(divergence_text),
            _cell(evidence, 140),
        )) + ' |')

    lines.extend((
        '',
        '## Reading the result',
        '',
        '- `native_html` is read directly from `__substg1.0_10130102`; `missing` and `empty` are different causes even when both end at the same generated HTML.',
        '- `rtf_html_attempted`/`rtf_html_result` show whether the RTF branch ran, and whether it produced HTML, produced nothing, or raised.',
        '- `decoding` compares the exact bytes obtained with the parser text, chosen encoding, strict UTF-8 result, and replacement characters.',
        '- A non-empty `PLAIN_TEXT_FALLBACK` is a recovered body, not evidence that the source had no body.',
        '',
    ))
    return '\n'.join(lines)
