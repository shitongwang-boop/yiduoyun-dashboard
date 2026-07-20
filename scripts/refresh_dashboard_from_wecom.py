#!/usr/bin/env python3
"""Refresh the dashboard payload directly from a WeCom Smart Sheet."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
EXCLUDED_STATUSES = {"取消", "已取消"}


def cli_call(action, params):
    command = ["wecom-cli", "doc", action, json.dumps(params, ensure_ascii=False)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"wecom-cli exited with {completed.returncode}")
    try:
        envelope = json.loads(completed.stdout)
        content = envelope["result"]["content"]
        payload = json.loads(next(item["text"] for item in content if item.get("type") == "text"))
    except (KeyError, StopIteration, json.JSONDecodeError) as error:
        raise RuntimeError("无法解析企业微信智能表格响应") from error
    if payload.get("errcode") != 0:
        raise RuntimeError(f"企业微信智能表格读取失败：{payload.get('errmsg', 'unknown error')}")
    return payload


def fetch_records(config):
    records = []
    cursor = None
    while True:
        params = {"url": config["source_url"], "sheet_id": config["sheet_id"], "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = cli_call("smartsheet_get_records", params)
        records.extend(page.get("records", []))
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if not cursor:
            raise RuntimeError("企业微信智能表格返回了不完整的分页游标")
    return records


def display_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    if isinstance(value, list):
        return "、".join(part for part in (display_text(item) for item in value) if part)
    return str(value).strip()


def user_ids(value):
    if not isinstance(value, list):
        return []
    return [str(item["user_id"]) for item in value if isinstance(item, dict) and item.get("user_id")]


def timestamp_text(value, with_time):
    if value in (None, "", "-"):
        return "-"
    try:
        timestamp = int(float(value)) / 1000
        value = datetime.fromtimestamp(timestamp, SHANGHAI)
    except (TypeError, ValueError, OSError):
        return display_text(value) or "-"
    date_part = f"{value.year}年{value.month}月{value.day}日"
    return f"{date_part} {value.hour:02d}:{value.minute:02d}" if with_time else date_part


def embedded_requirements(html_path):
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    match = re.search(r"const REQUIREMENT_DATA = (.*?);\n    // REQUIREMENT_DATA_END", html, re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    result = {}
    for topic in payload.get("topics", {}).values():
        for requirement in topic.get("requirements", []):
            requirement_id = str(requirement.get("id") or "").strip()
            if requirement_id:
                result[requirement_id] = requirement
    return result


def get_requirement_id(values):
    raw = values.get("自动编号") or values.get("需求编号")
    if isinstance(raw, dict):
        return display_text(raw) or str(raw.get("seq") or "")
    return display_text(raw)


def build_user_name_lookup(records, legacy):
    lookup = {}
    for record in records:
        values = record.get("values", {})
        old = legacy.get(get_requirement_id(values))
        if not old:
            continue
        for field, old_field in (("产品主PD", "owner"), ("提出人", "proposer")):
            old_name = str(old.get(old_field) or "").strip()
            ids = user_ids(values.get(field))
            if old_name and old_name != "-" and len(ids) == 1:
                lookup.setdefault(ids[0], old_name)
    return lookup


def user_display(value, fallback, lookup):
    ids = user_ids(value)
    if ids:
        return "、".join(lookup.get(item, f"用户ID:{item}") for item in ids)
    return fallback or "未填写"


def normalize_record(record, source_index, legacy, user_lookup, done_statuses):
    values = record.get("values", {})
    requirement_id = get_requirement_id(values) or f"ROW-{source_index + 1}"
    previous = legacy.get(requirement_id, {})
    status = display_text(values.get("当前状态")) or "未标记"
    priority = display_text(values.get("优先级")) or "-"
    owner = user_display(values.get("产品主PD"), previous.get("owner"), user_lookup)
    proposer = user_display(values.get("提出人"), previous.get("proposer"), user_lookup)
    return {
        "row": previous.get("row") or source_index + 1,
        "id": requirement_id,
        "name": display_text(values.get("需求名称")) or "未命名需求",
        "status": status,
        "done": status in done_statuses,
        "priority": priority,
        "owner": owner,
        "department": display_text(values.get("提出部门")) or "-",
        "proposer": proposer,
        "batch": display_text(values.get("产品方案批次")) or "-",
        "onlineDate": timestamp_text(values.get("上线时间"), False),
        "createdAt": timestamp_text(record.get("create_time"), True),
        "creator": display_text(record.get("creator_name")) or previous.get("creator") or "未填写",
        "specialAttention": display_text(values.get("是否特别关注")),
        "scenario": display_text(values.get("业务场景说明")),
        "topic": display_text(values.get("迭代主题")),
    }


def included(requirement, priority_prefixes):
    return requirement["priority"].startswith(tuple(priority_prefixes)) and requirement["status"] not in EXCLUDED_STATUSES


def build_payload(records, config, html_path):
    fields = cli_call("smartsheet_get_fields", {"url": config["source_url"], "sheet_id": config["sheet_id"]})
    field_names = {field["field_title"] for field in fields.get("fields", [])}
    missing = set(config.get("required_headers", [])) - field_names
    if missing:
        raise RuntimeError(f"业务需求收集表缺少必填字段：{', '.join(sorted(missing))}")
    legacy = embedded_requirements(html_path)
    user_lookup = build_user_name_lookup(records, legacy)
    normalized = [normalize_record(record, index, legacy, user_lookup, set(config["done_statuses"])) for index, record in enumerate(records)]
    topics = {}
    for display_name, source_names in config["topics"].items():
        aliases = set(source_names)
        requirements = [
            requirement for requirement in normalized
            if requirement["topic"] in aliases and included(requirement, config["included_priority_prefixes"])
        ]
        done = sum(requirement["done"] for requirement in requirements)
        total = len(requirements)
        topics[display_name] = {
            "total": total,
            "done": done,
            "rate": round(done * 100 / total) if total else 0,
            "requirements": requirements,
        }
    source_digest = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    timestamp = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M")
    return {
        "meta": {
            "sourceUrl": config["source_url"],
            "sourceFile": "企业微信智能表格·业务需求收集表",
            "sourceSheet": config["sheet_name"],
            "sourceSha256": source_digest,
            "snapshotAt": timestamp,
            "generatedAt": timestamp,
            "completionRule": "当前状态为已上线、已有功能已支持、已完成或问题已解决",
            "priorityRule": f"数据包含优先级 {'、'.join(config['included_priority_prefixes'])}，且排除已取消需求",
            "specialAttentionRule": "是否特别关注为是",
            "unresolvedUserIds": sorted({
                item.split(":", 1)[1]
                for topic in topics.values()
                for requirement in topic["requirements"]
                for item in (requirement["owner"], requirement["proposer"])
                if item.startswith("用户ID:")
            }),
        },
        "topics": topics,
    }


def inject_payload(html_path, payload):
    html = html_path.read_text(encoding="utf-8")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    replacement = "// REQUIREMENT_DATA_START\n    const REQUIREMENT_DATA = " + serialized + ";\n    // REQUIREMENT_DATA_END"
    updated, count = re.subn(r"// REQUIREMENT_DATA_START.*?// REQUIREMENT_DATA_END", lambda _: replacement, html, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError("index.html 中未找到唯一的需求数据注入标记")
    html_path.write_text(updated, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="从企业微信智能表格刷新一朵云驾驶舱")
    parser.add_argument("--config", type=Path, default=Path("config/wecom_yiduoyun_config.json"))
    parser.add_argument("--html", type=Path, default=Path("index.html"))
    parser.add_argument("--snapshot-dir", type=Path, default=Path("work/data"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    records = fetch_records(config)
    if not records:
        raise RuntimeError("业务需求收集表返回 0 条记录，已停止刷新")
    payload = build_payload(records, config, args.html)
    args.snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d_%H%M%S")
    snapshot = args.snapshot_dir / f"wecom_requirements_{stamp}.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    snapshot.write_text(content, encoding="utf-8")
    (args.snapshot_dir / "latest_requirements.json").write_text(content, encoding="utf-8")
    inject_payload(args.html, payload)
    total = sum(topic["total"] for topic in payload["topics"].values())
    done = sum(topic["done"] for topic in payload["topics"].values())
    print(json.dumps({"status": "ok", "records": len(records), "requirements": total, "done": done, "snapshot": str(snapshot)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise
