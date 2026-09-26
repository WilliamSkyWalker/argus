"""Shared Selenium Grid session cleanup for QA browser startup."""
from ..logger import get_logger

log = get_logger("browser.grid")


def cleanup_grid_sessions(grid_url: str) -> None:
    """Kill all existing sessions on the Grid before starting a new one."""
    import json
    import urllib.request
    try:
        status_url = grid_url.rstrip("/") + "/status"
        with urllib.request.urlopen(status_url, timeout=5) as resp:
            data = json.loads(resp.read())
        nodes = data.get("value", {}).get("nodes", [])
        for node in nodes:
            for slot in node.get("slots", []):
                session = slot.get("session")
                if session:
                    sid = session["sessionId"]
                    log.info("清理残留 session: %s", sid)
                    delete_url = grid_url.rstrip("/") + f"/session/{sid}"
                    req = urllib.request.Request(delete_url, method="DELETE")
                    try:
                        urllib.request.urlopen(req, timeout=5)
                    except Exception:
                        pass
    except Exception as e:
        log.debug("Grid 清理跳过: %s", e)
