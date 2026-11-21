# Copyright 2026-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contract tests for the ``integer_decode_paths`` CodecOptions parameter.

Two collectors read the same BSON bytes with different declarations:

* Party A treats the BSON integers at ``rows.amount`` as int64.
* Party B only treats the BSON integer at ``totals.amount`` as a plain int.

The same bytes are also decoded with an interleaved declaration and with the
legacy options that declare nothing. Every check runs against both the C
extension and the pure-Python decoder so both implementations honor the same
contract.
"""

from __future__ import annotations

import sys
from typing import Any

sys.path[0:0] = [""]

from bson.binary import USER_DEFINED_SUBTYPE
from test import unittest

INT32_VALUE = 42
INT64_VALUE = 2**40
BOOL_VALUE = True


def _load_pure_python_bson():
    """Re-import bson with the C extension blocked, then restore sys.modules."""
    saved = {
        key: sys.modules.pop(key)
        for key in list(sys.modules)
        if key == "bson" or key.startswith("bson.")
    }
    sys.modules["bson._cbson"] = None  # type: ignore[assignment]
    try:
        import bson as pure_bson

        # Import the submodule inside the isolated context so it resolves to
        # the pure-Python build and remains attached to the package object.
        import bson.raw_bson  # noqa: F401
    finally:
        for key in list(sys.modules):
            if key == "bson" or key.startswith("bson."):
                del sys.modules[key]
        sys.modules.update(saved)
    return pure_bson


class _IntegerDecodePathsContract:
    """Shared assertions, bound to either the C or pure-Python bson module."""

    @classmethod
    def _bind(cls, mod: Any) -> None:
        cls.mod = mod
        cls.CodecOptions = mod.codec_options.CodecOptions
        cls.IntegerDecoding = mod.codec_options.IntegerDecoding
        cls.TypeRegistry = mod.codec_options.TypeRegistry
        cls.TypeDecoder = mod.codec_options.TypeDecoder
        cls.Int64 = mod.int64.Int64
        cls.Binary = mod.binary.Binary
        cls.RawBSONDocument = mod.raw_bson.RawBSONDocument

    def setUp(self) -> None:
        # The Binary payload decodes (via the legacy decoder) to a plain dict
        # whose int32 "amount" mirrors the native int32 in rows[0].
        self.binary_payload = self.mod.encode(
            {"amount": INT32_VALUE, "note": "embedded"}
        )
        self.binary_value = self.Binary(self.binary_payload, USER_DEFINED_SUBTYPE)
        self.document = {
            "rows": [
                {"amount": INT32_VALUE},
                {"amount": self.Int64(INT64_VALUE)},
                {"amount": BOOL_VALUE},
                {"blob": self.binary_value},
            ],
            "totals": {
                "amount": self.Int64(INT64_VALUE),
                "breakdown": [
                    {"amount": INT32_VALUE},
                    {"amount": self.Int64(INT64_VALUE)},
                ],
            },
        }
        self.encoded = self.mod.encode(self.document)

        self.party_a = self.CodecOptions(
            integer_decode_paths={"rows.amount": self.IntegerDecoding.INT64}
        )
        self.party_b = self.CodecOptions(
            integer_decode_paths={"totals.amount": self.IntegerDecoding.INT}
        )
        self.interleaved = self.CodecOptions(
            integer_decode_paths={
                "rows.amount": self.IntegerDecoding.INT64,
                "totals.amount": self.IntegerDecoding.INT,
            }
        )
        self.legacy = self.CodecOptions()

    def assert_is_plain_int(self, value: Any, expected: int) -> None:
        self.assertIs(type(value), int)
        self.assertEqual(value, expected)

    def assert_is_int64(self, value: Any, expected: int) -> None:
        self.assertIs(type(value), self.Int64)
        self.assertEqual(value, expected)

    def test_party_a_converts_only_declared_rows_integers(self) -> None:
        doc = self.mod.decode(self.encoded, self.party_a)

        rows = doc["rows"]
        # The native int32 at the declared path becomes an Int64 ...
        self.assert_is_int64(rows[0]["amount"], INT32_VALUE)
        # ... and the native int64 stays an Int64 with the same value.
        self.assert_is_int64(rows[1]["amount"], INT64_VALUE)
        # Booleans are not BSON integers and are left untouched.
        self.assertIs(type(rows[2]["amount"]), bool)
        self.assertIs(rows[2]["amount"], BOOL_VALUE)

        # totals.amount is not declared by party A: int64 keeps default Int64.
        self.assert_is_int64(doc["totals"]["amount"], INT64_VALUE)
        breakdown = doc["totals"]["breakdown"]
        self.assert_is_plain_int(breakdown[0]["amount"], INT32_VALUE)
        self.assert_is_int64(breakdown[1]["amount"], INT64_VALUE)

    def test_party_b_converts_only_declared_totals_integer(self) -> None:
        doc = self.mod.decode(self.encoded, self.party_b)

        # The declared int64 at totals.amount becomes a plain Python int.
        self.assert_is_plain_int(doc["totals"]["amount"], INT64_VALUE)

        # rows.amount is not declared: default decoding is preserved.
        rows = doc["rows"]
        self.assert_is_plain_int(rows[0]["amount"], INT32_VALUE)
        self.assert_is_int64(rows[1]["amount"], INT64_VALUE)
        self.assertIs(type(rows[2]["amount"]), bool)

        # The breakdown subdocuments also contain "amount" but at an
        # undeclared path, so their integers keep their default decoding.
        breakdown = doc["totals"]["breakdown"]
        self.assert_is_plain_int(breakdown[0]["amount"], INT32_VALUE)
        self.assert_is_int64(breakdown[1]["amount"], INT64_VALUE)

    def test_interleaved_declarations_apply_independently(self) -> None:
        doc = self.mod.decode(self.encoded, self.interleaved)

        rows = doc["rows"]
        self.assert_is_int64(rows[0]["amount"], INT32_VALUE)
        self.assert_is_int64(rows[1]["amount"], INT64_VALUE)
        self.assertIs(type(rows[2]["amount"]), bool)

        self.assert_is_plain_int(doc["totals"]["amount"], INT64_VALUE)
        breakdown = doc["totals"]["breakdown"]
        self.assert_is_plain_int(breakdown[0]["amount"], INT32_VALUE)
        self.assert_is_int64(breakdown[1]["amount"], INT64_VALUE)

    def test_legacy_options_without_declarations_keep_defaults(self) -> None:
        doc = self.mod.decode(self.encoded, self.legacy)

        rows = doc["rows"]
        self.assert_is_plain_int(rows[0]["amount"], INT32_VALUE)
        self.assert_is_int64(rows[1]["amount"], INT64_VALUE)
        self.assertIs(type(rows[2]["amount"]), bool)
        self.assert_is_int64(doc["totals"]["amount"], INT64_VALUE)
        breakdown = doc["totals"]["breakdown"]
        self.assert_is_plain_int(breakdown[0]["amount"], INT32_VALUE)
        self.assert_is_int64(breakdown[1]["amount"], INT64_VALUE)

    def test_binary_is_never_path_decoded_and_legacy_decoder_runs_once(self) -> None:
        calls = 0

        def legacy_decoder(binary: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return self.mod.decode(bytes(binary))

        for options in (self.party_a, self.party_b, self.interleaved, self.legacy):
            calls = 0
            doc = self.mod.decode(self.encoded, options)
            blob = doc["rows"][3]["blob"]

            # The engine leaves the BSON Binary in place and never invokes the
            # legacy binary decoder itself.
            self.assertIs(type(blob), self.Binary)
            self.assertEqual(calls, 0)

            # The consumer runs the legacy decoder exactly once. Even though
            # the resulting dict mirrors a native rows document and carries an
            # "amount" with the same integer, that integer is not at a declared
            # BSON path and therefore keeps default (plain int) decoding.
            embedded = legacy_decoder(blob)
            self.assertEqual(calls, 1)
            self.assert_is_plain_int(embedded["amount"], INT32_VALUE)
            self.assertEqual(embedded["note"], "embedded")

    def test_registered_binary_decoder_runs_once_and_is_not_path_converted(self) -> None:
        mod = self.mod

        class BinaryDictDecoder(self.TypeDecoder):
            bson_type = self.Binary

            def __init__(self) -> None:
                self.calls = 0

            def transform_bson(self, value: Any) -> dict[str, Any]:
                self.calls += 1
                return mod.decode(bytes(value))

        decoder = BinaryDictDecoder()
        options = self.CodecOptions(
            type_registry=self.TypeRegistry([decoder]),
            integer_decode_paths={"rows.amount": self.IntegerDecoding.INT64},
        )
        doc = mod.decode(self.encoded, options)

        # The native int32 at the declared path converts once to Int64 ...
        self.assert_is_int64(doc["rows"][0]["amount"], INT32_VALUE)
        # ... while the dict produced by the registered binary decoder keeps
        # the integer's default decoding even though it also has "amount".
        embedded = doc["rows"][3]["blob"]
        self.assertIsInstance(embedded, dict)
        self.assert_is_plain_int(embedded["amount"], INT32_VALUE)
        self.assertEqual(embedded["note"], "embedded")
        self.assertEqual(decoder.calls, 1)

    def test_other_dictionary_contents_are_preserved(self) -> None:
        doc = self.mod.decode(self.encoded, self.interleaved)

        self.assertEqual(
            set(doc),
            {"rows", "totals"},
        )
        self.assertEqual([set(row) for row in doc["rows"]], [
            {"amount"},
            {"amount"},
            {"amount"},
            {"blob"},
        ])
        self.assertEqual(set(doc["totals"]), {"amount", "breakdown"})
        self.assertEqual(doc["rows"][3]["blob"], self.binary_value)

    def test_negative_values_and_nested_arrays(self) -> None:
        document = {
            "a": [[{"c": -7}], [{"c": self.Int64(-INT64_VALUE)}]],
            "neg32": -(2**31),
            "neg64": self.Int64(-(2**63)),
        }
        options = self.CodecOptions(
            integer_decode_paths={
                "a.c": self.IntegerDecoding.INT64,
                "neg32": self.IntegerDecoding.INT64,
                "neg64": self.IntegerDecoding.INT,
            }
        )
        decoded = self.mod.decode(self.mod.encode(document), options)

        self.assert_is_int64(decoded["a"][0][0]["c"], -7)
        self.assert_is_int64(decoded["a"][1][0]["c"], -INT64_VALUE)
        self.assert_is_int64(decoded["neg32"], -(2**31))
        self.assert_is_plain_int(decoded["neg64"], -(2**63))

    def test_decode_all_and_iter_honor_the_declarations(self) -> None:
        docs = self.mod.decode_all(self.encoded, self.party_a)
        self.assertEqual(len(docs), 1)
        self.assert_is_int64(docs[0]["rows"][0]["amount"], INT32_VALUE)

        iterated = list(self.mod.decode_iter(self.encoded, self.party_b))
        self.assertEqual(len(iterated), 1)
        self.assert_is_plain_int(iterated[0]["totals"]["amount"], INT64_VALUE)

    def test_raw_bson_inflation_honors_the_declarations(self) -> None:
        # RawBSONDocument inflates each subdocument as its own BSON root, so a
        # declaration matches relative to the inflated document's own root.
        raw_options = self.CodecOptions(
            document_class=self.RawBSONDocument,
            integer_decode_paths={"amount": self.IntegerDecoding.INT64},
        )
        raw_doc = self.mod.decode(self.encoded, raw_options)

        inflated_row = raw_doc["rows"][0]
        self.assertIsInstance(inflated_row, self.RawBSONDocument)
        self.assert_is_int64(inflated_row["amount"], INT32_VALUE)
        self.assert_is_int64(raw_doc["rows"][1]["amount"], INT64_VALUE)

        # The undeclared "totals.amount" keeps its default int64 decoding.
        inflated_totals = raw_doc["totals"]
        self.assert_is_int64(inflated_totals["amount"], INT64_VALUE)


class TestIntegerDecodePathsCExtension(_IntegerDecodePathsContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import bson

        if not bson.has_c():
            raise unittest.SkipTest("C extension is not available")
        cls._bind(bson)


class TestIntegerDecodePathsPurePython(_IntegerDecodePathsContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._bind(_load_pure_python_bson())


def _type_name(value: Any) -> str:
    return type(value).__name__


def _assert_same_shape(testcase: unittest.TestCase, left: Any, right: Any) -> None:
    """Recursively compare values and their decoded types across modules."""
    if isinstance(left, dict):
        testcase.assertIsInstance(right, dict)
        testcase.assertEqual(set(left), set(right))
        for key in left:
            _assert_same_shape(testcase, left[key], right[key])
    elif isinstance(left, list):
        testcase.assertIsInstance(right, list)
        testcase.assertEqual(len(left), len(right))
        for left_item, right_item in zip(left, right, strict=True):
            _assert_same_shape(testcase, left_item, right_item)
    else:
        testcase.assertEqual(_type_name(left), _type_name(right))
        if isinstance(left, (bytes, bytearray)):
            testcase.assertEqual(bytes(left), bytes(right))
        else:
            testcase.assertEqual(left, right)


class TestCExtensionAndPurePythonParity(unittest.TestCase):
    """The C and pure-Python decoders return identical values and leaf types."""

    @classmethod
    def setUpClass(cls) -> None:
        import bson

        cls.c_bson = bson
        if not bson.has_c():
            raise unittest.SkipTest("C extension is not available")
        cls.pure_bson = _load_pure_python_bson()

    def _options_for(self, mod: Any) -> dict[str, Any]:
        CodecOptions = mod.codec_options.CodecOptions
        IntegerDecoding = mod.codec_options.IntegerDecoding
        return {
            "party_a": CodecOptions(
                integer_decode_paths={"rows.amount": IntegerDecoding.INT64}
            ),
            "party_b": CodecOptions(
                integer_decode_paths={"totals.amount": IntegerDecoding.INT}
            ),
            "interleaved": CodecOptions(
                integer_decode_paths={
                    "rows.amount": IntegerDecoding.INT64,
                    "totals.amount": IntegerDecoding.INT,
                }
            ),
            "legacy": CodecOptions(),
        }

    def _document_for(self, mod: Any) -> dict[str, Any]:
        payload = mod.encode({"amount": INT32_VALUE, "note": "embedded"})
        return {
            "rows": [
                {"amount": INT32_VALUE},
                {"amount": mod.int64.Int64(INT64_VALUE)},
                {"amount": BOOL_VALUE},
                {"blob": mod.binary.Binary(payload, USER_DEFINED_SUBTYPE)},
            ],
            "totals": {
                "amount": mod.int64.Int64(INT64_VALUE),
                "breakdown": [
                    {"amount": INT32_VALUE},
                    {"amount": mod.int64.Int64(INT64_VALUE)},
                ],
            },
        }

    def test_same_bytes_decode_to_the_same_typed_contract(self) -> None:
        c_encoded = self.c_bson.encode(self._document_for(self.c_bson))
        pure_encoded = self.pure_bson.encode(self._document_for(self.pure_bson))
        self.assertEqual(c_encoded, pure_encoded)

        c_options = self._options_for(self.c_bson)
        pure_options = self._options_for(self.pure_bson)
        for name in c_options:
            c_doc = self.c_bson.decode(c_encoded, c_options[name])
            pure_doc = self.pure_bson.decode(pure_encoded, pure_options[name])
            _assert_same_shape(self, c_doc, pure_doc)


if __name__ == "__main__":
    unittest.main()
