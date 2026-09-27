"""Toolchain setup command registration and dispatch."""


def register(sub):
    # saygo mcp <init|doctor>
    mcp_p = sub.add_parser("mcp", help="Toolchain setup (Appium/node/drivers/iOS WDA)")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)
    mcp_init_p = mcp_sub.add_parser(
        "init", help="One-shot install of the Appium toolchain into ~/.saygo/runtime "
                     "(never touches system node/npm)")
    mcp_init_p.add_argument("--ios-team-id", default=None, metavar="TEAM",
                            help="Apple team id for iOS WDA signing (else .env IOS_TEAM_ID)")
    mcp_init_p.add_argument("--device", default=None, metavar="UDID",
                            help="iOS device udid to pre-build WebDriverAgent for")
    mcp_init_p.add_argument("--force-node", action="store_true",
                            help="Install sandboxed node even if a system node exists")
    mcp_init_p.add_argument("--skip-ios", action="store_true",
                            help="Android only: skip iOS driver + WDA setup")
    mcp_sub.add_parser("doctor", help="Show detected/installed toolchain paths")
    mcp_p.set_defaults(handler=dispatch)


def dispatch(args):
    from saygo.devices import toolchain
    if args.mcp_command == "init":
        toolchain.mcp_init(ios_team_id=args.ios_team_id, device=args.device,
                          force_node=args.force_node, skip_ios=args.skip_ios)
    else:
        toolchain.doctor()
