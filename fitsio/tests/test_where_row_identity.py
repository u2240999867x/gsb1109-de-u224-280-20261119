import json

import numpy as np
import pytest

from ..fitslib import FITS
from .where_row_diagnostic import (
    CONTROL_PHYSICAL,
    CONTROL_ROWIDS,
    PRIMARY_PHYSICAL,
    PRIMARY_ROWIDS,
    THRESHOLD,
    WINDOW,
    build_report,
    main,
    make_table,
    observe,
    probe_integer_scaled_read,
    read_stored_columns,
    summarise,
)


@pytest.fixture()
def primary_path(tmp_path):
    path = tmp_path / "primary.fits"
    make_table(path, PRIMARY_ROWIDS, PRIMARY_PHYSICAL)
    return path


@pytest.fixture()
def control_path(tmp_path):
    path = tmp_path / "control.fits"
    make_table(path, CONTROL_ROWIDS, CONTROL_PHYSICAL)
    return path


def test_where_window_indices_are_relative_not_global(primary_path):
    obs = observe(primary_path)
    physical = obs["physical"]

    expected_full = np.where(physical >= THRESHOLD)[0]
    np.testing.assert_array_equal(obs["full_ge"], expected_full)

    expected_window = np.where(physical[WINDOW[0] : WINDOW[1]] >= THRESHOLD)[0]
    np.testing.assert_array_equal(obs["window_ge"], expected_window)

    # The returned values are window-local.  They are not global rows.
    assert obs["window_ge"].tolist() == [0, 1, 2, 3]
    assert (obs["window_ge"] + WINDOW[0]).tolist() == [3, 4, 5, 6]
    np.testing.assert_array_equal(
        obs["rowids"][obs["window_ge"] + WINDOW[0]],
        np.array([1051, 1068, 1085, 1102], dtype="i8"),
    )


def test_native_window_arguments_are_one_based_and_length(primary_path):
    with FITS(primary_path) as fits:
        hdu = fits[1]
        expression = "BRIGHT >= %r" % THRESHOLD

        # TableHDU.where passes (firstrow + 1, lastrow - firstrow).
        # Native firstrow is 1-based and nrows is a length, not an end.
        native = hdu._FITS.where(
            hdu._ext + 1, expression, WINDOW[0] + 1, WINDOW[1] - WINDOW[0]
        )
        np.testing.assert_array_equal(
            native, hdu.where(expression, WINDOW[0], WINDOW[1])
        )

        # Shifting the native start by one row returns a different window,
        # which is why absolute rows need firstrow + relative_index.
        shifted = hdu._FITS.where(
            hdu._ext + 1,
            expression,
            WINDOW[0] + 2,
            WINDOW[1] - WINDOW[0],
        )
        np.testing.assert_array_equal(
            shifted, hdu.where(expression, WINDOW[0] + 1, WINDOW[1] + 1)
        )


def test_same_hit_count_different_source_rows(primary_path, control_path):
    primary = summarise(observe(primary_path))
    control = summarise(observe(control_path))

    assert primary["correct_count"] == 4
    assert primary["offset_count"] == 4
    assert primary["stored_count"] == 4
    assert control["correct_count"] == 4
    assert control["stored_count"] == 4

    # Offset misread: relative indices used as global rows.
    assert primary["offset_overlap"] == 1
    assert primary["offset_ids"].tolist() == [1001, 1017, 1034, 1051]
    assert primary["correct_ids"].tolist() == [1051, 1068, 1085, 1102]

    # Scaling interpretation: stored values compared with a physical threshold.
    assert primary["stored_overlap"] == 2
    assert primary["stored_ids"].tolist() == [1085, 1102, 1119, 1136]

    # A different set of source identifiers gives the same rectangular counts,
    # but it still does not identify the same rows.
    assert control["correct_ids"].tolist() == [7053, 7070, 7104, 7138]
    assert control["stored_ids"].tolist() == [7053, 7087, 7104, 7121]
    assert control["stored_overlap"] == 2
    assert set(control["correct_ids"]) != set(control["stored_ids"])


def test_representation_only_is_fixed_by_adding_firstrow(primary_path):
    obs = observe(primary_path)
    physical = obs["physical"]

    # Same numbers as the local Python slice: this is representation-only.
    local_python = np.where(physical[WINDOW[0] : WINDOW[1]] >= THRESHOLD)[0]
    np.testing.assert_array_equal(obs["window_ge"], local_python)

    # Adding firstrow maps the same rows to the global frame.
    global_python = np.where(physical >= THRESHOLD)[0]
    keep = (global_python >= WINDOW[0]) & (global_python < WINDOW[1])
    np.testing.assert_array_equal(
        obs["window_ge"] + WINDOW[0], global_python[keep]
    )


def test_threshold_and_window_endpoint_semantics(primary_path):
    obs = observe(primary_path)
    physical = obs["physical"]

    equal_rows = np.where(physical == THRESHOLD)[0]
    assert equal_rows.tolist() == [1, 6, 10]
    np.testing.assert_array_equal(obs["full_eq"], equal_rows)
    np.testing.assert_array_equal(
        obs["rowids"][obs["full_eq"]],
        np.array([1017, 1102, 1170], dtype="i8"),
    )

    # >= includes values exactly on the threshold; > does not.
    np.testing.assert_array_equal(
        np.setdiff1d(obs["full_ge"], obs["full_gt"]), equal_rows
    )

    # Left endpoint: row 3 has physical brightness exactly 8.0.
    assert physical[WINDOW[0]] == 8.0
    assert obs["endpoint_ge8"].tolist() == [0]
    assert obs["endpoint_gt8"].tolist() == []

    # Right endpoint is exclusive: row 8 is outside [3, 8) and inside [3, 9).
    assert physical[WINDOW[1] - 1] == 3.0
    assert obs["right_exclusive"].tolist() == [0, 1, 2, 3, 4]
    assert obs["left_inclusive"].tolist() == [0, 1, 2, 3, 4, 5]


def test_duplicate_brightness_does_not_imply_same_row(primary_path):
    obs = observe(primary_path)
    physical = obs["physical"]

    # Three distinct source rows share physical brightness 5.0.
    assert obs["full_eq"].tolist() == [1, 6, 10]
    assert len(set(obs["rowids"][obs["full_eq"]].tolist())) == 3

    # Three distinct stored values equal 5.0, but they are different rows and
    # their physical brightness is 6.0, not 5.0.
    assert obs["stored_full_eq"].tolist() == [0, 5, 9]
    np.testing.assert_array_equal(
        physical[obs["stored_full_eq"]], np.array([6.0, 6.0, 6.0])
    )

    # Same count, same read-back brightness, different identities.
    assert obs["full_eq"].size == obs["stored_full_eq"].size == 3
    assert set(obs["rowids"][obs["full_eq"]]) != set(
        obs["rowids"][obs["stored_full_eq"]]
    )


def test_empty_window_is_rejected_not_empty_result(primary_path):
    with FITS(primary_path) as fits:
        hdu = fits[1]
        expression = "BRIGHT >= %r" % THRESHOLD

        with pytest.raises(
            ValueError,
            match="firstrow and nrows must be positive integers",
        ):
            hdu.where(expression, WINDOW[0], WINDOW[0])

        # The native layer rejects the same zero-length window before
        # fits_find_rows is reached.
        with pytest.raises(
            ValueError,
            match="firstrow and nrows must be positive integers",
        ):
            hdu._FITS.where(hdu._ext + 1, expression, WINDOW[0] + 1, 0)

        assert hdu.where("BRIGHT > 1.0e99").size == 0
        assert hdu.where("BRIGHT > 1.0e99", WINDOW[0], WINDOW[1]).size == 0


def test_boundary_rejections_are_distinct_from_no_match(primary_path):
    with FITS(primary_path) as fits:
        hdu = fits[1]

        with pytest.raises(ValueError, match="firstrow cannot be negative"):
            hdu.where("BRIGHT >= 5.0", -1, 3)
        with pytest.raises(
            ValueError, match="lastrow cannot be less than firstrow"
        ):
            hdu.where("BRIGHT >= 5.0", 6, 2)
        with pytest.raises(
            ValueError, match="lastrow cannot be greater than nrows"
        ):
            hdu.where("BRIGHT >= 5.0", 0, hdu.get_nrows() + 1)

        # A valid window with no true rows is an empty array, not an error.
        assert hdu.where("BRIGHT >= 5.0", 11, 12).size == 0


def test_scaled_column_predicate_is_physical_layer(primary_path):
    obs = observe(primary_path)

    # The stored bytes and the public read are intentionally different.
    assert not np.array_equal(obs["stored"], obs["physical"])
    np.testing.assert_array_equal(
        obs["stored"],
        np.array([5.0, 6.0, 3.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 5.0, 6.0, 7.0]),
    )
    np.testing.assert_array_equal(
        obs["physical"],
        np.array([6.0, 5.0, 8.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 6.0, 5.0, 4.0]),
    )

    # where() matches the physical TSCAL/TZERO interpretation.
    np.testing.assert_array_equal(
        obs["full_ge"], np.where(obs["physical"] >= THRESHOLD)[0]
    )

    # The stored-layer interpretation has the same count but different rows.
    np.testing.assert_array_equal(
        obs["stored_full_ge"], np.where(obs["stored"] >= THRESHOLD)[0]
    )
    assert obs["full_ge"].size == obs["stored_full_ge"].size == 9
    assert set(obs["rowids"][obs["full_ge"]]) != set(
        obs["rowids"][obs["stored_full_ge"]]
    )


def test_read_path_control_for_integer_scaled_column(tmp_path):
    path = tmp_path / "integer.fits"
    result = probe_integer_scaled_read(path)

    # The predicate uses the scaled physical values.
    np.testing.assert_array_equal(
        result["where_gt"], np.where(result["expected"] > 2.0)[0]
    )

    # The public read path disagrees in this snapshot.  This is a separate
    # read-layer control, not a change to where().
    assert not np.array_equal(result["read"], result["expected"])


def test_stored_reader_preserves_independent_rowids(tmp_path):
    path = tmp_path / "primary.fits"
    make_table(path, PRIMARY_ROWIDS, PRIMARY_PHYSICAL)
    with FITS(path) as fits:
        stored = read_stored_columns(fits[1])
    np.testing.assert_array_equal(stored["ROWID"], PRIMARY_ROWIDS)


def test_json_report_is_serializable_and_consistent(
    primary_path, tmp_path, capsys
):
    inventory = build_report()
    assert inventory["primary_summary"]["correct_ids"].tolist() == [
        1051,
        1068,
        1085,
        1102,
    ]

    assert main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primary"]["window_ge"] == [0, 1, 2, 3]
    assert payload["primary_summary"]["offset_ids"] == [1001, 1017, 1034, 1051]
    assert payload["primary_summary"]["stored_ids"] == [
        1085,
        1102,
        1119,
        1136,
    ]
    assert payload["primary_summary"]["offset_overlap"] == 1
    assert payload["primary_summary"]["stored_overlap"] == 2
