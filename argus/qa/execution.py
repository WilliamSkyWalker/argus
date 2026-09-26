"""Shared case preparation and sequential, device-queue and Grid execution."""

import os
import time
from pathlib import Path

from argus.logger import get_logger
from argus.qa.agent import Agent
from argus.qa.cases import (
    _apply_account_placeholders,
    _extract_reset_mode,
    _extract_target_url,
    _should_skip_by_automation,
    _should_skip_by_platform,
    _should_skip_by_probes,
    _substitute_placeholders,
)
from argus.qa.device_setup import _check_or_reconnect_device, _reset_android_state


log = get_logger("qa.execution")


def _skip_reason(case, platform):
    return (_should_skip_by_platform(case, platform)
            or _should_skip_by_automation(case)
            or _should_skip_by_probes(case))


def _empty_result(status, reason, **metadata):
    return {"result": status, "reason": reason, "steps": 0,
            "duration": 0, "steps_detail": [], **metadata}


def _prepare_browser_case(platform, case, url):
    target = url or _extract_target_url(case)
    if not target:
        return
    try:
        driver = getattr(platform, "_driver", None)
        if driver is not None:
            driver.delete_all_cookies()
            driver.execute_script(
                "try { window.localStorage.clear(); "
                "window.sessionStorage.clear(); } catch (e) {}"
            )
    except Exception as exc:
        log.debug("Browser state cleanup skipped: %s", exc)
    platform.open_target(target)
    time.sleep(3)


def _prepare_android_case(platform, case):
    mode = _extract_reset_mode(case)
    if mode and mode != "none":
        _reset_android_state(platform, mode)
    elif getattr(platform, "platform_name", "") == "android":
        _reset_android_state(platform, "none")


def _maybe_heal(agent, case_text: str, result: dict) -> None:
    """Case fail/timeout/error 后跑一次 Healer 根因分析，结果挂到
    result["heal_report"]（report.py 渲染「Healer 根因分析」块用）。

    Healer 是锦上添花：自身 LLM 调用失败绝不能影响跑测主流程，只记 warning。"""
    if result.get("result") not in ("fail", "timeout", "error"):
        return
    try:
        from argus.qa.healer import analyze_failure
        steps_detail = result.get("steps_detail") or []
        # 取最后一张有截图的 step 截图给 healer 看
        last_png = None
        for s in reversed(steps_detail):
            if s.get("screenshot_png"):
                last_png = s["screenshot_png"]
                break
        heal = analyze_failure(
            test_case=case_text,
            scenario_steps=result.get("scenario_steps") or [],
            step_status=result.get("step_status") or {},
            steps_detail=steps_detail,
            last_screenshot_png=last_png,
            llm_client=agent.brain.client,
            model=agent.brain.model,
        )
        if not heal.is_empty:
            result["heal_report"] = heal.to_dict()
    except Exception as e:
        log.warning("Healer 分析失败（不影响测试结果）: %s", e)


def _probe_run_context(target_dir: Path | None, account: dict | None) -> dict:
    """跑测级上下文，塞给 Agent.probe_context 供非视觉断言插件框数据用。

    probe 要能把「本次跑测产生的数据」从历史里挑出来 —— 时间窗 agent 自己有，
    身份锚点（哪个账号 / 哪个 target / 哪次 run）只有这里知道。
    """
    return {
        "run_id": os.environ.get("ARGUS_RUN_ID", ""),
        "target": target_dir.name if target_dir else "",
        "account": dict(account or {}),
    }


def _run_sequential(cfg: dict, test_cases: list[str], url: str | None,
                    account: dict | None = None,
                    target_dir: Path | None = None) -> list[dict]:
    """Run test cases sequentially with a single Agent.

    If ``account`` provided, every case has its ${EMAIL}/${PASSWORD}/...
    placeholders substituted before being sent to Agent.run().
    """
    log.info("创建 Agent...")
    agent = Agent(config=cfg)
    agent.probe_context = _probe_run_context(target_dir, account)
    log.info("Agent 创建完成")

    results = []
    current_platform = cfg.get("platform", "")
    for i, raw_tc in enumerate(test_cases):
        tc = _apply_account_placeholders(raw_tc, account) if account else raw_tc
        # Platform tag mismatch → skip this case before running pm_clear etc
        skip_reason = _skip_reason(tc, current_platform)
        if skip_reason:
            log.info("[%d/%d] SKIP: %s", i + 1, len(test_cases), skip_reason)
            results.append(_empty_result('skipped', skip_reason, case=tc))
            continue

        _prepare_browser_case(agent.platform, tc, url)

        # Android per-case state reset via `**Reset before**: <mode>` directive.
        _prepare_android_case(agent.platform, tc)

        tc = _substitute_placeholders(tc)

        log.info("[%d/%d] 开始执行: %s", i + 1, len(test_cases), tc[:60])
        result = agent.run(tc)
        log.info("[%d/%d] 结果: %s", i + 1, len(test_cases), result.get("result", "?"))
        # fail/timeout/error → Healer 根因分析（失败不影响主流程）
        _maybe_heal(agent, tc, result)
        results.append({"case": tc, **result})

    return results


def _run_dispatched_devices(cfg: dict, test_cases: list[str],
                            devices: list[str], accounts: list[dict],
                            url: str | None,
                            target_dir: Path | None = None) -> list[dict]:
    """N-Android-device 动态调度器。

    一个共享 case 队列 + N 个 worker thread，每个 worker：
      * 自己的 Agent 实例（绑定一台 serial）
      * 自己的账号（accounts[i]）
      * 谁先空闲谁拉下一个 case

    跟静态预分片 (cases[start:end]) 比，动态调度自动负载均衡：某台设
    备在难 case 上卡 N 分钟时，其他设备照常拉下一个 case 跑，不会被
    最慢的设备拖死。

    报告聚合按原始 case index 回填，最终顺序跟 test_cases 输入一致。
    """
    import copy
    import threading
    from concurrent.futures import as_completed, ThreadPoolExecutor
    from queue import Empty, Queue

    n = len(devices)

    # Per-device cfg：复制顶层 cfg 后改 serial（避免 share 同一 dict 引起 race）
    per_device_cfg: list[dict] = []
    for serial in devices:
        c = copy.deepcopy(cfg)
        c.setdefault("android", {})["serial"] = serial
        c.setdefault("appium", {})["device"] = serial   # 移动端统一 Appium，按 worker 绑设备
        per_device_cfg.append(c)

    # Per-device Agent 实例 — 单台失败时降级，其他设备继续。
    # 容错动机：3 台并发跑大批 case 时，WiFi 设备掉线 / 某台 adb 卡死 / 某台
    # uiautomator2 推 apk 失败 都不该让整批 abort。残存 N-1 台仍能跑完，
    # 失败的设备/账号记 log，最终报告里能看到「跑了 X 台、Y 台 fail-to-start」。
    log.info("调度: 创建 %d 个 Agent (每台设备一个)...", n)
    agents: list[Agent] = []
    alive_devices: list[str] = []
    alive_accounts: list[dict] = []
    failed_devices: list[tuple[str, str]] = []
    for i, c in enumerate(per_device_cfg):
        serial = devices[i]
        acct = accounts[i] if i < len(accounts) else (accounts[0] if accounts else {})
        log_safe = {k: v for k, v in acct.items()
                    if "pass" not in k.lower() and "secret" not in k.lower()}
        log.info("  Agent #%d device=%s account=%s", i + 1, serial, log_safe)
        try:
            _agent = Agent(config=c)
            # 每台设备绑自己的账号 —— probe 按它区分同一时间窗里的多台设备数据
            _agent.probe_context = _probe_run_context(target_dir, acct)
            agents.append(_agent)
            alive_devices.append(serial)
            alive_accounts.append(acct)
        except Exception as e:
            log.error("  Agent #%d (device=%s) 启动失败，跳过该设备: %s",
                      i + 1, serial, e)
            failed_devices.append((serial, str(e)))

    if not agents:
        raise RuntimeError(
            f"全部 {n} 台设备 Agent 启动均失败，无法运行调度器: {failed_devices}"
        )

    if failed_devices:
        log.warning("⚠️ %d/%d 台设备启动失败，仅用剩余 %d 台跑 — 失败列表: %s",
                    len(failed_devices), n, len(agents),
                    [f[0] for f in failed_devices])

    # 用幸存的 device / account 列表覆盖 — 后续 worker 函数引用的是这些
    n = len(agents)
    devices = alive_devices
    accounts = alive_accounts
    log.info("%d 个 Agent ready (跳过 %d 台)", n, len(failed_devices))

    # 共享 case 队列：(原始 index, raw_case_text)
    queue: "Queue[tuple[int, str]]" = Queue()
    for i, tc in enumerate(test_cases):
        queue.put((i, tc))

    total = len(test_cases)
    results: list[dict | None] = [None] * total
    results_lock = threading.Lock()
    counter = [0]
    counter_lock = threading.Lock()
    # 「正在处理 case」的 worker 数：离线 worker 会把 case 放回队列（requeue）。
    # 队列瞬时为空时若其他 worker 直接退出，晚到的 requeue 就成了孤儿 case。
    # 队列空 + 无人在忙（不可能再 requeue）才允许 worker 退出。
    busy_workers = [0]
    busy_lock = threading.Lock()
    current_platform = cfg.get("platform", "")

    def worker(worker_idx: int):
        # 在 worker thread 入口给 logger 设 worker 标签，让 log 行能区分是哪个
        # worker 打的。case 开始时再细化为 W{idx}/c{N}。
        from argus.logger import set_case_context
        set_case_context(f"W{worker_idx}")

        agent = agents[worker_idx]
        device = devices[worker_idx]
        # 账号池小于设备数时尾部设备 fallback 用 accounts[0]
        account = (accounts[worker_idx] if worker_idx < len(accounts)
                   else (accounts[0] if accounts else None))
        acct_email = account.get("email", "(no-account)") if account else "(no-account)"
        log.info("[Worker %d device=%s account=%s] start", worker_idx, device, acct_email)

        # 设备健康自愈状态：连续 N 次 health check 失败该 worker 退出，
        # 把剩余 case 让其他 worker 抢走。避免一台设备挂了拖死一摞 case。
        consecutive_offline = 0
        MAX_OFFLINE_BEFORE_WORKER_EXIT = 3

        while True:
            try:
                idx, raw_case = queue.get(timeout=2)
            except Empty:
                # 队列空 ≠ 全部干完：还在 case 中的 worker 可能因设备离线把
                # case 放回队列（requeue）。无人在忙才真正退出，否则继续等。
                with busy_lock:
                    no_one_busy = busy_workers[0] == 0
                if no_one_busy:
                    break
                continue

            with busy_lock:
                busy_workers[0] += 1
            try:
                # 进入该 case，更新 logger context — 后续所有 log 行都带 [W{idx}/c{N}]
                set_case_context(f"W{worker_idx}/c{idx + 1}")

                # 0) Pre-case health check：检查设备 online + 必要时 reconnect
                #    防止 worker 持有 dead platform handle 后续 case 全 instant fail
                if not _check_or_reconnect_device(device, agent):
                    consecutive_offline += 1
                    log.warning(
                        "[Worker %d/%s] case %d pre-check 设备离线 (连续 %d/%d 次)，"
                        "把 case 放回队列让其他 worker 尝试",
                        worker_idx, device, idx, consecutive_offline,
                        MAX_OFFLINE_BEFORE_WORKER_EXIT
                    )
                    queue.put((idx, raw_case))
                    if consecutive_offline >= MAX_OFFLINE_BEFORE_WORKER_EXIT:
                        log.error(
                            "[Worker %d/%s] 连续 %d 次设备离线，worker 退出 — "
                            "剩余 case 由其他 worker 接管",
                            worker_idx, device, MAX_OFFLINE_BEFORE_WORKER_EXIT
                        )
                        return
                    time.sleep(5)
                    continue
                consecutive_offline = 0

                # 1) 替换账号占位符（per-worker，每台设备用各自账号）
                tc = _apply_account_placeholders(raw_case, account) if account else raw_case

                # 2) 平台 / automation tag 过滤
                skip_reason = _skip_reason(tc, current_platform)
                with counter_lock:
                    counter[0] += 1
                    n_done = counter[0]
                if skip_reason:
                    log.info("[%d/%d Worker %d] SKIP: %s", n_done, total, worker_idx, skip_reason)
                    with results_lock:
                        results[idx] = _empty_result('skipped', skip_reason, case=tc, device=device)
                    continue

                # 3) Android per-case state reset
                try:
                    _prepare_android_case(agent.platform, tc)
                except Exception as e:
                    log.warning("[Worker %d] reset 失败: %s", worker_idx, e)

                tc = _substitute_placeholders(tc)

                log.info("[%d/%d Worker %d/%s] 执行: %s",
                         n_done, total, worker_idx, device, tc[:60])
                try:
                    result = agent.run(tc)
                    log.info("[%d/%d Worker %d] 结果: %s",
                             n_done, total, worker_idx, result.get("result", "?"))
                except Exception as e:
                    # exc_info so a framework crash (vs a test fail) leaves a stack
                    # in the log instead of a bare message — a bare "division by
                    # zero" cost a full debug cycle once.
                    log.error("[%d/%d Worker %d] 用例异常: %s",
                              n_done, total, worker_idx, e, exc_info=True)
                    result = _empty_result('error', str(e))

                # fail/timeout/error → Healer 根因分析（失败不影响主流程）
                _maybe_heal(agent, tc, result)

                with results_lock:
                    results[idx] = {"case": tc, **result, "device": device}
            finally:
                # requeue（如有）发生在 try 内、此减一之前，因此任何时刻
                # 「队列空 且 busy==0」⇒ 不会再有新 case，其他 worker 可安全退出
                with busy_lock:
                    busy_workers[0] -= 1

        log.info("[Worker %d device=%s] 队列空，退出", worker_idx, device)

    # 启动 N 个 worker thread
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="argus-dev") as pool:
        futures = [pool.submit(worker, i) for i in range(n)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                log.error("Worker 异常退出: %s", e)

    # Teardown agents
    for ag in agents:
        try:
            ag.platform.teardown()
        except Exception:
            pass

    # 填充任何因异常残留的 None slot
    for i, r in enumerate(results):
        if r is None:
            results[i] = _empty_result('error', 'worker did not produce result', case=test_cases[i])

    return results


def _run_concurrent(cfg: dict, test_cases: list[str], url: str | None,
                    concurrency: int) -> list[dict]:
    """Run test cases concurrently with multiple Agents on Selenium Grid."""
    import copy
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    # Clean up Grid sessions once before creating agents
    grid_url = cfg.get("browser", {}).get("grid_url", "")
    if grid_url:
        from argus.platforms.selenium_grid import cleanup_grid_sessions
        cleanup_grid_sessions(grid_url)

    # Disable per-agent Grid cleanup
    cfg_no_cleanup = copy.deepcopy(cfg)
    cfg_no_cleanup.setdefault("browser", {})["_skip_grid_cleanup"] = True

    log.info("并发模式: 创建 %d 个 Agent...", concurrency)
    agents = []
    for i in range(concurrency):
        log.info("创建 Agent #%d...", i + 1)
        agent = Agent(config=copy.deepcopy(cfg_no_cleanup))
        agents.append(agent)
    log.info("%d 个 Agent 创建完成", len(agents))

    # Thread-safe agent pool
    agent_pool = agents[:]
    pool_lock = threading.Lock()
    total = len(test_cases)
    counter = [0]  # mutable counter for progress
    counter_lock = threading.Lock()

    current_platform = cfg.get("platform", "")

    def run_one(tc: str) -> dict:
        # Platform tag mismatch → skip before acquiring agent
        skip_reason = _skip_reason(tc, current_platform)
        if skip_reason:
            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            log.info("[%d/%d] SKIP: %s", idx, total, skip_reason)
            return _empty_result('skipped', skip_reason, case=tc)
        # Acquire an agent from the pool
        with pool_lock:
            agent = agent_pool.pop()
        try:
            # Reset browser state between scenarios (cookies, storage, URL)
            # so each case starts fresh regardless of which case ran before.
            _prepare_browser_case(agent.platform, tc, url)

            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            log.info("[%d/%d] 开始执行: %s", idx, total, tc[:60])
            result = agent.run(tc)
            log.info("[%d/%d] 结果: %s", idx, total, result.get("result", "?"))
            # fail/timeout/error → Healer 根因分析（失败不影响主流程）
            _maybe_heal(agent, tc, result)
            return {"case": tc, **result}
        finally:
            # Return agent to pool
            with pool_lock:
                agent_pool.append(agent)

    results = [None] * total
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_idx = {
            executor.submit(run_one, tc): i
            for i, tc in enumerate(test_cases)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                log.error("用例执行异常: %s", e)
                results[idx] = _empty_result('error', str(e), case=test_cases[idx])

    # Teardown all agents
    for agent in agents:
        try:
            agent.platform.teardown()
        except Exception:
            pass

    return results
