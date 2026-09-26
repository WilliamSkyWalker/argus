"""Compatibility entry point for existing native-host launchers and module CLI."""
if __name__ == "__main__":
    # Installed native-host launchers may execute this file directly, without
    # the repository on sys.path. The actual bridge is also stdlib-only.
    from pathlib import Path
    import runpy

    runpy.run_path(str(Path(__file__).resolve().parent / "integrations" / "browser_bridge.py"),
                   run_name="__main__")
else:
    from .integrations.browser_bridge import main
