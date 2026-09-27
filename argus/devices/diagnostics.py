"""Evidence for a decision-making agent; never guesses success or sends input."""


def collect(platform, error=None):
    import time
    from .actions import capabilities
    result = {"at": time.time(), "error": str(error) if error else None,
              "business_success": None,
              "recovery_actions": ["reobserve"],
              "guidance": "Use current evidence to choose one recovery, then reobserve. "
                          "A capture failure alone does not prove the application cannot be captured. "
                          "Never repeat input whose dispatch outcome is uncertain."}
    if platform is None:
        result["diagnostic_error"] = "Controller attachment failed; inspect the binding before reconnecting."
        result["recovery_actions"] = []
        return result
    result["capabilities"] = capabilities(platform)
    if callable(getattr(type(platform), "diagnose", None)):
        try:
            result.update(platform.diagnose())
        except Exception as exc:
            result["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
    return result
