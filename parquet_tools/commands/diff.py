"""Read-only CLI for comparing two Parquet footer schemas."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from typing import Dict, List, Optional, Tuple

from parquet_tools.schema_diff.engine import Policy, compare_schemas
from parquet_tools.schema_diff.footer import FooterDocument, try_load_footer

EXIT_COMPATIBLE = 0
EXIT_INCOMPATIBLE = 1
EXIT_UNVERIFIABLE = 2
EXIT_READ_ERROR = 3
EXIT_MIGRATION = 4


def _parse_rename_ids(values: Optional[List[str]]) -> List[int]:
    result: List[int] = []
    for value in values or []:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                field_id = int(token)
            except ValueError as error:
                raise ValueError(
                    f"invalid field ID {token!r}; expected an integer"
                ) from error
            if field_id < 0:
                raise ValueError(
                    f"invalid field ID {field_id}; field IDs must be non-negative"
                )
            result.append(field_id)
    return sorted(set(result))


def configure_parser(parser: ArgumentParser) -> ArgumentParser:
    parser.add_argument("old", metavar="OLD", help="old Parquet file footer")
    parser.add_argument("new", metavar="NEW", help="new Parquet file footer")
    parser.add_argument(
        "--allow-display-order",
        dest="allow_display_order",
        action="store_true",
        default=True,
        help="treat leaf column order changes as display-only (default)",
    )
    parser.add_argument(
        "--no-allow-display-order",
        dest="allow_display_order",
        action="store_false",
        help="treat leaf column order changes as incompatible",
    )
    parser.add_argument(
        "--allow-rename-id",
        dest="allow_rename_ids",
        action="append",
        metavar="ID[,ID...]",
        default=[],
        help="allow a rename only when both sides carry this stable field ID; repeatable",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="output format (default: json)",
    )
    parser.add_argument(
        "--json-indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2; use 0 for one line)",
    )
    parser.set_defaults(handler=_cli)
    return parser


def build_policy(args: Namespace) -> Policy:
    return Policy(
        allow_display_order=bool(args.allow_display_order),
        allowed_rename_ids=tuple(_parse_rename_ids(args.allow_rename_ids)),
    )


def _cli(args: Namespace) -> int:
    try:
        policy = build_policy(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_READ_ERROR

    old_doc, old_error = _load(args.old)
    new_doc, new_error = _load(args.new)
    if old_error or new_error:
        errors = [error for error in (old_error, new_error) if error]
        payload = {
            "tool": "parquet-footer-diff",
            "report_version": 1,
            "read_only": True,
            "evidence_scope": "footer_only",
            "status": "read_error",
            "errors": errors,
            "policy": policy.as_dict(),
        }
        _print_payload(payload, args)
        return EXIT_READ_ERROR

    assert old_doc is not None and new_doc is not None
    report = compare_schemas(old_doc.tree, new_doc.tree, policy)
    report = {
        "tool": report["tool"],
        "report_version": report["report_version"],
        "read_only": report["read_only"],
        "evidence_scope": report["evidence_scope"],
        "data_pages_scanned": report["data_pages_scanned"],
        "inputs": {
            "old": old_doc.summary(),
            "new": new_doc.summary(),
        },
        "policy": report["policy"],
        "summary": report["summary"],
        "changes": report["changes"],
        "identity_pairs": report["identity_pairs"],
        "pending_confirmations": report["pending_confirmations"],
        "value_verification": report["value_verification"],
    }
    _print_payload(report, args)
    return _exit_code(report)


def _load(path: str) -> Tuple[Optional[FooterDocument], Optional[str]]:
    return try_load_footer(path)


def _exit_code(report: Dict[str, object]) -> int:
    summary = report["summary"]
    directions = summary["directions"]
    if "incompatible" == directions["old_to_new"]["status"] or \
            "incompatible" == directions["new_to_old"]["status"]:
        return EXIT_INCOMPATIBLE
    if "unverifiable" == directions["old_to_new"]["status"] or \
            "unverifiable" == directions["new_to_old"]["status"]:
        return EXIT_UNVERIFIABLE
    if "compatible_with_migration" == directions["old_to_new"]["status"] or \
            "compatible_with_migration" == directions["new_to_old"]["status"]:
        return EXIT_MIGRATION
    return EXIT_COMPATIBLE


def _print_payload(payload: Dict[str, object], args: Namespace) -> None:
    if args.format == "json":
        indent = args.json_indent if args.json_indent > 0 else None
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=False,
                indent=indent,
                separators=(",", ": ") if indent else (",", ":"),
            )
        )
    else:
        _print_text(payload)


def _print_text(payload: Dict[str, object]) -> None:
    if payload.get("status") == "read_error":
        print("status: read_error")
        for error in payload.get("errors", []):
            print(f"error: {error}")
        return
    print("Parquet footer schema diff (read-only; footer bytes only)")
    inputs = payload.get("inputs", {})
    for label in ("old", "new"):
        document = inputs.get(label, {})
        print(
            f"{label}: {document.get('path')} "
            f"(footer {document.get('footer_size')} bytes, "
            f"sha256 {document.get('footer_sha256')})"
        )
    print(f"policy: {json.dumps(payload.get('policy'), ensure_ascii=False)}")
    summary = payload["summary"]
    for direction in ("old_to_new", "new_to_old"):
        item = summary["directions"][direction]
        print(
            f"{direction}: {item['status']} "
            f"(migration={item['requires_migration']}, "
            f"incompatible={item['incompatible_changes']}, "
            f"unverifiable={item['unverifiable_changes']})"
        )
    for change in payload["changes"]:
        old = change["old"]
        new = change["new"]
        old_text = old["physical_path"] if old else "-"
        new_text = new["physical_path"] if new else "-"
        old_to_new = change["compatibility"]["old_to_new"]["status"]
        new_to_old = change["compatibility"]["new_to_old"]["status"]
        print(
            f"{change['change_id']} {change['impact']} {change['kind']} "
            f"[{change['change_level']}] {old_text} -> {new_text} "
            f"(old->new {old_to_new}, new->old {new_to_old}, "
            f"identity {change['identity']['basis']})"
        )
    for group in payload["pending_confirmations"]:
        print(
            f"PENDING {group['kind']}: {group['reason']} "
            f"(old candidates {len(group['old_candidates'])}, "
            f"new candidates {len(group['new_candidates'])})"
        )
    for check in payload.get("value_verification", []):
        print(
            f"VALUE-CHECK {check['kind']} [{check['change_level']}] "
            f"{check['old_path']} -> {check['new_path']} "
            f"({check['direction']}, {check['value_verification']})"
        )
