class FITSFormatError(Exception):
    """
    Format error in FITS file
    """

    def __init__(self, value):
        super(FITSFormatError, self).__init__(value)
        self.value = value

    def __str__(self):
        return str(self.value)


class ColumnEditError(ValueError):
    """
    One or more table edits could not be stored exactly.

    The ``errors`` attribute contains one dict per rejected candidate with the
    column name, row number, attempted value, candidate stored code and reason.
    No part of a rejected edit group is written.
    """

    def __init__(self, errors):
        self.errors = list(errors)
        super(ColumnEditError, self).__init__(self._message())

    def _message(self):
        parts = []
        for e in self.errors:
            parts.append(
                "column '%s' row %s: %s (value=%r, candidate=%r)"
                % (
                    e.get('column'),
                    e.get('row'),
                    e.get('reason'),
                    e.get('value'),
                    e.get('candidate'),
                )
            )
        return "; ".join(parts)

    def __str__(self):
        return self._message()

