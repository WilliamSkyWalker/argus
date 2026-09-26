"""MCP backend policy; CLI and Runtime remain independently usable."""
from argus.platforms import device_session as ds


class UnsupportedBackendError(ValueError):
    pass


def check(session=None, backend=None):
    state = ds.load_state(session) or {}
    if backend == 'playwright' or (state.get('kind') == 'browser' and state.get('browser_backend') == 'playwright'):
        raise UnsupportedBackendError(
            'Playwright is not supported through Argus MCP. Connect a browser extension '
            'session instead; existing Playwright sessions remain available through CLI only.')


def check_task(command, task_id, options):
    if command == 'create':
        for session in options.get('bindings', {}).values():
            check(session)
    elif command in {'observe', 'submit', 'recover', 'resume', 'handoff', 'resolve'}:
        from argus.runtime.interactive import default_store
        state = default_store().get(task_id)
        for spec in state['workflow']['resources'].values():
            check(spec.get('session'), spec.get('backend'))
