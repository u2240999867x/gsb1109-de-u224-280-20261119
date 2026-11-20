from __future__ import annotations


__all__ = [
    'FIXTURES',
    'FixtureSpec',
    'generate_fixtures',
    'manifest_for',
]


import hashlib
import json
import pathlib
import struct

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import compressed_rtf

from ..ole_writer import OleWriter


_MESSAGE_PROPS = ((0x340D0003, 6, 0x40000),)
_ATTACHMENT_PROPS = ((0x37050003, 3, 1),)

_GUID_STREAM = bytes.fromhex(
    '0820060000000000c000000000000046'
    '8603020000000000c000000000000046'
)
_ENTRY_STREAM = bytes.fromhex(
    '80850000060000008185000006000100'
    'd7850000060002000000000009000300'
)
_NAMES_STREAM = bytes.fromhex(
    '1800000063006f006e00740065006e0074002d007400790070006500'
)

_PLAIN_RTF = (
    br'{\rtf1\ansi\deff0{\fonttbl{\f0\fnil Arial;}}'
    br'\f0\fs20 RTF fallback text.\par}'
)
_HTML_RTF = (
    br'{\rtf1\ansi\ansicpg1252\fromhtml1 \fbidis '
    br'\deff0{\fonttbl{\f0\fswiss Arial;}}\f0\fs24 '
    br'{\*\htmltag64 <html><body><p>RTF-ORIGIN</p></body></html>}}'
)

_MOJIBAKE_HTML = (
    '<html><head><meta charset="windows-1252"></head>'
    '<body><p>中文正文</p></body></html>'
).encode('utf-8')
_REPLACEMENT_HTML = (
    b'<html><head><meta charset="utf-8"></head>'
    b'<body><p>caf\xe9 \x81</p></body></html>'
)
_OK_HTML = b'<html><body><p>Native HTML body</p></body></html>'


@dataclass(frozen=True)
class FixtureSpec:
    name: str
    message_type: str
    scenario: str
    native_html: Optional[bytes] = None
    native_html_state: str = 'missing'
    rtf: Optional[bytes] = _PLAIN_RTF
    rtf_state: str = 'no-html'
    plain: Optional[str] = 'Plain stream text.\r\nLine two.'
    plain_source: str = 'top-level'
    mime_html: Optional[bytes] = None
    mime_html_state: str = 'absent'

    def manifest(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'message_type': self.message_type,
            'scenario': self.scenario,
            'native_html': self.native_html_state,
            'rtf': self.rtf_state,
            'plain': self.plain_source,
            'mime_html': self.mime_html_state,
        }


def _message_props(items) -> bytes:
    data = bytearray(32 + 16 * len(items))
    for index, (name, type_, value) in enumerate(items):
        struct.pack_into('<IIQ', data, 32 + index * 16, name, type_, value)
    return bytes(data)


def _attachment_props(items) -> bytes:
    data = bytearray(8 + 16 * len(items))
    for index, (name, type_, value) in enumerate(items):
        struct.pack_into('<IIQ', data, 8 + index * 16, name, type_, value)
    return bytes(data)


def _utf16(value: str) -> bytes:
    return value.encode('utf-16-le')


def _add_common(writer: OleWriter, class_type: str, subject: str) -> None:
    writer.addEntry('__substg1.0_001A001F', _utf16(class_type))
    writer.addEntry('__substg1.0_0037001F', _utf16(subject))
    writer.addEntry('__substg1.0_0C1A001F', _utf16('Archive Diagnostics'))
    writer.addEntry('__substg1.0_5D01001F', _utf16('diagnostics@example.invalid'))
    writer.addEntry('__nameid_version1.0/__substg1.0_00020102', _GUID_STREAM)
    writer.addEntry('__nameid_version1.0/__substg1.0_00030102', _ENTRY_STREAM)
    writer.addEntry('__nameid_version1.0/__substg1.0_00040102', _NAMES_STREAM)


def _add_rtf(writer: OleWriter, spec: FixtureSpec) -> None:
    if spec.rtf is not None:
        writer.addEntry('__substg1.0_10090102', compressed_rtf.compress(spec.rtf))


def _signed_mime(spec: FixtureSpec) -> bytes:
    signature_boundary = 'diag-signature'
    body_boundary = 'diag-body'
    body_parts = [
        'Content-Type: text/plain; charset=utf-8\r\n'
        'Content-Transfer-Encoding: 8bit\r\n\r\n'
        f'{spec.plain or ""}\r\n'
    ]
    if spec.mime_html is not None:
        body_parts.append(
            'Content-Type: text/html; charset=utf-8\r\n'
            'Content-Transfer-Encoding: 8bit\r\n\r\n'
            + spec.mime_html.decode('utf-8', 'surrogateescape') + '\r\n'
        )
    body = ''.join(f'--{body_boundary}\r\n{part}' for part in body_parts)
    body += f'--{body_boundary}--\r\n'

    mime = (
        'Content-Type: multipart/signed;\r\n'
        '\tprotocol="application/x-pkcs7-signature";\r\n'
        '\tmicalg=sha1;\r\n'
        f'\tboundary="{signature_boundary}"\r\n\r\n'
        f'--{signature_boundary}\r\n'
        'Content-Type: multipart/mixed;\r\n'
        f'\tboundary="{body_boundary}"\r\n\r\n'
        f'{body}'
        f'--{signature_boundary}\r\n'
        'Content-Type: application/x-pkcs7-signature; name="smime.p7m"\r\n'
        'Content-Transfer-Encoding: base64\r\n\r\n'
        'YWJj\r\n'
        f'--{signature_boundary}--\r\n'
    )
    return mime.encode('utf-8')


def _write_regular(path: pathlib.Path, spec: FixtureSpec) -> None:
    writer = OleWriter()
    _add_common(writer, 'IPM.Note', spec.scenario)
    if spec.plain is not None:
        writer.addEntry('__substg1.0_1000001F', _utf16(spec.plain))
    if spec.native_html is not None:
        writer.addEntry('__substg1.0_10130102', spec.native_html)
    _add_rtf(writer, spec)
    writer.addEntry('__properties_version1.0', _message_props(_MESSAGE_PROPS))
    writer.write(path)


def _write_signed(path: pathlib.Path, spec: FixtureSpec) -> None:
    writer = OleWriter()
    _add_common(writer, 'IPM.Note.SMIME.MultipartSigned', spec.scenario)
    if spec.native_html is not None:
        writer.addEntry('__substg1.0_10130102', spec.native_html)
    _add_rtf(writer, spec)

    attachment = '__attach_version1.0_#00000000'
    writer.addEntry(attachment + '/__substg1.0_37010102', _signed_mime(spec))
    writer.addEntry(attachment + '/__substg1.0_3704001F', _utf16('smime.p7m'))
    writer.addEntry(attachment + '/__substg1.0_3707001F', _utf16('smime.p7m'))
    writer.addEntry(attachment + '/__substg1.0_370E001F', _utf16('multipart/signed'))
    writer.addEntry(
        attachment + '/__properties_version1.0',
        _attachment_props(_ATTACHMENT_PROPS),
    )
    writer.addEntry('__properties_version1.0', _message_props(_MESSAGE_PROPS))
    writer.write(path)


def _specs() -> Tuple[FixtureSpec, ...]:
    common = {
        'json': _PLAIN_RTF,
    }
    specs: List[FixtureSpec] = []

    for message_type in ('regular', 'signed'):
        plain_source = 'top-level' if message_type == 'regular' else 'signed-mime'
        plain = 'Plain stream text.\r\nLine two.' if message_type == 'regular' else 'Signed MIME plain text.\r\nLine two.'
        specs.extend((
            FixtureSpec(
                f'{message_type}/missing-native__rtf-no-html.msg',
                message_type,
                'missing native HTML, plain RTF, body still available',
                native_html=None,
                native_html_state='missing',
                plain=plain,
                plain_source=plain_source,
            ),
            FixtureSpec(
                f'{message_type}/empty-native__rtf-no-html.msg',
                message_type,
                'zero-byte native HTML, plain RTF, body still available',
                native_html=b'',
                native_html_state='empty',
                plain=plain,
                plain_source=plain_source,
            ),
            FixtureSpec(
                f'{message_type}/missing-native__no-rtf.msg',
                message_type,
                'missing native HTML and no RTF, plain body available',
                native_html=None,
                native_html_state='missing',
                rtf=None,
                rtf_state='absent',
                plain=plain,
                plain_source=plain_source,
            ),
            FixtureSpec(
                f'{message_type}/missing-native__rtf-html.msg',
                message_type,
                'missing native HTML, RTF contains encapsulated HTML',
                native_html=None,
                native_html_state='missing',
                rtf=_HTML_RTF,
                rtf_state='html',
                plain=plain,
                plain_source=plain_source,
            ),
            FixtureSpec(
                f'{message_type}/native-html-mojibake.msg',
                message_type,
                'native HTML bytes declared windows-1252 but encoded UTF-8',
                native_html=_MOJIBAKE_HTML,
                native_html_state='bytes',
                plain=plain,
                plain_source=plain_source,
            ),
            FixtureSpec(
                f'{message_type}/native-html-replacement.msg',
                message_type,
                'native HTML declares UTF-8 but contains invalid bytes',
                native_html=_REPLACEMENT_HTML,
                native_html_state='bytes',
                plain=plain,
                plain_source=plain_source,
            ),
            FixtureSpec(
                f'{message_type}/native-html-ok.msg',
                message_type,
                'native HTML present and valid',
                native_html=_OK_HTML,
                native_html_state='bytes',
                plain=plain,
                plain_source=plain_source,
            ),
        ))

    specs.append(FixtureSpec(
        'signed/missing-native__rtf-no-html__mime-html.msg',
        'signed',
        'signed MIME supplies HTML while RTF has no HTML',
        native_html=None,
        native_html_state='missing',
        plain='Signed MIME plain text.\r\nLine two.',
        plain_source='signed-mime',
        mime_html=b'<html><body><p>MIME-ORIGIN</p></body></html>',
        mime_html_state='bytes',
    ))

    return tuple(specs)


FIXTURES = _specs()


def generate_fixtures(directory) -> Dict[str, Any]:
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {
        'description': 'Controlled body-path fixtures for extract_msg body diagnostics.',
        'fixtures': [],
    }

    for spec in FIXTURES:
        path = directory / spec.name
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.message_type == 'regular':
            _write_regular(path, spec)
        else:
            _write_signed(path, spec)
        data = path.read_bytes()
        entry = spec.manifest()
        entry.update({
            'path': spec.name,
            'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
        })
        manifest['fixtures'].append(entry)

    (directory / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    return manifest


def manifest_for(directory) -> Dict[str, Any]:
    path = pathlib.Path(directory) / 'manifest.json'
    return json.loads(path.read_text(encoding='utf-8'))
