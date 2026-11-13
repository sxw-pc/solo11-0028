"""Audit report generation in machine-readable JSON and human Markdown."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tier_triage.audit.ledger import Ledger
from tier_triage.util import write_json

TIERS = ("hot", "warm", "cold", "purge_candidate")


def _distribution(plan: dict[str, Any], evaluation: dict[str, Any] | None) -> dict[str, int]:
    if evaluation and evaluation.get("summary", {}).get("tier_distribution"):
        source = evaluation["summary"]["tier_distribution"]
        return {tier: int(source.get(tier, 0)) for tier in TIERS}
    result = {tier: 0 for tier in TIERS}
    for action in plan.get("actions", []):
        if action["action"] == "mark_purge":
            result["purge_candidate"] += 1
        elif action["action"] == "move":
            result[action["target_layer"]] += 1
        else:
            result[action["current_layer"]] += 1
    return result


def _load_batch(ledger_path: str | Path | None,
                batch_id: str | None) -> tuple[dict[str, Any] | None,
                                                list[dict[str, Any]],
                                                list[dict[str, Any]]]:
    if not ledger_path:
        return None, [], []
    ledger = Ledger(ledger_path)
    try:
        if batch_id:
            batch = ledger.get_batch(batch_id)
        else:
            batches = ledger.list_batches(1)
            batch = batches[0] if batches else None
        if batch is None:
            return None, [], []
        actions = ledger.list_actions(batch["batch_id"])
        rollbacks = ledger.get_rollbacks(batch["batch_id"])
        return batch, actions, rollbacks
    finally:
        ledger.close()


def build_report(evaluation: dict[str, Any], plan: dict[str, Any],
                 ledger_path: str | Path | None = None,
                 batch_id: str | None = None) -> dict[str, Any]:
    plan_actions = plan.get("actions", [])
    batch, actions, rollbacks = _load_batch(ledger_path, batch_id)
    current_layers = {layer: 0 for layer in ("hot", "warm", "cold")}
    for action in plan_actions:
        current_layers[action["current_layer"]] += 1
    blocked = [
        {"partition_id": action["partition_id"],
         "source": action["source"], "reason": action.get("reason", "")}
        for action in plan_actions if action["status"] == "blocked"
    ]
    purge_list = [
        {"partition_id": action["partition_id"], "source": action["source"],
         "bytes": int(action.get("bytes", 0)),
         "file_count": int(action.get("file_count", 0)),
         "reason": action.get("reason", "")}
        for action in plan_actions if action["action"] == "mark_purge"
    ]
    capacity_profile = plan.get("capacity_profile")
    capacity_view: dict[str, Any] | None = None
    if capacity_profile:
        pressure = capacity_profile.get("pressure", {})
        capacity_view = {
            "profile_hash": capacity_profile.get("profile_hash"),
            "oversize_partitions": list(
                capacity_profile.get("oversize_partitions", [])),
            "anomaly_count": capacity_profile.get("anomaly_count", 0),
            "capacity_blocked": pressure.get("capacity_blocked", []),
            "table_budgets": pressure.get("table_budgets", []),
        }
    batch_summary: dict[str, Any] | None = None
    if batch:
        action_status: dict[str, int] = {}
        for action in actions:
            action_status[action["status"]] = action_status.get(action["status"], 0) + 1
        batch_summary = {
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "actor": batch["actor"],
            "started_at": batch["started_at"],
            "finished_at": batch["finished_at"],
            "action_count": batch["action_count"],
            "moved_count": batch["moved_count"],
            "marked_count": batch["marked_count"],
            "error": batch.get("error"),
            "action_status": dict(sorted(action_status.items())),
            "rollbacks": rollbacks,
        }
    return {
        "schema_version": 1,
        "as_of": plan.get("as_of") or evaluation.get("as_of"),
        "plan_id": plan.get("plan_id"),
        "plan_hash": plan.get("plan_hash"),
        "inventory_hash": plan.get("inventory_hash"),
        "policy_fingerprint": plan.get("policy_fingerprint"),
        "tier_distribution": _distribution(plan, evaluation),
        "current_layer_distribution": current_layers,
        "projected_release_bytes": int(
            plan.get("summary", {}).get("projected_release_bytes", 0)),
        "purge_reclaimable_bytes": sum(item["bytes"] for item in purge_list),
        "blocked_items": blocked,
        "purge_list": purge_list,
        "capacity_profile": capacity_view,
        "action_summary": plan.get("summary", {}),
        "batch_summary": batch_summary,
    }


def _bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 冷热分诊台 · 审计报告",
        "",
        f"- 评估时钟（as-of）：`{report['as_of']}`",
        f"- 计划：`{report['plan_id']}`",
        f"- 计划哈希：`{report['plan_hash']}`",
        f"- 策略指纹：`{report['policy_fingerprint']}`",
        f"- 清单哈希：`{report['inventory_hash']}`",
        "",
        "## 分层分布",
        "",
        "| 目标层 | 分区数 |",
        "| --- | ---: |",
    ]
    for tier in TIERS:
        lines.append(f"| {tier} | {report['tier_distribution'].get(tier, 0)} |")
    lines.extend([
        "",
        "## 空间",
        "",
        f"- 预计下沉/清退字节：**{_bytes(report['projected_release_bytes'])}**",
        f"- purge 清退候选（仅标记）：**{_bytes(report['purge_reclaimable_bytes'])}**",
        "",
        "> 本工具不做物理删除；purge 仅进入复核清单。",
        "",
    ])
    capacity = report.get("capacity_profile")
    if capacity:
        lines.extend([
            "## 体积画像与容量压力",
            "",
            f"- 画像哈希：`{capacity['profile_hash']}`；画像异常数："
            f"{capacity['anomaly_count']}",
            f"- 越过 max_hot_bytes 的分区（不得留在 hot）："
            f"{', '.join(f'`{_cell(p)}`' for p in capacity['oversize_partitions']) or '无'}",
        ])
        if capacity["table_budgets"]:
            lines.extend(["", "| 表 | warm 预算 | 已驻留 | 预计占用 | 预算内 |",
                          "| --- | ---: | ---: | ---: | --- |"])
            for row in capacity["table_budgets"]:
                lines.append(
                    f"| `{_cell(row['table'])}` | {row['max_warm_bytes']} | "
                    f"{row['resident_warm_bytes']} | "
                    f"{row['projected_warm_bytes']} | "
                    f"{'是' if row['within_budget'] else '否'} |")
        if capacity["capacity_blocked"]:
            lines.extend(["", "容量阻断（按缓解顺序：大而老优先，小而旧不一刀切）："])
            for row in capacity["capacity_blocked"]:
                lines.append(
                    f"- `{_cell(row['partition_id'])}`（{row['bytes']} B）："
                    f"{_cell(row['reason'])}")
        lines.append("")
    lines.extend(["## 被阻断项", ""])
    if report["blocked_items"]:
        lines.extend(["| 分区 | 原因 |", "| --- | --- |"])
        for item in report["blocked_items"]:
            lines.append(f"| `{_cell(item['partition_id'])}` | {_cell(item['reason'])} |")
    else:
        lines.append("无。")
    lines.extend(["", "## purge 清退清单", ""])
    if report["purge_list"]:
        lines.extend([
            "| 分区 | 文件数 | 字节 | 原因 |",
            "| --- | ---: | ---: | --- |",
        ])
        for item in report["purge_list"]:
            lines.append(
                f"| `{_cell(item['partition_id'])}` | {item['file_count']} | "
                f"{item['bytes']} | {_cell(item['reason'])} |")
    else:
        lines.append("无。")
    lines.extend(["", "## 批次摘要", ""])
    batch = report.get("batch_summary")
    if batch:
        lines.extend([
            f"- 批次：`{batch['batch_id']}`",
            f"- 状态：**{batch['status']}**",
            f"- 动作：{batch['action_count']}（移动 {batch['moved_count']}，"
            f"标记 {batch['marked_count']}）",
            f"- 操作者：`{batch['actor']}`",
            f"- 动作状态：{batch['action_status']}",
        ])
        if batch.get("error"):
            lines.append(f"- 错误：{_cell(batch['error'])}")
        if batch["rollbacks"]:
            lines.append("- 回滚记录：")
            for item in batch["rollbacks"]:
                lines.append(
                    f"  - 还原 {item['restored_count']}，不可恢复 "
                    f"{item['unrecoverable_count']}，时间 {item['created_at']}")
        else:
            lines.append("- 回滚记录：无")
    else:
        lines.append("尚未执行批次。")
    lines.extend([
        "",
        "## 复现说明",
        "",
        "计划哈希覆盖评估时钟、策略指纹、清单哈希与全部动作。"
        "相同输入、策略与时钟应产生同一哈希。",
        "",
    ])
    return "\n".join(lines)


def write_reports(base_path: str | Path, report: dict[str, Any]) -> tuple[Path, Path]:
    base = Path(base_path)
    if base.suffix in {".md", ".json"}:
        base = base.with_suffix("")
    markdown_path = base.with_name(base.name + ".md")
    json_path = base.with_name(base.name + ".json")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    write_json(json_path, report)
    return markdown_path, json_path
