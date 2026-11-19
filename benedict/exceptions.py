class ExtrasRequireModuleNotFoundError(ModuleNotFoundError):
    def __init__(self, *, target: str) -> None:
        message = (
            f"Extras require '[{target}]' module not found, "
            f"you can install it by running: "
            f"'python -m pip install \"python-benedict[{target}]\"'"
        )
        super().__init__(message)


class KeypathMismatchError(KeyError, ValueError):
    """
    Raised when a batch keypath removal manifest no longer matches the data.
    """
