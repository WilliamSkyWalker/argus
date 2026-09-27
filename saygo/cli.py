"""Saygo command-line entry point."""

import argparse
from saygo.config import init_config
from saygo.logger import set_level


def build_parser():
    from saygo.commands import background, device, figma, probes, run, targets, toolchain, task, doctor
    from saygo.runtime import cli as workflow

    parser = argparse.ArgumentParser(description="Saygo — LLM QA Agent")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")
    update = sub.add_parser('update', help='Check releases or configure automatic managed runtime updates')
    update.add_argument('options', nargs=argparse.REMAINDER)
    update.set_defaults(handler=lambda args: __import__('saygo.updates', fromlist=['main']).main(args.options))
    init = sub.add_parser("init", help="Create default .env config file")
    init.add_argument('--user', action='store_true', help='Create user config at ~/.saygo/config.env')
    init.set_defaults(handler=lambda args: init_config(user=args.user))
    for commands in (targets, run, toolchain, device, probes, background, figma, workflow, task, doctor):
        commands.register(sub)
    return parser


def main(argv=None):
    import sys
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == 'update':
        from saygo.updates import main as update_main
        return update_main(arguments[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        set_level("DEBUG")
    if args.command == "workflow":
        from saygo.runtime.cli import dispatch
        dispatch(args)
    elif hasattr(args, "handler"):
        args.handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
