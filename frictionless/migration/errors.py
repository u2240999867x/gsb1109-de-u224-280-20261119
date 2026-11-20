from __future__ import annotations

from ..error import Error


class RelocationError(Error):
    type = "relocation-error"
    title = "Relocation Error"
    description = "A package relocation cannot be completed."
    template = "The package cannot be relocated: {note}"
