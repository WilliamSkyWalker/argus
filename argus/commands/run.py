"""Run command configuration, reporting and execution selection."""

import logging
import os
from pathlib import Path
from datetime import datetime

from argus.commands.background import _launch_background
from argus.config import load_config
from argus.logger import get_logger
from argus.qa.cases import (
    TESTS_DIR,
    _load_accounts,
    _load_preconditions,
    _probes_mode,
    _read_target_url,
    _resolve_test_target,
)
from argus.qa.device_setup import _ensure_devices_connected, _install_apk_on_devices


log = get_logger("commands.run")


def register(sub):
    # argus run <test case file or inline text>
    run_p = sub.add_parser("run", help="Run test case(s)")
    run_p.add_argument("test", help="Test case text or path to .md/.txt file")
    run_p.add_argument("--platform", choices=["ios", "android", "browser", "rdp"], default=None,
                       help="Platform to test on (default: from config; rdp is experimental)")
    run_p.add_argument("--url", default=None,
                       help="Open this URL before running (browser platform)")
    run_p.add_argument("--max-steps", type=int, default=None,
                       help="Override max steps for the test")
    run_p.add_argument("--report", nargs="?", const="__auto__", default=None,
                       metavar="PATH",
                       help="Save report to file (.json or .html). "
                            "Without value: auto-save to tests/<target>/reports/")
    run_p.add_argument("--grid", default=None, metavar="URL",
                       help="Selenium Grid URL (e.g. http://localhost:4444)")
    run_p.add_argument("-j", "--concurrency", type=int, default=1, metavar="N",
                       help="Concurrent sessions (requires Selenium Grid, default: 1)")
    run_p.add_argument("--device", nargs="+", metavar="SERIAL",
                       help="Android device serial(s). Multiple serials run a DYNAMIC "
                            "scheduler: cases enter one shared queue and each device pulls "
                            "the next when idle (slow device on hard case ≠ block others). "
                            "Account from _accounts.json[i] auto-bound to device i.")
    run_p.add_argument("--apk", default=None, metavar="PATH",
                       help="Install this APK on all --device targets before running")
    run_p.add_argument("--shard", default=None, metavar="N/M",
                       help="Manually run only shard N of M of cases (0-based). E.g. "
                            "--shard 0/3 跑前 1/3。与 --device 多设备调度无关，是单进程内"
                            "对 cases 列表的硬切片，便于手动拆批跑。")
    run_p.add_argument("--bg", action="store_true",
                       help="Run in background (headless, silent)")
    # 非视觉断言（probe 插件，见 docs/probes.md）的两种偏置跑法
    probes_grp = run_p.add_mutually_exclusive_group()
    probes_grp.add_argument("--skip-probes", action="store_true",
                            help="Don't run probe steps (analytics etc.) — mark them "
                                 "'skip' and move on. For when the data channel is down "
                                 "or you only care about UI regressions.")
    probes_grp.add_argument("--only-probes", action="store_true",
                            help="Only run cases that declare a probe assertion. UI steps "
                                 "in those cases still run — the events have to be "
                                 "triggered before they can be queried.")
    run_p.set_defaults(handler=dispatch)


def dispatch(args):
    devices = args.device or []
    # 先确保设备 online。TLS/WiFi adb 是会自动断的，每次跑前主动
    # connect 一遍 + 等待最多 20s，能省去手动 adb reconnect 的麻烦。
    if devices:
        devices = _ensure_devices_connected(devices)
    apk = getattr(args, "apk", None)
    if apk and devices:
        # 装不上的设备直接剔除，幸存的继续 — 跟后续 Agent 启动失败的容错思路一致
        devices = _install_apk_on_devices(apk, devices)
    # 手动 --shard 透传给 cmd_run 通过 env
    if args.shard:
        os.environ["ARGUS_SHARD"] = args.shard
    # probe 偏置也走 env：--bg 子进程（env 继承）和多设备 worker（cfg deepcopy
    # 自 load_config）都能拿到，不用在两条路径里各传一遍参数
    if getattr(args, "skip_probes", False):
        os.environ["PROBES_MODE"] = "skip"
    elif getattr(args, "only_probes", False):
        os.environ["PROBES_MODE"] = "only"
    if args.bg:
        # 后台模式：单 daemon process。多设备时 daemon 内部跑调度器
        # （前台 cmd_run(devices=...)），不再 spawn N 个 child。
        primary_serial = devices[0] if devices else None
        _launch_background(args, android_serial=primary_serial,
                           extra_devices=devices[1:] if len(devices) > 1 else None)
    else:
        # 前台：cmd_run 直接收 devices，>1 时走调度器
        if devices and len(devices) == 1:
            os.environ["ANDROID_SERIAL"] = devices[0]
        cmd_run(args.test, platform=args.platform, url=args.url,
                max_steps=args.max_steps, report_path=args.report,
                grid_url=args.grid, concurrency=args.concurrency,
                devices=devices if len(devices) > 1 else None)


def cmd_run(test: str, platform: str | None = None, url: str | None = None,
            max_steps: int | None = None, report_path: str | None = None,
            grid_url: str | None = None, concurrency: int = 1,
            devices: list[str] | None = None):
    from argus.qa.execution import _run_concurrent
    from argus.qa.execution import _run_dispatched_devices
    from argus.qa.execution import _run_sequential

    cfg = load_config()

    if not cfg["llm"]["api_key"]:
        print("Error: API key not configured.")
        print("Run: argus init")
        print("Then edit .env to add your LLM_API_KEY.")
        return

    # CLI overrides
    if platform:
        cfg["platform"] = platform
    if max_steps:
        cfg["agent"]["max_steps"] = max_steps
    if grid_url:
        cfg["browser"]["grid_url"] = grid_url

    # Background mode forces headless
    if os.environ.get("ARGUS_BG_RUN"):
        cfg.setdefault("browser", {})["headless"] = True

    # Resolve test target
    test_cases, target_dir = _resolve_test_target(test)

    # 分片：--shard N/M 让本进程只跑 cases[start:end]，便于多设备并发分摊
    shard_spec = os.environ.get("ARGUS_SHARD", "")  # 兜底：env 也接受
    # 上游 cmd_run() 调用方式两种：args.shard（main）和 env（_launch_background）；
    # 这里统一从 env 取，main() 在派发到 cmd_run 前把 args.shard 写入 env
    if shard_spec:
        try:
            n_str, m_str = shard_spec.split("/", 1)
            shard_idx, shard_total = int(n_str), int(m_str)
            if not (0 <= shard_idx < shard_total):
                raise ValueError(f"shard index {shard_idx} 越界 (total={shard_total})")
            total = len(test_cases)
            # 连续切片（不用步长抽样）：报告里设备 0 跑 case [0:147]、设备 1 跑 [147:294]，
            # 排查时易于追踪。chunk 上取整避免余数 case 漏掉。
            chunk = (total + shard_total - 1) // shard_total
            start = shard_idx * chunk
            end = min(start + chunk, total)
            test_cases = test_cases[start:end]
            log.info("Shard %d/%d: 取 case [%d:%d] / %d 共 %d 个",
                     shard_idx, shard_total, start, end, total, len(test_cases))
        except Exception as e:
            # 解析失败静默跑全集 = 整台设备重复跑一遍 suite，比直接终止更糟
            log.error("--shard 解析失败 (%s): %s", shard_spec, e)
            raise SystemExit(f"--shard 参数无效: {shard_spec} ({e})")

    # 加载 tests/<first-level>/_preconditions.md（如存在）prepend 到每个 case
    # 让 LLM 在 Background fixture 失效时按指南自己恢复，而不是直接 fail
    preconditions = _load_preconditions(target_dir)
    if preconditions:
        test_cases = [f"{preconditions}\n\n{tc}" for tc in test_cases]
        log.info("已加载 _preconditions.md (%d 字符)，prepend 到 %d 个 case",
                 len(preconditions), len(test_cases))

    # 加载账号池。**不在这里做占位符替换** — 把决策推到 runner：
    #   - _run_sequential: 单 device 用 1 个账号（env 指定的 index 或 0）
    #   - _run_dispatched_devices: 每个 worker thread 用各自的账号
    # 这样多设备调度时不同 worker 能拿到不同账号绑定。
    accounts = _load_accounts(target_dir)
    if accounts:
        log.info("已加载账号池：%d 个账号", len(accounts))

    # Default report path: tests/<target>/reports/<timestamp>.html
    if report_path == "" or (report_path is not None and report_path == "auto"):
        report_path = None  # will be set below
    if report_path is None and target_dir:
        # --report flag present without value → auto-generate path
        pass  # only auto-generate when --report is explicitly used
    # 当 --report 走 auto 路径时，记录顶层 reports/ 目录，用于 latest.html 软链
    auto_reports_root: Path | None = None
    # run-log FileHandler / get_logger patch 的清理句柄（跑完后在 finally 里
    # 统一回收 — 否则每次 cmd_run 都往所有 argus logger 上多挂一个 handler，
    # get_logger 也被层层 wrap，长进程（如 MCP server）下持续泄漏）
    run_log_handler: logging.FileHandler | None = None
    orig_get_logger = None
    if report_path == "__auto__" and not target_dir:
        report_path = None  # No target: never treat the auto sentinel as a filename.
    if report_path == "__auto__" and target_dir:
        # Auto report 路径：tests 下第一级目录的 reports/<ts>/ 子目录
        # 示例：my-app/mobile/01-login/foo.feature → tests/my-app/reports/<ts>/foo-<ts>.html
        # 每个 run 单独一个时间戳目录，避免多次跑后 reports/ 根目录散落几十个文件
        # 注意不能用 target_dir/reports/ — 那会落到含 README.md 的祖先目录下，与 cases 混在一起
        log_path = None
        try:
            rel = target_dir.resolve().relative_to(TESTS_DIR.resolve())
        except ValueError:
            rel = None
        if rel and rel.parts:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            auto_reports_root = TESTS_DIR / rel.parts[0] / "reports"
            reports_dir = auto_reports_root / timestamp
            reports_dir.mkdir(parents=True, exist_ok=True)
            test_p = Path(test)
            basename = test_p.stem or "run"
            if test_p.parent.name == "_auto_filter":
                basename = f"{basename}-auto"
            report_path = str(reports_dir / f"{basename}-{timestamp}.html")
            log_path = reports_dir / f"{basename}-{timestamp}.log"
        else:
            # inline 文本或 target_dir 不在 TESTS_DIR 下：不自动落盘，走 stderr
            report_path = None
        # Also attach a file handler so the run log lands next to the HTML report.
        # logger.py sets propagate=False on each argus.* logger to avoid duplicate
        # stderr output, which also means attaching to the `argus` root would
        # collect nothing — we have to walk the existing argus.* loggers.
        if log_path is not None:
            try:
                file_handler = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
                file_handler.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                      datefmt="%H:%M:%S")
                )
                for name, logger_obj in list(logging.Logger.manager.loggerDict.items()):
                    if name.startswith("argus") and isinstance(logger_obj, logging.Logger):
                        logger_obj.addHandler(file_handler)
                # Also patch get_logger so loggers created AFTER this point (e.g.
                # platforms instantiated later) also pick up the handler.
                from argus import logger as _logger_mod
                _orig_get_logger = _logger_mod.get_logger
                def _patched_get_logger(name):
                    logger = _orig_get_logger(name)
                    if file_handler not in logger.handlers:
                        logger.addHandler(file_handler)
                    return logger
                _logger_mod.get_logger = _patched_get_logger
                run_log_handler = file_handler
                orig_get_logger = _orig_get_logger
                log.info("Run log → %s", log_path)
            except Exception as e:
                log.debug("无法附加 file handler: %s", e)

    try:
        # Auto-read URL from target README if not specified via CLI
        if not url and target_dir:
            url = _read_target_url(target_dir)
            if url:
                log.info("从 README.md 读取起始 URL: %s", url)

        log.info("共 %d 个测试用例", len(test_cases))

        # probe 偏置跑法的开场说明（跑完谁也别猜为什么少跑了/为什么全绿）
        _pm = _probes_mode()
        if _pm == "skip":
            log.warning("--skip-probes: 非视觉断言 step 一律跳过不验证"
                        "（每个 case 的 reason 里会点名哪几步没验证）")
        elif _pm == "only":
            from argus.probes import has_probe_directive
            _n_probe = sum(1 for tc in test_cases if has_probe_directive(tc))
            log.info("--only-probes: %d/%d 个 case 声明了 probe 断言，其余全部 skip",
                     _n_probe, len(test_cases))
            if not _n_probe:
                log.warning("没有任何 case 挂了 `# argus-probe:` 声明 —— 本次将全部 skip。"
                            "先确认用例里写了 directive（见 docs/probes.md）")

        if devices and len(devices) > 1:
            # 多 Android 设备：动态调度器，N worker 各持 Agent 抢任务
            log.info("调度模式: %d 台设备", len(devices))
            if accounts and len(accounts) < len(devices):
                log.warning("账号池 %d 个 < 设备 %d 台，最后 %d 台将共用 accounts[0]",
                            len(accounts), len(devices), len(devices) - len(accounts))
            results = _run_dispatched_devices(cfg, test_cases, devices, accounts, url,
                                              target_dir=target_dir)
        elif concurrency > 1:
            # Browser + Selenium Grid：thread pool 多 agent 共抢任务
            results = _run_concurrent(cfg, test_cases, url, concurrency)
        else:
            # 单设备（或单 browser）：顺序跑，用账号池第 0 项（无并发，无需分配）
            single_account = accounts[0] if accounts else None
            if single_account:
                log_safe = {k: v for k, v in single_account.items()
                            if "pass" not in k.lower() and "secret" not in k.lower()}
                log.info("单设备账号 [1/%d]: %s", len(accounts), log_safe)
            results = _run_sequential(cfg, test_cases, url, account=single_account,
                                      target_dir=target_dir)

        # Console summary
        print(f"\n{'='*60}")
        print("测试报告")
        print(f"{'='*60}")
        passed = sum(1 for r in results if r["result"] == "pass")
        skipped = sum(1 for r in results if r["result"] == "skipped")
        total = len(results)
        failed = total - passed - skipped
        total_dur = sum(r.get("duration", 0) for r in results)
        for r in results:
            if r["result"] == "pass":
                status = "PASS"
            elif r["result"] == "skipped":
                status = "SKIP"
            else:
                status = "FAIL"
            dur = f"{r.get('duration', 0):.1f}s"
            print(f"  [{status}] {r['case'][:60]}  ({dur})")
            print(f"        {r.get('reason', '')} ({r['steps']} steps)")
        print(f"\n  总计: {passed}/{total} 通过, {failed} 失败, {skipped} 跳过 | 总耗时: {total_dur:.1f}s")

        # Export report if requested
        if report_path:
            from argus.qa.report import save_html, save_json
            if report_path.endswith(".json"):
                save_json(results, report_path)
            else:
                if not report_path.endswith(".html"):
                    report_path += ".html"
                # 伴生 .json 先落（更便宜的 artifact）：后台 run 的 status/report
                # 工具（CLI & MCP）靠同名 .json 读 summary 和结构化结果，HTML 无法
                # 解析；万一 save_html 渲染异常，至少 JSON 报告已保住整轮结果。
                save_json(results, str(Path(report_path).with_suffix(".json")))
                save_html(results, report_path)
            print(f"\n  报告已保存: {report_path}")

            # Update latest symlink
            # auto 路径下 report 落在 reports/<ts>/ 里，latest.html 要放到顶层 reports/ 才有意义
            # 显式 --report PATH 时仍把 latest.html 放在该路径同级目录
            report_p = Path(report_path)
            if auto_reports_root is not None:
                latest = auto_reports_root / "latest.html"
                target_rel = f"{report_p.parent.name}/{report_p.name}"
            else:
                latest = report_p.parent / "latest.html"
                target_rel = report_p.name
            # 先建唯一命名的临时软链再 os.replace 原子替换 — 并发 run 同时
            # unlink+symlink 会互相踩（TOCTOU）；报告已落盘，软链失败绝不致命
            try:
                tmp_link = latest.parent / f".latest-{os.getpid()}.tmp"
                if tmp_link.is_symlink() or tmp_link.exists():
                    tmp_link.unlink()
                tmp_link.symlink_to(target_rel)
                os.replace(tmp_link, latest)
                print(f"  最新报告:   {latest}")
            except Exception as e:
                log.warning("latest.html 软链更新失败（报告已保存，不影响结果）: %s", e)
    finally:
        # 回收 run-log FileHandler + 还原 get_logger（见上方挂载处注释）
        if run_log_handler is not None:
            try:
                if orig_get_logger is not None:
                    from argus import logger as _logger_mod
                    _logger_mod.get_logger = orig_get_logger
                for name, logger_obj in list(logging.Logger.manager.loggerDict.items()):
                    if (name.startswith("argus")
                            and isinstance(logger_obj, logging.Logger)
                            and run_log_handler in logger_obj.handlers):
                        logger_obj.removeHandler(run_log_handler)
                run_log_handler.close()
            except Exception as e:
                log.debug("run-log handler 清理失败: %s", e)
