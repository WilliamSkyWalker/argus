"""Discover and invoke nonvisual assertion probes."""

from saygo.config import load_config
from saygo.qa.cases import TESTS_DIR, _load_accounts


def register(sub):
    # saygo probes <subcommand> — 非视觉断言插件（埋点/后端落库/上报日志）
    probes_p = sub.add_parser(
        "probes", help="Non-visual assertion plugins (analytics events, DB, logs)")
    probes_sub = probes_p.add_subparsers(dest="probes_command", required=True)
    pl = probes_sub.add_parser("list", help="List registered probes (loads each to verify)")
    pl.add_argument("--json", action="store_true")
    pc = probes_sub.add_parser(
        "check", help="Run one probe once — debug it without starting a test run")
    pc.add_argument("name", help="Probe name as registered in .saygo/probes.json")
    pc.add_argument("args", nargs="*",
                    help="k=v pairs, same syntax as the `# saygo-probe:` directive")
    pc.add_argument("--target", default=None,
                    help="tests/<target> — binds accounts[0] from its _accounts.json")
    pc.add_argument("--since-s", type=float, default=300.0,
                    help="Pretend the case started N seconds ago (query time window)")
    pc.add_argument("--wait", action="store_true",
                    help="Keep polling on 'inconclusive' until the timeout budget runs out")
    pc.add_argument("--timeout-s", type=float, default=None,
                    help="Override the probe's timeout budget (only with --wait)")
    pc.add_argument("--json", action="store_true")
    probes_p.set_defaults(handler=cmd_probes)


def cmd_probes(args):
    """`saygo probes list|check` —— 非视觉断言插件的发现 + 单发调试。

    `check` 不起设备、不跑用例，直接按 directive 语法调一次插件，用来把
    「查询写对了没」和「跑测流程」解耦调试。
    """
    import json as _json
    import time as _t

    from saygo.probes import ProbeRunner, build_probe, summarize_data
    from saygo.probes.spec import parse_directive
    from saygo.probes.base import ProbeContext

    cfg = load_config()
    runner = ProbeRunner(cfg)
    reg = runner.registry

    if args.probes_command == "list":
        rows = []
        for name in reg.names():
            entry = reg.entries[name]
            timeout_s, poll_s = runner.limits(name)
            row = {"name": name, "type": entry.kind,
                   "timeout_s": timeout_s, "poll_interval_s": poll_s}
            try:
                build_probe(entry)
                row["status"] = "ok"
            except Exception as e:
                row["status"] = "error"
                row["error"] = f"{type(e).__name__}: {e}"
            rows.append(row)
        if getattr(args, "json", False):
            print(_json.dumps({"config_path": reg.config_path, "probes": rows},
                              ensure_ascii=False, indent=2))
            return
        print(f"注册表: {reg.config_path}")
        if not rows:
            print("  (空 —— 配一个 .saygo/probes.json，可 cp .saygo/probes.json.example)")
            return
        for r in rows:
            mark = "✅" if r["status"] == "ok" else "❌"
            print(f"  {mark} {r['name']:<16s} {r['type']:<11s} "
                  f"预算 {r['timeout_s']:.0f}s / 轮询 {r['poll_interval_s']:.0f}s")
            if r.get("error"):
                print(f"       {r['error']}")
        return

    # ── check ──
    spec = parse_directive(" ".join([args.name] + list(args.args or [])))
    if spec is None:
        print("解析不出 probe 声明，检查参数语法（同 `# saygo-probe:` directive）")
        raise SystemExit(2)
    if not runner.has(spec.name):
        print(runner.missing_reason(spec.name))
        raise SystemExit(2)

    timeout_s, poll_s = runner.limits(spec.name)
    if args.timeout_s is not None:
        timeout_s = float(args.timeout_s)

    target_dir = (TESTS_DIR / args.target) if args.target else None
    accounts = _load_accounts(target_dir) if target_dir else []
    appium = cfg.get("appium") or {}

    # started = 假装 case 是 since_s 秒前开跑的（给 probe 一个查询时间窗）；
    # poll_started = 本次调试轮询的起点（预算 timeout_s 从这里算）
    started = _t.time() - float(args.since_s or 0)
    poll_started = _t.time()
    attempt = 0
    result = None
    while True:
        attempt += 1
        elapsed = _t.time() - poll_started
        ctx = ProbeContext(
            step_index=0, step_text=f"[saygo probes check] {spec.summary()}",
            case_started_at=started, step_started_at=started, now=_t.time(),
            attempt=attempt, elapsed_s=max(0.0, elapsed), timeout_s=timeout_s,
            platform=cfg.get("platform", ""),
            device=str(appium.get("device") or (cfg.get("android") or {}).get("serial") or ""),
            app_package=str(appium.get("package") or appium.get("bundle_id") or ""),
            run_id="probes-check", case_id="probes-check",
            target=str(args.target or ""),
            account=dict(accounts[0]) if accounts else {},
            artifacts_dir="",
        )
        result = runner.check(spec, ctx)
        if result.is_pass or result.is_fail or not args.wait:
            break
        if elapsed >= timeout_s:
            print(f"预算 {timeout_s:.0f}s 耗尽，最后一次仍未定论")
            break
        sleep_s = max(2.0, float(result.retry_after_s or poll_s))
        print(f"  未定论（{result.evidence or result.error}）→ {sleep_s:.0f}s 后重查")
        _t.sleep(sleep_s)

    out = {"probe": spec.name, "args": spec.args, "verdict": result.verdict,
           "evidence": result.evidence, "error": result.error, "attempts": attempt}
    if getattr(args, "json", False):
        out["data"] = result.data
        print(_json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        mark = {"pass": "✅", "fail": "❌"}.get(result.verdict, "⏳")
        print(f"{mark} {spec.summary()} → {result.verdict}")
        if result.evidence:
            print(f"   evidence: {result.evidence}")
        if result.error:
            print(f"   error: {result.error}")
        if result.data:
            print(f"   data: {summarize_data(result.data)[:2000]}")
    raise SystemExit(0 if result.is_pass else 1)
