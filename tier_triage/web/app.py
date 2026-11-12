"""Local-only Flask inspection UI. Business actions call the service layer."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from tier_triage.audit.ledger import Ledger
from tier_triage.exec.rollback import RollbackError, rollback_batch


def _human_bytes(value: int | None) -> str:
    amount = int(value or 0)
    if amount >= 1024 * 1024:
        return f"{amount / (1024 * 1024):.1f} MiB"
    if amount >= 1024:
        return f"{amount / 1024:.1f} KiB"
    return f"{amount} B"


def _dashboard(ledger: Ledger) -> dict:
    snapshot = ledger.latest_snapshot()
    plan = ledger.latest_plan()
    batches = ledger.list_batches(12)
    return {"snapshot": snapshot, "plan": plan, "batches": batches}


def create_app(lake_root: str, ledger_path: str) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.urandom(24)
    app.config["LAKE_ROOT"] = str(Path(lake_root).expanduser().resolve())
    app.config["LEDGER_PATH"] = str(Path(ledger_path).expanduser().resolve())
    app.jinja_env.filters["human_bytes"] = _human_bytes
    with Ledger(app.config["LEDGER_PATH"]):
        pass

    @app.before_request
    def restrict_to_loopback() -> None:
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403)

    @app.get("/")
    def index():
        with Ledger(app.config["LEDGER_PATH"]) as ledger:
            data = _dashboard(ledger)
        return render_template("index.html", **data)

    @app.get("/snapshots/<snapshot_id>")
    def snapshot_detail(snapshot_id: str):
        with Ledger(app.config["LEDGER_PATH"]) as ledger:
            snapshot = ledger.get_snapshot(snapshot_id)
            if snapshot is None:
                abort(404)
        return render_template("snapshot.html", snapshot=snapshot,
                               inventory=snapshot["inventory"])

    @app.get("/plans/<plan_id>")
    def plan_detail(plan_id: str):
        with Ledger(app.config["LEDGER_PATH"]) as ledger:
            plan = ledger.get_plan(plan_id)
            if plan is None:
                abort(404)
        return render_template("plan.html", plan=plan)

    @app.get("/batches")
    def batches():
        with Ledger(app.config["LEDGER_PATH"]) as ledger:
            rows = ledger.list_batches(100)
        return render_template("batches.html", batches=rows)

    @app.get("/batches/<batch_id>")
    def batch_detail(batch_id: str):
        with Ledger(app.config["LEDGER_PATH"]) as ledger:
            batch = ledger.get_batch(batch_id)
            if batch is None:
                abort(404)
            actions = ledger.list_actions(batch_id)
            rollbacks = ledger.get_rollbacks(batch_id)
        return render_template("batch.html", batch=batch, actions=actions,
                               rollbacks=rollbacks)

    @app.post("/batches/<batch_id>/rollback")
    def batch_rollback(batch_id: str):
        try:
            result = rollback_batch(
                batch_id, app.config["LAKE_ROOT"], app.config["LEDGER_PATH"],
                actor="web")
            flash(
                f"回滚 {result['status']}：还原 {result['restored_count']} 个，"
                f"不可恢复 {result['unrecoverable_count']} 个。", "ok")
        except RollbackError as exc:
            flash(f"回滚失败：{exc}", "error")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
