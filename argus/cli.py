"""Argus command-line entry point."""

import argparse
from argus.config import init_config
from argus.logger import set_level


def build_parser():
    from argus.commands import background, device, figma, probes, run, targets, toolchain, task, doctor
    from argus.runtime import cli as workflow

    parser = argparse.ArgumentParser(description="Argus — LLM QA Agent")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init", help="Create default .env config file")
    init.set_defaults(handler=lambda args: init_config())
    for commands in (targets, run, toolchain, device, probes, background, figma, workflow, task, doctor):
        commands.register(sub)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        set_level("DEBUG")
    if args.command == "workflow":
        from argus.runtime.cli import dispatch
        dispatch(args)
    elif hasattr(args, "handler"):
        args.handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
