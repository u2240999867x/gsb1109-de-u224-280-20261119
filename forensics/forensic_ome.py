#!/usr/bin/env python3
"""Forensic audit of multifile OME-TIFF previews using current tifffile.

This module intentionally does not change tifffile's reading behavior.  It
reproduces the delivered four-plane case, records what the OME series actually
uses as each plane's source and decode keyframe, and compares that with a
direct, isolated read of the expected source IFD.

Commands::

    python forensics/forensic_ome.py build --out forensics/cases
    python forensics/forensic_ome.py audit --case forensics/cases/base
    python forensics/forensic_ome.py scenarios --case forensics/cases/base
    python forensics/forensic_ome.py verify --cases forensics/cases

The audit is read-only.  Scenario commands copy the case to a temporary
directory before hiding or restoring the companion file.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import warnings
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import numpy
import tifffile


MASTER_NAME = 'M.ome.tif'
COMPANION_NAME = 'B.ome.tif'
MASTER_UUID = 'urn:uuid:11111111-1111-1111-1111-111111111111'
COMPANION_UUID = 'urn:uuid:22222222-2222-2222-2222-222222222222'
DEFLATE = 32946


# ---------------------------------------------------------------------------
# fixture builder


def ome_xml(
    *,
    uuid: str,
    name: str,
    master_references: list[str] | None = None,
    size_z: int = 4,
) -> str:
    """Return an OME-XML document for the master or companion file."""
    references = '' if master_references is None else '\n'.join(
        master_references
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" UUID="{uuid}">
<Image ID="Image:0" Name="{name}">
<Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint8" SizeT="1" SizeC="1" SizeZ="{size_z}" SizeY="4" SizeX="4">
<Channel ID="Channel:0" SamplesPerPixel="1"/>
{references}
</Pixels></Image></OME>'''


def master_reference(
    *,
    file_name: str,
    uuid: str,
    ifd: int,
    first_z: int,
) -> str:
    """Return one OME TiffData reference."""
    return (
        f'<TiffData IFD="{ifd}" FirstZ="{first_z}" PlaneCount="1">'
        f'<UUID FileName="{file_name}">{uuid}</UUID>'
        '</TiffData>'
    )


def write_master(
    path: Path,
    *,
    reference_order: str,
    zmap: tuple[int, int],
    predictors: tuple[int, int, int],
    rows: tuple[tuple[int, ...], tuple[int, ...]],
) -> None:
    """Write the four-plane master with two references to the companion."""
    master_references = [
        master_reference(
            file_name=MASTER_NAME, uuid=MASTER_UUID, ifd=0, first_z=0
        ),
        master_reference(
            file_name=MASTER_NAME, uuid=MASTER_UUID, ifd=zmap[0], first_z=1
        ),
        master_reference(
            file_name=MASTER_NAME, uuid=MASTER_UUID, ifd=zmap[1], first_z=2
        ),
        master_reference(
            file_name=COMPANION_NAME,
            uuid=COMPANION_UUID,
            ifd=0,
            first_z=0,
        ),
        master_reference(
            file_name=COMPANION_NAME,
            uuid=COMPANION_UUID,
            ifd=1,
            first_z=3,
        ),
    ]
    if reference_order == 'B_first':
        master_references = [
            master_references[3],
            master_references[4],
            *master_references[:3],
        ]

    description = ome_xml(
        uuid=MASTER_UUID,
        name='M',
        master_references=master_references,
    )
    zero_row = numpy.zeros(4, dtype=numpy.uint8)
    row_one = numpy.asarray(rows[0], dtype=numpy.uint8)
    row_two = numpy.asarray(rows[1], dtype=numpy.uint8)

    with tifffile.TiffWriter(path) as writer:
        writer.write(
            numpy.tile(zero_row, (4, 1)),
            compression='deflate',
            predictor=bool(predictors[0] == 2),
            rowsperstrip=2,
            metadata=None,
            description=description,
        )
        writer.write(
            numpy.tile(row_one, (4, 1)),
            compression='deflate',
            predictor=bool(predictors[1] == 2),
            rowsperstrip=2,
            metadata=None,
        )
        writer.write(
            numpy.tile(row_two, (4, 1)),
            compression='deflate',
            predictor=bool(predictors[2] == 2),
            rowsperstrip=2,
            metadata=None,
        )


def write_companion(path: Path) -> None:
    """Write two true-zero companion pages with the companion UUID."""
    description = ome_xml(uuid=COMPANION_UUID, name='B')
    zeros = numpy.zeros((4, 4), dtype=numpy.uint8)
    with tifffile.TiffWriter(path) as writer:
        writer.write(
            zeros,
            compression='deflate',
            predictor=False,
            rowsperstrip=2,
            metadata=None,
            description=description,
        )
        writer.write(
            zeros,
            compression='deflate',
            predictor=False,
            rowsperstrip=2,
            metadata=None,
        )


def make_case(
    directory: Path,
    *,
    name: str,
    description: str,
    reference_order: str,
    zmap: tuple[int, int],
    predictors: tuple[int, int, int],
    rows: tuple[tuple[int, ...], tuple[int, ...]],
) -> dict[str, Any]:
    """Create one deterministic delivery case and its manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    master_path = directory / MASTER_NAME
    companion_path = directory / COMPANION_NAME
    write_master(
        master_path,
        reference_order=reference_order,
        zmap=zmap,
        predictors=predictors,
        rows=rows,
    )
    write_companion(companion_path)

    manifest = {
        'name': name,
        'description': description,
        'master': MASTER_NAME,
        'companion': COMPANION_NAME,
        'uuid': {
            'master': MASTER_UUID,
            'companion': COMPANION_UUID,
        },
        'expected': {
            'Z0': {'file': COMPANION_NAME, 'ifd': 0},
            'Z1': {'file': MASTER_NAME, 'ifd': zmap[0]},
            'Z2': {'file': MASTER_NAME, 'ifd': zmap[1]},
            'Z3': {'file': COMPANION_NAME, 'ifd': 1},
        },
        'conditions': {
            'reference_order': reference_order,
            'master_reference_ifds': list(zmap),
            'master_predictors': list(predictors),
            'first_rows': [list(row) for row in rows],
        },
    }
    (directory / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    )
    return manifest


def build_cases(output_root: Path) -> list[Path]:
    """Build the baseline and one-condition controls."""
    output_root.mkdir(parents=True, exist_ok=True)
    cases = [
        {
            'name': 'base',
            'description': '基线：M IFD0 不分差，M IFD1 水平差分，M IFD2 不分差',
            'reference_order': 'M_first',
            'zmap': (1, 2),
            'predictors': (1, 2, 1),
            'rows': ((10, 13, 17, 22), (20, 24, 29, 35)),
        },
        {
            'name': 'control_ref_order',
            'description': '单条件对照：只把 B 的 TiffData 引用移到 M 之前',
            'reference_order': 'B_first',
            'zmap': (1, 2),
            'predictors': (1, 2, 1),
            'rows': ((10, 13, 17, 22), (20, 24, 29, 35)),
        },
        {
            'name': 'control_swap_pages',
            'description': '单条件对照：只交换 Z1/Z2 指向的 M IFD（2/1）',
            'reference_order': 'M_first',
            'zmap': (2, 1),
            'predictors': (1, 2, 1),
            'rows': ((10, 13, 17, 22), (20, 24, 29, 35)),
        },
        {
            'name': 'control_swap_predictor',
            'description': '单条件对照：只交换 M IFD1/IFD2 的差分布局',
            'reference_order': 'M_first',
            'zmap': (1, 2),
            'predictors': (1, 1, 2),
            'rows': ((10, 13, 17, 22), (20, 24, 29, 35)),
        },
        {
            'name': 'control_pixels',
            'description': '单条件对照：只改 M IFD1/IFD2 的行内像素',
            'reference_order': 'M_first',
            'zmap': (1, 2),
            'predictors': (1, 2, 1),
            'rows': ((200, 30, 40, 50), (90, 5, 7, 9)),
        },
    ]
    paths: list[Path] = []
    for specification in cases:
        path = output_root / specification['name']
        make_case(
            path,
            name=specification['name'],
            description=specification['description'],
            reference_order=specification['reference_order'],
            zmap=specification['zmap'],
            predictors=specification['predictors'],
            rows=specification['rows'],
        )
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# manifest and direct page reads


def normalise_name(value: str) -> str:
    """Return just the file name for comparisons and reports."""
    return Path(value).name


def load_manifest(case_dir: Path) -> dict[str, Any]:
    """Load the case manifest, or derive expected references from OME-XML."""
    manifest_path = case_dir / 'manifest.json'
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())

    master_path = case_dir / MASTER_NAME
    with tifffile.TiffFile(master_path) as tif:
        root = ElementTree.fromstring(tif.ome_metadata)
    expected: dict[str, dict[str, Any]] = {}
    default_file = master_path.name
    for element in root.iter():
        if not element.tag.endswith('TiffData'):
            continue
        ifd = int(element.attrib.get('IFD', 0))
        first_z = int(element.attrib.get('FirstZ', 0))
        plane_count = int(
            element.attrib.get(
                'PlaneCount', element.attrib.get('NumPlanes', 1)
            )
        )
        file_name = default_file
        for child in element:
            if child.tag.endswith('UUID') and child.attrib.get('FileName'):
                file_name = normalise_name(child.attrib['FileName'])
                break
        for offset in range(plane_count):
            expected[f'Z{first_z + offset}'] = {
                'file': file_name,
                'ifd': ifd + offset,
            }
    return {
        'name': case_dir.name,
        'master': master_path.name,
        'companion': COMPANION_NAME,
        'expected': expected,
        'conditions': {},
    }


def interpret_raw(
    raw_first_row: list[int] | None,
    predictor: int,
) -> list[int] | None:
    """Return the first row after applying one IFD's predictor rule."""
    if raw_first_row is None:
        return None
    raw = numpy.asarray(raw_first_row, dtype=numpy.uint8)
    if predictor == 2:  # PREDICTOR.HORIZONTAL, uint8 wraps like TIFF
        return numpy.add.accumulate(raw).tolist()
    return raw.tolist()


def read_page_direct(
    path: Path,
    ifd: int,
    cache: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    """Read one IFD in a fresh TiffFile, so the page is its own keyframe."""
    cache_key = (str(path.resolve()), int(ifd))
    if cache_key in cache:
        return cache[cache_key]

    with tifffile.TiffFile(path) as tif:
        page = tif.pages[ifd]
        array = numpy.asarray(page.asarray()).copy()
        raw_first_row = None
        if (
            page.compression in {8, DEFLATE}
            and page.dataoffsets
            and page.databytecounts
        ):
            try:
                filehandle = tif.filehandle
                filehandle.seek(page.dataoffsets[0])
                encoded = filehandle.read(page.databytecounts[0])
                decoded = zlib.decompress(encoded)
                dtype = numpy.dtype(tif.byteorder + page.dtype.char)
                raw = numpy.frombuffer(decoded, dtype=dtype)
                raw_first_row = raw[: page.imagewidth].tolist()
            except Exception:
                raw_first_row = None
        result = {
            'file': path.name,
            'ifd': int(ifd),
            'offset': int(page.offset),
            'kind': 'TiffPage',
            'predictor': int(page.predictor),
            'compression': int(page.compression),
            'rowsperstrip': int(page.rowsperstrip),
            'shape': list(array.shape),
            'first_row': array[0].tolist() if array.ndim >= 1 else [],
            'raw_first_row': raw_first_row,
            'raw_under_own_rule': interpret_raw(
                raw_first_row, int(page.predictor)
            ),
            'array': array,
        }
    cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# preview audit


def page_source(page: Any) -> dict[str, Any] | None:
    """Describe the actual page selected by the OME series."""
    if page is None:
        return None
    keyframe = page.keyframe
    return {
        'file': normalise_name(page.parent.filename),
        'ifd': int(page.index),
        'offset': int(page.offset),
        'kind': type(page).__name__,
        'keyframe_file': normalise_name(keyframe.parent.filename),
        'keyframe_ifd': int(keyframe.index),
        'keyframe_offset': int(keyframe.offset),
        'keyframe_predictor': int(keyframe.predictor),
    }


def plane_for_z(
    preview: numpy.ndarray,
    series: Any,
    z_index: int,
) -> numpy.ndarray:
    """Return the 2-D preview plane for one Z index."""
    axes = series.axes
    if 'Z' not in axes:
        return preview[z_index]
    if axes.index('Z') == 0:
        return preview[z_index]
    index = [0] * preview.ndim
    index[axes.index('Z')] = z_index
    return preview[tuple(index)]


def zero_classification(
    *,
    page: dict[str, Any] | None,
    expected: dict[str, Any],
    zero: bool,
) -> str | None:
    """Classify an all-zero preview plane by its actual source."""
    if not zero:
        return None
    if page is None:
        return 'fill_missing_page'
    if (
        page['file'] == expected['file']
        and page['ifd'] == int(expected['ifd'])
    ):
        return 'true_zero_expected_page'
    if page['file'] == MASTER_NAME:
        return 'substitute_master_real_page'
    if page['file'] == COMPANION_NAME:
        return 'companion_real_page_wrong_reference'
    return 'other_real_page'


DECISIONS = {
    'OK': '可入库',
    'REF_OK_PIXELS_WRONG': '暂缓：引用对但像素按错规则解释',
    'SOURCE_MISMATCH': '暂缓：实际页来源与交件清单不符',
    'FILL_MISSING': '暂缓：伴随文件缺失，当前为填充值',
}


def layer_array(page: Any, **kwargs: Any) -> numpy.ndarray:
    """Read a page's array, reopening closed companion handles silently.

    OME series close companion TiffFile instances after caching their pages.
    ``TiffPage.asarray`` can reopen them, but warns once per read.
    """
    if page is None:
        raise ValueError('page is None')
    filehandle = page.parent.filehandle
    was_closed = filehandle.closed
    if was_closed:
        filehandle.open()
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', message='.*reading array from closed file.*'
            )
            return numpy.asarray(page.asarray(**kwargs))
    finally:
        if was_closed:
            filehandle.close()


def audit_open_tif(
    tif: tifffile.TiffFile,
    case_dir: Path,
    manifest: dict[str, Any],
    *,
    direct_cache: dict[tuple[str, int], dict[str, Any]] | None = None,
    layers: set[str] | None = None,
) -> dict[str, Any]:
    """Audit an already-open TiffFile after its series has been built.

    If ``layers`` is given, only those planes are decoded.  The full preview is
    decoded when ``layers`` is ``None``.  Selected planes are always compared
    with a direct read of their expected IFD and, for non-OK layers, with a
    direct read of the keyframe page that supplied the decode rule.
    """
    if direct_cache is None:
        direct_cache = {}
    series = tif.series[0]
    expected_map = manifest['expected']
    rows: list[dict[str, Any]] = []
    layer_count = len(expected_map)
    selected = (
        set(expected_map)
        if layers is None
        else {layer for layer in expected_map if layer in layers}
    )
    preview = tif.asarray() if layers is None else None
    keyframe_shape = series.keyframe.shape

    for z_index in range(layer_count):
        layer = f'Z{z_index}'
        if layer not in selected:
            continue
        expected = expected_map[layer]
        expected_path = case_dir / expected['file']
        page = series[z_index]
        source = page_source(page)
        if preview is not None:
            plane = plane_for_z(preview, series, z_index)
        elif page is not None:
            plane = layer_array(page)
        else:
            plane = numpy.zeros(keyframe_shape, dtype=series.dtype)
        preview_first_row = plane[0].tolist() if plane.ndim >= 1 else []
        zero = bool(numpy.all(plane == 0))

        direct_expected: dict[str, Any] | None = None
        direct_actual: dict[str, Any] | None = None
        keyframe_direct: dict[str, Any] | None = None
        pixel_match_expected = False
        pixel_match_actual = False
        direct_error = None
        try:
            if expected_path.exists():
                direct_expected = read_page_direct(
                    expected_path, int(expected['ifd']), direct_cache
                )
                pixel_match_expected = bool(
                    numpy.array_equal(plane, direct_expected['array'])
                )
        except Exception as exc:
            direct_error = f'{type(exc).__name__}: {exc}'

        if source is not None:
            actual_path = case_dir / source['file']
            try:
                direct_actual = read_page_direct(
                    actual_path, source['ifd'], direct_cache
                )
                pixel_match_actual = bool(
                    numpy.array_equal(plane, direct_actual['array'])
                )
            except Exception as exc:
                direct_error = f'{type(exc).__name__}: {exc}'
            if (
                source['keyframe_file'] != source['file']
                or source['keyframe_ifd'] != source['ifd']
            ):
                try:
                    keyframe_direct = read_page_direct(
                        case_dir / source['keyframe_file'],
                        source['keyframe_ifd'],
                        direct_cache,
                    )
                except Exception as exc:
                    direct_error = f'{type(exc).__name__}: {exc}'
            else:
                keyframe_direct = direct_actual

        rule_check: dict[str, Any] | None = None
        if direct_actual is not None and source is not None:
            raw = direct_actual['raw_first_row']
            own_rule = direct_actual['raw_under_own_rule']
            preview_rule = interpret_raw(raw, source['keyframe_predictor'])
            rule_check = {
                'raw_first_row': raw,
                'source_ifd_predictor': direct_actual['predictor'],
                'source_own_rule_first_row': own_rule,
                'preview_keyframe_predictor': source['keyframe_predictor'],
                'preview_rule_first_row': preview_rule,
                'preview_first_row': preview_first_row,
                'preview_equals_own_rule': bool(
                    own_rule is not None
                    and own_rule == preview_first_row
                ),
                'preview_equals_preview_rule': bool(
                    preview_rule is not None
                    and preview_rule == preview_first_row
                ),
            }
            if keyframe_direct is not None:
                rule_check['keyframe_first_row'] = keyframe_direct[
                    'first_row'
                ]
                rule_check['keyframe_ifd'] = keyframe_direct['ifd']
                rule_check['keyframe_file'] = keyframe_direct['file']

        zero_probe_first_row: list[int] | None = None
        zero_probe_changed = False
        if zero:
            probe_dtype = numpy.dtype(series.dtype)
            if page is None:
                probe = numpy.full(
                    keyframe_shape, 255, dtype=probe_dtype
                )
            else:
                probe = layer_array(page, fillvalue=255)
            zero_probe_first_row = probe[0].tolist() if probe.ndim else []
            zero_probe_changed = not numpy.array_equal(plane, probe)

        source_matches_expected = bool(
            source is not None
            and source['file'] == expected['file']
            and source['ifd'] == int(expected['ifd'])
        )
        if source is None:
            status = 'FILL_MISSING'
        elif not source_matches_expected:
            status = 'SOURCE_MISMATCH'
        elif not pixel_match_expected:
            status = 'REF_OK_PIXELS_WRONG'
        else:
            status = 'OK'

        rows.append(
            {
                'layer': layer,
                'expected': {
                    'file': expected['file'],
                    'ifd': int(expected['ifd']),
                },
                'source': source,
                'source_matches_expected': source_matches_expected,
                'status': status,
                'decision': DECISIONS[status],
                'zero': zero,
                'zero_class': zero_classification(
                    page=source, expected=expected, zero=zero
                ),
                'zero_probe_first_row': zero_probe_first_row,
                'zero_probe_changed': zero_probe_changed,
                'preview_first_row': preview_first_row,
                'direct_expected': direct_expected,
                'direct_actual': direct_actual,
                'keyframe_direct': keyframe_direct,
                'pixel_match_expected': pixel_match_expected,
                'pixel_match_actual': pixel_match_actual,
                'rule_check': rule_check,
                'direct_error': direct_error,
            }
        )

    return {
        'case': manifest.get('name', case_dir.name),
        'description': manifest.get('description', ''),
        'series_axes': series.axes,
        'series_shape': list(series.shape),
        'rows': rows,
    }


def audit_case(
    case_dir: Path,
    *,
    layers: set[str] | None = None,
) -> dict[str, Any]:
    """Open the master, build one OME preview, and audit every layer."""
    manifest = load_manifest(case_dir)
    with tifffile.TiffFile(case_dir / manifest['master']) as tif:
        result = audit_open_tif(
            tif, case_dir, manifest, layers=layers
        )
    result['manifest'] = manifest
    return result


# ---------------------------------------------------------------------------
# scenario reproduction


def copy_case(case_dir: Path, target: Path) -> Path:
    """Copy a case so that the companion can be hidden without side effects."""
    copy = target / case_dir.name
    shutil.copytree(case_dir, copy)
    return copy


def hide_companion(case_dir: Path) -> Path:
    """Move the companion aside and return its hidden path."""
    companion = case_dir / COMPANION_NAME
    hidden = case_dir / 'B.hidden'
    if companion.exists():
        companion.rename(hidden)
    return hidden


def restore_companion(case_dir: Path) -> None:
    """Restore the companion after a missing-file preview."""
    hidden = case_dir / 'B.hidden'
    companion = case_dir / COMPANION_NAME
    if hidden.exists():
        hidden.rename(companion)


def reproduce_scenarios(case_dir: Path) -> dict[str, Any]:
    """Run the five operational sequences on isolated copies."""
    scenarios: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix='ome-forensics-') as temp:
        temp_root = Path(temp)

        # A: B missing, first whole preview.
        work = copy_case(case_dir, temp_root / 'missing')
        hide_companion(work)
        manifest = load_manifest(work)
        with tifffile.TiffFile(work / manifest['master']) as tif:
            audit = audit_open_tif(tif, work, manifest)
        scenarios.append(
            {
                'name': 'A_missing_first_preview',
                'title': 'B 未到时打开 M 做整组预览',
                'notes': [
                    'Z0 的后续 TiffData 引用未生效，仍指向 M IFD0 的真实零页。',
                    'Z3 无页可选，按 fillvalue=0 填充。',
                    '唯一 M keyframe 是 M IFD0（predictor=1），M IFD1/2 都以它为规则。',
                ],
                'audit': audit,
            }
        )

        # B: same instance after companion arrives, preview again.
        with tifffile.TiffFile(work / manifest['master']) as tif:
            before = audit_open_tif(tif, work, manifest)
            restore_companion(work)
            after = audit_open_tif(tif, work, manifest)
        scenarios.append(
            {
                'name': 'B_same_instance_after_companion',
                'title': '保留 B 未到时打开的实例；补件后同实例重看',
                'notes': [
                    '同一个 TiffFile 实例的 _series 缓存已建立，不会因 B 出现而重跑 OME 解析。',
                    '来源仍是 M IFD0/1/2，Z3 仍是 None 填充；与补件前的读数逐位相同。',
                ],
                'audit': after,
                'before': before,
                'after': after,
            }
        )

        # C: new instance after companion, whole preview.
        work_present = copy_case(case_dir, temp_root / 'present')
        manifest_present = load_manifest(work_present)
        with tifffile.TiffFile(work_present / manifest_present['master']) as tif:
            audit = audit_open_tif(tif, work_present, manifest_present)
        scenarios.append(
            {
                'name': 'C_new_instance_whole_preview',
                'title': 'B 补齐后新开 M 做整组预览',
                'notes': [
                    'B 被载入，Z0/Z3 指向 B IFD0/1；Z1/Z2 指向 M IFD1/2。',
                    '第一个 M 页（IFD1，predictor=2）成为该文件在 series 内的 keyframe；M IFD2 被按 predictor=2 解码。',
                ],
                'audit': audit,
            }
        )

        # D: new instance; page IFD2 read and cached before preview.
        with tifffile.TiffFile(work_present / manifest_present['master']) as tif:
            tif.pages.cache = True
            page = tif.pages[2]
            page_first_row = page.asarray()[0].tolist()
            audit = audit_open_tif(tif, work_present, manifest_present)
        scenarios.append(
            {
                'name': 'D_page_ifd2_cached_first',
                'title': '新开 M，先经页入口读取并缓存 M IFD2，再整组预览',
                'notes': [
                    'cache=True 时 pages[2] 返回并缓存真正的 TiffPage；它保留自己的 predictor=1。',
                    'series 组装时该 TiffPage 作为自身 keyframe，M IFD2 不再被 M IFD1 的规则覆盖。',
                    f'页入口当时的 M IFD2 首行直读：{page_first_row}。',
                ],
                'audit': audit,
                'page_entry_first_row': page_first_row,
            }
        )

        # E: preview first, then page check and preview again.
        with tifffile.TiffFile(work_present / manifest_present['master']) as tif:
            first = audit_open_tif(tif, work_present, manifest_present)
            page = tif.pages[2]
            cached_frame_first_row = page.asarray()[0].tolist()
            actual_page = page.aspage()
            actual_page_first_row = actual_page.asarray()[0].tolist()
            second = audit_open_tif(tif, work_present, manifest_present)
        scenarios.append(
            {
                'name': 'E_preview_then_page_check',
                'title': '先整组预览，再做同样的页检查并重看预览',
                'notes': [
                    '预览已经把 M IFD2 解析成 TiffFrame，keyframe=M IFD1（predictor=2），并缓存了整个 series。',
                    'pages[2] 返回该 TiffFrame，读数仍是错误的 cumsum 结果。',
                    'page.aspage() 从文件重读真正的 TiffPage（predictor=1），能给出正确读数，但不会替换 series 中的 TiffFrame。',
                    '重看预览仍使用已缓存的 series，错误不会改变；必须新开 TiffFile。',
                ],
                'audit': first,
                'page_check_frame_first_row': cached_frame_first_row,
                'page_check_aspage_first_row': actual_page_first_row,
                'audit_after_page_check': second,
            }
        )

    return {'case': case_dir.name, 'scenarios': scenarios}


# ---------------------------------------------------------------------------
# Markdown rendering


def row_to_markdown(row: dict[str, Any]) -> str:
    """Render one detailed audit row."""
    source = row['source']
    if source is None:
        source_text = '无（None，fillvalue 填充）'
        rule_text = '—'
    else:
        source_text = (
            f"{source['file']}:IFD{source['ifd']} "
            f"({source['kind']} @ {source['offset']})"
        )
        rule_text = (
            f"{source['keyframe_file']}:IFD{source['keyframe_ifd']} "
            f"predictor={source['keyframe_predictor']}"
        )
    direct = row['direct_expected']
    direct_text = '—'
    if direct is not None:
        direct_text = (
            f"首行 {direct['first_row']}; predictor={direct['predictor']}; "
            f"raw={direct['raw_first_row']}"
        )
    zero_text = row['zero_class'] or '—'
    if row['zero'] and row['zero_probe_first_row'] is not None:
        zero_text += (
            f"; fillvalue255 探针首行 {row['zero_probe_first_row']} "
            f"(读数变化={row['zero_probe_changed']})"
        )
    return (
        f"| {row['layer']} | {row['expected']['file']}:IFD"
        f"{row['expected']['ifd']} | {source_text} | {rule_text} | "
        f"{direct_text} | {row['preview_first_row']} | "
        f"{row['status']} | {zero_text} | {row['decision']} |"
    )


def row_to_compact(row: dict[str, Any]) -> str:
    """Render one audit row without repeated rule/direct columns."""
    source = row['source']
    if source is None:
        source_text = 'None'
    else:
        source_text = f"{source['file']}:IFD{source['ifd']}"
    direct = row['direct_expected']
    direct_text = '—' if direct is None else str(direct['first_row'])
    return (
        f"| {row['layer']} | {source_text} | {direct_text} | "
        f"{row['preview_first_row']} | {row['status']} | "
        f"{row['zero_class'] or '—'} |"
    )


AUDIT_HEADER = (
    '| 层 | 交件预期 | 实际页来源 | 预览解码规则(keyframe) | '
    '原件直读首行 | 预览首行 | 状态 | 零值判别 | 入库结论 |\n'
    '|---|---|---|---|---|---|---|---|---|'
)

COMPACT_HEADER = (
    '| 层 | 实际页来源 | 原件直读首行 | 预览首行 | 状态 | 零值判别 |\n'
    '|---|---|---|---|---|---|'
)


def render_rule_checks(audit: dict[str, Any]) -> str:
    """Render pixel-rule cross-checks for suspicious layers."""
    rows = [
        row
        for row in audit['rows']
        if row['status'] == 'REF_OK_PIXELS_WRONG'
        and row.get('rule_check') is not None
    ]
    if not rows:
        return ''
    lines = [
        '',
        '#### 像素规则核对（来源正确、像素错误）',
        '',
        '| 层 | 源 IFD 自身 predictor | 源 IFD 规则下首行 | 预览所用 keyframe predictor | 该规则下首行 | 预览实际首行 | 判定 |',
        '|---|---|---|---|---|---|---|',
    ]
    for row in rows:
        check = row['rule_check']
        lines.append(
            f"| {row['layer']} | {check['source_ifd_predictor']} | "
            f"{check['source_own_rule_first_row']} | "
            f"{check['preview_keyframe_predictor']} | "
            f"{check['preview_rule_first_row']} | "
            f"{check['preview_first_row']} | "
            f"预览等于自身规则={check['preview_equals_own_rule']}; "
            f"预览等于 keyframe 规则="
            f"{check['preview_equals_preview_rule']} |"
        )
    lines.append('')
    lines.append(
        '核对逻辑：源 IFD 被按 `preview_keyframe_predictor` 解释时，'
        '首行恰好等于预览；而按它自己的 predictor 解释时等于原件直读。'
        '这就把“读对文件、读错规则”钉在解码规则，而不是字节或引用。'
    )
    lines.append('')
    return '\n'.join(lines)


def render_audit(audit: dict[str, Any], title: str) -> str:
    """Render one audit as Markdown."""
    lines = [f'### {title}', '']
    if audit.get('description'):
        lines.append(audit['description'])
        lines.append('')
    lines.append(AUDIT_HEADER)
    for row in audit['rows']:
        lines.append(row_to_markdown(row))
    lines.append('')
    rule_checks = render_rule_checks(audit)
    if rule_checks:
        lines.append(rule_checks)
    return '\n'.join(lines)


def render_scenarios(result: dict[str, Any]) -> str:
    """Render all five reproduction sequences."""
    lines = [
        f"# 同一交件的五次预览与页检查：{result['case']}",
        '',
        '所有读数都来自当前仓库的 tifffile 读取路径。'
        '“原件直读”是用新的 TiffFile 直接取某个文件的一个 IFD，'
        '不经过 OME series 的跨页 keyframe。',
        '',
    ]
    for scenario in result['scenarios']:
        lines.extend(
            render_audit(
                scenario['audit'],
                f"{scenario['name']} — {scenario['title']}",
            ).splitlines()
        )
        lines.append('')
        for note in scenario.get('notes', []):
            lines.append(f'- {note}')
        if 'before' in scenario:
            lines.append('')
            lines.append('补件前同实例读数：')
            lines.append(COMPACT_HEADER)
            lines.extend(
                row_to_compact(row) for row in scenario['before']['rows']
            )
        if 'page_entry_first_row' in scenario:
            lines.append('')
            lines.append(
                '页入口（cache=True）读取 M IFD2 首行：'
                f"{scenario['page_entry_first_row']}"
            )
        if 'page_check_frame_first_row' in scenario:
            lines.append('')
            lines.append(
                '预览后 pages[2]（缓存的 TiffFrame）首行：'
                f"{scenario['page_check_frame_first_row']}"
            )
            lines.append(
                '同一页 page.aspage() 首行：'
                f"{scenario['page_check_aspage_first_row']}"
            )
            lines.append('')
            lines.append('页检查之后再次整组预览：')
            lines.append(COMPACT_HEADER)
            lines.extend(
                row_to_compact(row)
                for row in scenario['audit_after_page_check']['rows']
            )
        lines.append('')
    return '\n'.join(lines) + '\n'


def render_controls(results: list[dict[str, Any]]) -> str:
    """Render one-condition controls and their decisive readings."""
    changes = {
        'base': '无（基线）',
        'control_ref_order': '只把 B 的 TiffData 引用移到 M 之前',
        'control_swap_pages': '只把 Z1/Z2 指向的 M IFD 交换为 2/1',
        'control_swap_predictor': '只交换 M IFD1/IFD2 的差分布局',
        'control_pixels': '只改 M IFD1/IFD2 的行内像素',
    }
    lines = [
        '# 单条件对照矩阵',
        '',
        '每个对照都只改变一个条件；其余文件布局、四层尺寸、Deflate 条带、'
        'uint8 和零值页保持不变。判断规则：来源匹配但预览与原件直读不符，'
        '即 `REF_OK_PIXELS_WRONG`。',
        '',
        '| 案例 | 唯一变化 | Z1 来源/预览/状态 | Z2 来源/预览/状态 |',
        '|---|---|---|---|',
    ]
    for result in results:
        rows = {row['layer']: row for row in result['rows']}
        one = rows['Z1']
        two = rows['Z2']

        def cell(row: dict[str, Any]) -> str:
            source = row['source']
            source_text = 'None' if source is None else (
                f"{source['file']}:IFD{source['ifd']} via "
                f"{source['keyframe_file']}:IFD{source['keyframe_ifd']}"
            )
            return (
                f"{source_text}; {row['preview_first_row']}; "
                f"{row['status']}"
            )

        lines.append(
            f"| {result['case']} | "
            f"{changes.get(result['case'], '')} | "
            f"{cell(one)} | {cell(two)} |"
        )
    lines.extend(
        [
            '',
            '`base` 与四个 `control_*` 的原始条件保存在各自的 '
            '`manifest.json` 中。`verify` 会对 Z1/Z2 的状态、来源和首行'
            '做断言，任一不符即以非零状态退出。',
            '',
        ]
    )
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# verification


def verify_controls(cases_root: Path) -> list[dict[str, Any]]:
    """Audit controls and assert the single-condition expectations."""
    expectations = {
        'base': {
            'Z1': ('OK', [10, 13, 17, 22]),
            'Z2': ('REF_OK_PIXELS_WRONG', [20, 44, 73, 108]),
        },
        'control_ref_order': {
            'Z0': ('SOURCE_MISMATCH', [0, 0, 0, 0]),
            'Z1': ('REF_OK_PIXELS_WRONG', [10, 3, 4, 5]),
            'Z2': ('OK', [20, 24, 29, 35]),
        },
        'control_swap_pages': {
            'Z1': ('OK', [20, 24, 29, 35]),
            'Z2': ('REF_OK_PIXELS_WRONG', [10, 3, 4, 5]),
        },
        'control_swap_predictor': {
            'Z1': ('OK', [10, 13, 17, 22]),
            'Z2': ('REF_OK_PIXELS_WRONG', [20, 4, 5, 6]),
        },
        'control_pixels': {
            'Z1': ('OK', [200, 30, 40, 50]),
            'Z2': ('REF_OK_PIXELS_WRONG', [90, 95, 102, 111]),
        },
    }
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for name, layer_expectations in expectations.items():
        case_dir = cases_root / name
        result = audit_case(case_dir)
        results.append(result)
        rows = {row['layer']: row for row in result['rows']}
        for layer, (status, first_row) in layer_expectations.items():
            row = rows[layer]
            if row['status'] != status:
                failures.append(
                    f'{name} {layer}: status {row["status"]} != {status}'
                )
            if row['preview_first_row'] != first_row:
                failures.append(
                    f'{name} {layer}: first row '
                    f'{row["preview_first_row"]} != {first_row}'
                )
    if failures:
        raise AssertionError('; '.join(failures))
    return results


# ---------------------------------------------------------------------------
# command line


def command_build(arguments: argparse.Namespace) -> None:
    paths = build_cases(Path(arguments.out))
    for path in paths:
        print(path)


def json_safe(value: Any) -> Any:
    """Convert audit values to JSON-compatible objects."""
    if isinstance(value, numpy.ndarray):
        return value.tolist()
    if isinstance(value, numpy.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return value


def donor_row(
    case_dir: Path,
    *,
    layer: str,
    file: str,
    ifd: int,
    direct_cache: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    """Return a comparison row for the page that supplied a decode rule."""
    direct = read_page_direct(case_dir / file, ifd, direct_cache)
    return {
        'layer': layer,
        'expected': {'file': file, 'ifd': ifd},
        'source': {
            'file': direct['file'],
            'ifd': direct['ifd'],
            'offset': direct['offset'],
            'kind': direct['kind'],
            'keyframe_file': direct['file'],
            'keyframe_ifd': direct['ifd'],
            'keyframe_offset': direct['offset'],
            'keyframe_predictor': direct['predictor'],
        },
        'source_matches_expected': True,
        'status': 'COMPARISON',
        'decision': '对照页：只用于解释可疑层的解码规则',
        'zero': False,
        'zero_class': None,
        'zero_probe_first_row': None,
        'zero_probe_changed': False,
        'preview_first_row': direct['first_row'],
        'direct_expected': direct,
        'direct_actual': direct,
        'keyframe_direct': direct,
        'pixel_match_expected': True,
        'pixel_match_actual': True,
        'rule_check': None,
        'direct_error': None,
    }


def command_audit(arguments: argparse.Namespace) -> None:
    case_dir = Path(arguments.case)
    layers = None
    if arguments.layers:
        layers = {
            layer.strip().upper() for layer in arguments.layers.split(',')
        }
    manifest = load_manifest(case_dir)
    direct_cache: dict[tuple[str, int], dict[str, Any]] = {}
    expected_layers = sorted(
        manifest['expected'], key=lambda name: int(name[1:])
    )
    selected = (
        set(expected_layers) if layers is None else layers & set(expected_layers)
    )
    with tifffile.TiffFile(case_dir / manifest['master']) as tif:
        series = tif.series[0]
        result = audit_open_tif(
            tif,
            case_dir,
            manifest,
            direct_cache=direct_cache,
            layers=None if layers is None else selected,
        )
    rows = result['rows']
    if layers is not None:
        donors: list[dict[str, Any]] = []
        present = {
            (row['source']['file'], row['source']['ifd'])
            for row in rows
            if row['source'] is not None
        }
        for row in list(rows):
            source = row['source']
            if source is None:
                continue
            key = (source['keyframe_file'], source['keyframe_ifd'])
            if key in present:
                continue
            present.add(key)
            donors.append(
                donor_row(
                    case_dir,
                    layer=f"Rule of {row['layer']}",
                    file=key[0],
                    ifd=key[1],
                    direct_cache=direct_cache,
                )
            )
        rows.extend(donors)
        order = {
            layer: index for index, layer in enumerate(expected_layers)
        }
        rows.sort(
            key=lambda row: (
                order.get(row['layer'], len(order)),
                row['layer'],
            )
        )
    result['rows'] = rows
    text = render_audit(
        result, f'{result["case"]} — 整组预览审计'
    )
    if arguments.json:
        text = json.dumps(json_safe(result), ensure_ascii=False, indent=2)
    if arguments.output:
        Path(arguments.output).write_text(text + '\n')
        print(arguments.output)
    else:
        print(text)


def command_scenarios(arguments: argparse.Namespace) -> None:
    result = reproduce_scenarios(Path(arguments.case))
    text = render_scenarios(result)
    if arguments.output:
        Path(arguments.output).write_text(text)
        print(arguments.output)
    else:
        print(text)


def command_verify(arguments: argparse.Namespace) -> None:
    results = verify_controls(Path(arguments.cases))
    text = render_controls(results)
    if arguments.output:
        Path(arguments.output).write_text(text)
        print(arguments.output)
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    build_parser = subparsers.add_parser(
        'build', help='build the baseline and one-condition fixtures'
    )
    build_parser.add_argument(
        '--out', default='forensics/cases', help='output directory'
    )
    build_parser.set_defaults(function=command_build)

    audit_parser = subparsers.add_parser(
        'audit', help='audit one case read-only'
    )
    audit_parser.add_argument('--case', required=True, help='case directory')
    audit_parser.add_argument(
        '--layers',
        default=None,
        help='comma-separated layers, for example Z2',
    )
    audit_parser.add_argument(
        '--json', action='store_true', help='emit JSON'
    )
    audit_parser.add_argument(
        '--output', default=None, help='write report to file'
    )
    audit_parser.set_defaults(function=command_audit)

    scenarios_parser = subparsers.add_parser(
        'scenarios', help='reproduce the five operational sequences'
    )
    scenarios_parser.add_argument(
        '--case', required=True, help='case directory'
    )
    scenarios_parser.add_argument(
        '--output', default=None, help='write report to file'
    )
    scenarios_parser.set_defaults(function=command_scenarios)

    verify_parser = subparsers.add_parser(
        'verify', help='assert one-condition control expectations'
    )
    verify_parser.add_argument(
        '--cases', default='forensics/cases', help='cases root directory'
    )
    verify_parser.add_argument(
        '--output', default=None, help='write report to file'
    )
    verify_parser.set_defaults(function=command_verify)

    arguments = parser.parse_args()
    arguments.function(arguments)


if __name__ == '__main__':
    main()
