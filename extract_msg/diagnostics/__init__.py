from __future__ import annotations


"""
Local body-path diagnostics for archived Outlook MSG files.

This package is read-only with respect to the archive.  It uses the normal
``openMsg`` entry point, records which body source was used, and reports the
first stage where a normal message and a signed message take different paths.
"""


__all__ = [
    'FIXTURES',
    'FixtureSpec',
    'compare_reports',
    'diagnose_directory',
    'diagnose_fixture',
    'format_markdown',
    'generate_fixtures',
    'main',
    'manifest_for',
]


from .body_path import (
    compare_reports, diagnose_directory, diagnose_fixture, format_markdown
)
from .fixtures import FIXTURES, FixtureSpec, generate_fixtures, manifest_for


def main(argv=None) -> int:
    """
    Command line entry point for the diagnostic.

    ``python -m extract_msg.diagnostics --generate-fixtures DIR``
        Write the controlled samples and ``manifest.json`` to ``DIR``.

    ``python -m extract_msg.diagnostics --diagnose DIR --markdown report.md``
        Diagnose every top-level ``*.msg`` in ``DIR`` and optionally write the
        human-readable report.
    """
    import argparse
    import json
    import pathlib

    parser = argparse.ArgumentParser(
        prog = 'python -m extract_msg.diagnostics',
        description = 'Read-only diagnosis of MSG body selection paths.',
    )
    parser.add_argument(
        '--generate-fixtures', metavar = 'DIR',
        help = 'Generate controlled regular/signed MSG samples and a manifest.',
    )
    parser.add_argument(
        '--diagnose', metavar = 'DIR',
        help = 'Diagnose all top-level *.msg files in DIR.',
    )
    parser.add_argument(
        '--json', metavar = 'PATH',
        help = 'Write the machine-readable diagnosis JSON to PATH.',
    )
    parser.add_argument(
        '--markdown', metavar = 'PATH',
        help = 'Write the Markdown report to PATH.',
    )
    args = parser.parse_args(argv)

    if not args.generate_fixtures and not args.diagnose:
        parser.error('specify --generate-fixtures DIR and/or --diagnose DIR')

    if args.generate_fixtures:
        manifest = generate_fixtures(args.generate_fixtures)
        print(f'Wrote {len(manifest["fixtures"])} fixtures and manifest.json to {args.generate_fixtures}')

    if args.diagnose:
        diagnosis = diagnose_directory(args.diagnose)
        if args.json:
            pathlib.Path(args.json).write_text(
                json.dumps(diagnosis, indent = 2, sort_keys = True, default = str) + '\n',
                encoding = 'utf-8',
            )
        if args.markdown:
            pathlib.Path(args.markdown).write_text(
                format_markdown(diagnosis), encoding = 'utf-8',
            )
        for report in diagnosis['reports']:
            if report.get('error'):
                print(f'{report["name"]}: ERROR {report["error"]}')
                continue
            print(
                f'{report["name"]}: native={report["native_html"]["state"]} '
                f'source={report["path"]["html_source"]} '
                f'html={report["html_body"]["length"]}B '
                f'triage={",".join(report["triage_codes"])}'
            )
        for comparison in diagnosis['comparisons']:
            divergence = comparison['first_divergence']
            if divergence:
                print(
                    f'{comparison["scenario"]}: first divergence at '
                    f'{divergence["stage"]} '
                    f'(regular={divergence["regular"]!r}, signed={divergence["signed"]!r})'
                )
        if not args.json and not args.markdown:
            print()
            print(format_markdown(diagnosis))

    return 0
