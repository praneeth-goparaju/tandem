from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime

from src.brain.runtime.spec import RuntimeContext
from src.db import get_db
from src.orchestrator.executor import run_single_agent
from src.orchestrator.registry import AGENT_REGISTRY, RUN_MODES
from src.orchestrator.runners.base import AgentRunResult

log = logging.getLogger("brain-runner")


async def run_orchestrated(mode: str):
    if mode not in RUN_MODES and mode not in AGENT_REGISTRY:
        print(f"Unknown mode: {mode}. Available: {', '.join(list(RUN_MODES.keys()) + list(AGENT_REGISTRY.keys()))}")
        sys.exit(1)

    start_time = datetime.now()

    if mode in AGENT_REGISTRY and mode not in RUN_MODES:
        result = await run_single_agent(mode)
        _print_summary([result], start_time)
        return

    run_config = RUN_MODES[mode]
    all_results: list[AgentRunResult] = []

    runtime_ctx = _runtime_context(mode, run_config)

    if run_config.get("pre"):
        print(f"\n{'─'*60}")
        print(f"Phase 0: Running {len(run_config['pre'])} pre-parallel agents [claude]...")
        print(f"{'─'*60}")

        for name in run_config["pre"]:
            try:
                result = await run_single_agent(name, runtime_ctx=runtime_ctx)
            except Exception as e:  # noqa: BLE001
                result = {"agent": name, "status": "exception", "error": str(e)}
            all_results.append(result)

    if run_config["parallel"]:
        print(f"\n{'─'*60}")
        print(f"Phase 1: Running {len(run_config['parallel'])} agents in parallel [claude]...")
        print(f"{'─'*60}")

        tasks = [
            run_single_agent(name, runtime_ctx=runtime_ctx)
            for name in run_config["parallel"]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, BaseException):
                all_results.append({"agent": "?", "status": "exception", "error": str(r)})
            else:
                all_results.append(r)

    if run_config["sequential"]:
        print(f"\n{'─'*60}")
        print(f"Phase 2: Running {len(run_config['sequential'])} agents sequentially [claude]...")
        print(f"{'─'*60}")

        for name in run_config["sequential"]:
            try:
                result = await run_single_agent(name, runtime_ctx=runtime_ctx)
            except Exception as e:  # noqa: BLE001
                result = {"agent": name, "status": "exception", "error": str(e)}
            all_results.append(result)

    if runtime_ctx["closing_agent"] or "follow_up" in runtime_ctx["expected_agents"]:
        await _refresh_home()

    _print_summary(all_results, start_time)
    _check_cost_anomaly(mode, all_results, start_time)
    _log_daily_rollup()

    chain_target = run_config.get("chain")
    if chain_target:
        print(f"\n{'─'*60}")
        print(f"Chain → mode '{chain_target}'")
        print(f"{'─'*60}")
        await run_orchestrated(chain_target)


async def _refresh_home() -> None:
    from src.brain.followups.home import refresh_home
    from src.brain.mcp._common import notifier

    await refresh_home(notifier())


def _runtime_context(mode: str, run_config: dict) -> RuntimeContext:
    sequential = list(run_config.get("sequential", []))
    expected_agents = [*run_config.get("pre", []), *run_config.get("parallel", []), *sequential]
    expected_area_owners = [
        a for a in expected_agents
        if AGENT_REGISTRY.get(a, {}).get("kind") == "area_owner"
    ]
    closing_agent = None
    if sequential and AGENT_REGISTRY.get(sequential[-1], {}).get("chat") == "private":
        closing_agent = sequential[-1]
    return {
        "active_mode": mode,
        "expected_agents": expected_agents,
        "expected_area_owners": expected_area_owners,
        "closing_agent": closing_agent,
    }


def _check_cost_anomaly(
    mode: str, results: list[AgentRunResult], cycle_start: datetime
) -> None:
    try:
        agent_names = [r.get("agent") for r in results if r.get("agent")]
        if not agent_names:
            return
        conn = get_db()

        placeholders = ",".join("?" * len(agent_names))
        rows = conn.execute(
            f"SELECT agent, MAX(rowid), cost_usd FROM agent_runs "  # noqa: S608
            f"WHERE agent IN ({placeholders}) GROUP BY agent",
            agent_names,
        ).fetchall()
        cycle_cost = sum((r[2] or 0) for r in rows)
        if cycle_cost <= 0:
            return

        cutoff = cycle_start.strftime("%Y-%m-%dT%H:%M:%S")
        prior = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H', started_at) AS bucket, SUM(cost_usd) "
            "FROM agent_runs WHERE cost_usd > 0 AND started_at < ? "
            "GROUP BY bucket ORDER BY bucket DESC LIMIT 10",
            (cutoff,),
        ).fetchall()
        if len(prior) < 3:
            log.info(f"cycle cost ${cycle_cost:.3f} (insufficient history for anomaly check)")
            return
        avg = sum(p[1] for p in prior) / len(prior)
        ratio = cycle_cost / avg if avg > 0 else 1.0
        if ratio >= 2.0:
            log.warning(
                f"cycle cost anomaly: mode={mode} cost=${cycle_cost:.3f} "
                f"vs rolling avg=${avg:.3f} ({ratio:.1f}× normal)"
            )
        else:
            log.info(f"cycle cost ${cycle_cost:.3f} ({ratio:.1f}× rolling avg)")
    except Exception as e:  # noqa: BLE001
        log.warning("cost anomaly check failed: %s", e)


def _log_daily_rollup() -> None:
    try:
        from src.db import get_db
        today = datetime.now().strftime("%Y-%m-%d")
        conn = get_db()
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS runs, "
            "  COALESCE(SUM(cost_usd),0) AS cost, "
            "  COALESCE(SUM(input_tokens),0) AS in_t, "
            "  COALESCE(SUM(output_tokens),0) AS out_t, "
            "  COALESCE(SUM(cache_read_tokens),0) AS cache_t "
            "FROM agent_runs "
            "WHERE substr(started_at,1,10) = ? "
            "GROUP BY agent ORDER BY cost DESC",
            (today,),
        ).fetchall()
        if not rows:
            return
        day_total = sum(r[2] for r in rows)
        log.info(f"daily_rollup date={today} agents={len(rows)} total=${day_total:.3f}")
        for agent, runs, cost, in_t, out_t, cache_t in rows:
            log.info(
                f"daily_rollup date={today} agent={agent} runs={runs} "
                f"cost=${cost:.4f} in={in_t} out={out_t} cache_read={cache_t}"
            )
    except Exception as e:  # noqa: BLE001
        log.warning("daily rollup failed: %s", e)


def _print_summary(results: list[AgentRunResult], start_time: datetime) -> None:
    elapsed = (datetime.now() - start_time).total_seconds()

    print(f"\n{'='*72}")
    print(f"ORCHESTRATION COMPLETE — {elapsed:.0f}s total [claude]")
    print(f"{'='*72}")

    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_cache_read = 0
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    for r in results:
        if r.get("status") == "skipped":
            print(f"  - {r.get('agent', '?'):<15} skipped")
            continue
        status = "✓" if r.get("status") in ("success", "submitted") else "✗"
        agent = r.get("agent", "?")
        tools = r.get("tools", 0)
        agent_elapsed = r.get("elapsed_s", 0)
        cost = r.get("cost_usd", 0.0) or 0.0
        in_tok = r.get("input_tokens", 0) or 0
        out_tok = r.get("output_tokens", 0) or 0
        cache_r = r.get("cache_read_tokens", 0) or 0
        model = (r.get("model") or "")[:6]
        error = r.get("error", "")

        total_cost += cost
        total_in += in_tok
        total_out += out_tok
        total_cache_read += cache_r

        line = (
            f"  {status} {agent:<15} {model:<6} {agent_elapsed:>5.0f}s "
            f"{tools:>3}t  ${cost:>6.3f}  "
            f"in:{in_tok/1000:>5.1f}k out:{out_tok/1000:>4.1f}k cache:{cache_r/1000:>4.1f}k"
        )
        if error:
            line += f"  ERROR: {error[:50]}"
        print(line)

    success = sum(1 for r in results if r.get("status") in ("success", "submitted"))
    cache_pct = (total_cache_read / max(total_in + total_cache_read, 1)) * 100
    ran = len(results) - skipped
    skipped_note = f" ({skipped} skipped)" if skipped else ""
    print(
        f"\n  {success}/{ran} agents succeeded{skipped_note}  "
        f"total: ${total_cost:.3f}  in:{total_in/1000:.1f}k out:{total_out/1000:.1f}k "
        f"cache-hit:{cache_pct:.0f}%"
    )
    log.info(
        f"cycle summary: agents={ran} skipped={skipped} ok={success} "
        f"cost=${total_cost:.3f} in_tok={total_in} out_tok={total_out} "
        f"cache_read={total_cache_read} cache_hit_pct={cache_pct:.0f}"
    )
    for r in results:
        if r.get("cost_usd", 0):
            log.info(
                f"agent_cost agent={r.get('agent')} model={r.get('model','')} "
                f"cost=${r.get('cost_usd',0):.4f} in={r.get('input_tokens',0)} "
                f"out={r.get('output_tokens',0)} cache_read={r.get('cache_read_tokens',0)} "
                f"elapsed={r.get('elapsed_s',0):.0f}s tools={r.get('tools',0)}"
            )
    print(f"{'='*72}")
