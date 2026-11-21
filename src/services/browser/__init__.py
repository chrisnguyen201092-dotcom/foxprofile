__all__ = ["BrowserLauncher"]


def __getattr__(name: str):
    if name == "BrowserLauncher":
        from .launcher import BrowserLauncher

        return BrowserLauncher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
