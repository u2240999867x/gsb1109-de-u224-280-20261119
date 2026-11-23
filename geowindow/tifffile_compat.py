"""Thin compatibility wrappers around the bundled tifffile package."""

from __future__ import annotations

from tifffile import TiffFile, TiffPage, TiffWriter  # noqa: F401


def make_subifd_page(tif: TiffFile, offset: int) -> TiffPage:
    """Construct a TiffPage for a SubIFD byte offset."""
    tif.filehandle.seek(offset)
    return TiffPage(tif, (0, 0))
