"""Network observation commands for the persistent browser extension journal."""
from saygo.platforms import device_session as ds


def register(commands):
    parser = commands.add_parser('network', help='Read HTTP, WebSocket and SSE traffic (extension backend)')
    parser.add_argument('network_command', choices=['read', 'start', 'stop', 'clear'], nargs='?', default='read')
    parser.add_argument('--session', '--serial', dest='session', required=True)
    parser.add_argument('--after', type=int, default=0, help='Read events after this cursor')
    parser.add_argument('--capture-id', help='Reject a cursor from a previous capture')
    parser.add_argument('--limit', type=int, default=100, help='Maximum events (1–200)')
    parser.add_argument('--url', default='', help='URL substring filter')
    parser.add_argument('--kind', choices=['http', 'ws', 'sse', 'stream'], default='')


def execute(args):
    state = ds.load_state(args.session)
    if not state or state.get('kind') != 'browser' or state.get('browser_backend') != 'extension':
        raise ValueError('Network observation requires a connected extension browser session')
    if args.after < 0 or not 1 <= args.limit <= 200:
        raise ValueError('--after must be nonnegative and --limit must be between 1 and 200')
    platform = ds.attach_browser(args.session, backend='extension')
    try:
        return platform.network(args.network_command, after=args.after, limit=args.limit,
            url=args.url, kind=args.kind, capture_id=args.capture_id)
    finally:
        ds.release_controller(platform, 'browser')
