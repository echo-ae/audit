"""Pipeline driver: Recon → (Hunt → Validate → Gapfill)* → Dedupe → Trace
                  → Feedback → (Hunt → Validate → Dedupe → Trace)* → Report
"""

from __future__ import annotations

import logging
from pathlib import Path

from audit import stages
from audit.config import HarnessConfig
from audit.runner import QuotaExhaustedError
from audit.state import StateDB
from audit.stages._common import StageContext

log = logging.getLogger(__name__)


async def run_pipeline(
    *,
    repo_path: Path,
    run_id: str,
    db: StateDB,
    config: HarnessConfig,
    resume: bool = False,
    max_recon_tasks: int | None = None,
    live_target: dict | None = None,
    scope_notes: str | None = None,
) -> Path:
    ctx = StageContext(
        run_id=run_id,
        repo_path=repo_path.resolve(),
        config=config,
        live_target=live_target,
        scope_notes=scope_notes,
    )

    if db.get_run(run_id) is None:
        db.create_run(str(repo_path.resolve()), run_id)
        log.info("[%s] starting fresh pipeline run against %s", run_id, repo_path)
    elif resume:
        # Flip status back to 'running' so subsequent /status calls don't
        # report a stale 'aborted'/'failed' while resume work is ongoing.
        db._conn.execute(  # type: ignore[attr-defined]
            "UPDATE runs SET status = 'running', finished_at = NULL WHERE run_id = ?",
            (run_id,),
        )
        db._conn.commit()  # type: ignore[attr-defined]
        # Re-queue any task left 'running' (interrupted mid-flight by a quota
        # abort or crash) or 'failed' (transient/quota error) so resume
        # actually re-attempts the incomplete work instead of skipping it —
        # Hunt only dispatches 'pending' tasks.
        requeued = db.reset_incomplete_tasks(run_id)
        if requeued:
            log.info("[%s] resume: re-queued %d interrupted/failed tasks", run_id, requeued)
        log.info("[%s] resuming existing run", run_id)
    else:
        raise RuntimeError(
            f"run_id {run_id!r} already exists; pass --resume to continue it."
        )

    try:
        # ---- Stage 1: Recon ----
        recon_kwargs = {} if max_recon_tasks is None else {"max_tasks": max_recon_tasks}
        await stages.run_recon(ctx, db, **recon_kwargs)

        # ---- Stages 2-3-4 loop: Hunt → Validate → Gapfill ----
        for i in range(config.gapfill_iterations + 1):
            log.info("[%s] loop %d: starting hunt", run_id, i)
            findings_added = await stages.run_hunt(ctx, db)
            if findings_added == 0 and i > 0:
                log.info("[%s] no new findings — exiting Hunt/Gapfill loop", run_id)
                break

            log.info("[%s] loop %d: starting validate", run_id, i)
            await stages.run_validate(ctx, db)

            if i >= config.gapfill_iterations:
                break  # final iteration: don't gapfill again
            log.info("[%s] loop %d: starting gapfill", run_id, i)
            new_tasks = await stages.run_gapfill(ctx, db)
            if new_tasks == 0:
                log.info("[%s] gapfill produced 0 tasks — exiting loop", run_id)
                break

        # ---- Stage 5: Dedupe ----
        log.info("[%s] starting dedupe", run_id)
        await stages.run_dedupe(ctx, db)

        # ---- Stage 6: Trace ----
        log.info("[%s] starting trace", run_id)
        await stages.run_trace(ctx, db)

        # ---- Stage 7: Feedback (re-runs Hunt/Validate/Dedupe/Trace) ----
        for i in range(config.feedback_iterations):
            log.info("[%s] feedback loop %d: starting feedback", run_id, i)
            new_tasks = await stages.run_feedback(ctx, db)
            if new_tasks == 0:
                break
            log.info("[%s] feedback loop %d: starting hunt", run_id, i)
            await stages.run_hunt(ctx, db)
            log.info("[%s] feedback loop %d: starting validate", run_id, i)
            await stages.run_validate(ctx, db)
            log.info("[%s] feedback loop %d: starting dedupe", run_id, i)
            await stages.run_dedupe(ctx, db)
            log.info("[%s] feedback loop %d: starting trace", run_id, i)
            await stages.run_trace(ctx, db)

        # ---- Stage 8: Report ----
        log.info("[%s] starting report", run_id)
        report_path = await stages.run_report(ctx, db)

        db.finish_run(run_id, "completed")
        log.info(
            "[%s] pipeline complete: usage records=%d — report at %s",
            run_id, db.usage_record_count(run_id), report_path,
        )
        return report_path

    except QuotaExhaustedError as e:
        # Subscription quota exhausted — surface clearly; user must wait
        # for the reset window. Run is resumable via --resume once quota
        # returns.
        log.error(
            "[%s] subscription quota exhausted — aborting (resumable with --resume): %s",
            run_id, str(e)[:300],
        )
        db.finish_run(run_id, "aborted")
        raise
    except Exception:
        db.finish_run(run_id, "failed")
        raise
