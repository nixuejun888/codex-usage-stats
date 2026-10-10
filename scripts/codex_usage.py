#!/usr/bin/env python3
"""Summarize local Codex token usage from session rollout logs.

Reads <codex-home>/sessions/**/rollout-*.jsonl and prints an aggregate table.
Standard library only, no network, read-only: it never writes to the Codex home.

Token facts come from `token_usage_record` events (one per model response).
`turn_context` events supply the model per turn, and `session_meta` marks
subagent rollouts. See SKILL.md for the invariants this depends on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deepseek_pricing  # noqa: E402  (sibling module, imported after sys.path fix)

DIMENSIONS = ("day", "thread", "model", "file", "turn")


def default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def local_date(timestamp: str | None) -> str:
    """UTC log timestamp -> local calendar date (the log stores UTC, the user does not)."""
    if not timestamp:
        return "unknown"
    try:
        stamp = timestamp.replace("Z", "+00:00")
        return datetime.fromisoformat(stamp).astimezone().date().isoformat()
    except ValueError:
        return "unknown"


def local_time(timestamp: str | None) -> str:
    """Same conversion, to the minute, for turn-level labels."""
    if not timestamp:
        return "unknown"
    try:
        stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return stamp.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return "unknown"


def empty_bucket() -> dict:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "responses": 0,
        "threads": set(),
    }


def add_usage(bucket: dict, usage: dict) -> None:
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            bucket[key] += value
    bucket["responses"] += 1


def read_thread_titles(codex_home: Path) -> dict:
    titles: dict[str, str] = {}
    index = codex_home / "session_index.jsonl"
    if not index.is_file():
        return titles
    with index.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("id"):
                titles[str(row["id"])] = str(row.get("thread_name") or "")
    return titles


def rollout_files(codex_home: Path, include_archived: bool) -> list[Path]:
    """Active sessions plus, by default, the archived ones Codex moved aside."""
    roots = [codex_home / "sessions"]
    if include_archived:
        roots.append(codex_home / "archived_sessions")
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(sorted(root.rglob("rollout-*.jsonl")))
    return found


def scan(codex_home: Path, since: str | None, until: str | None,
         include_archived: bool = True, pricing: bool = True,
         currency: str = deepseek_pricing.DEFAULT_CURRENCY) -> tuple[dict, dict]:
    """Return (usage rows, scan statistics).

    Each row carries `cost`, priced per response in `currency`, because
    DeepSeek's rate depends on the model and on whether that response ran in a
    peak window.
    """
    rows: list[dict] = []
    seen_responses: set[str] = set()
    stats = {"files": 0, "records": 0, "skipped_duplicates": 0, "skipped_window": 0,
             "unknown_models": set()}

    for path in rollout_files(codex_home, include_archived):
        stats["files"] += 1
        turn_models: dict[str, str] = {}
        meta: dict = {}
        file_rows: list[tuple[dict, dict]] = []

        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"token_usage_record"' in line:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    usage = payload.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    key = str(payload.get("response_id") or "")
                    if key and key in seen_responses:
                        stats["skipped_duplicates"] += 1
                        continue
                    if key:
                        seen_responses.add(key)
                    file_rows.append((event, payload))
                elif '"turn_context"' in line:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    turn_id, model = payload.get("turn_id"), payload.get("model")
                    if turn_id and model:
                        turn_models[str(turn_id)] = str(model)
                elif '"session_meta"' in line and not meta:
                    try:
                        meta = (json.loads(line).get("payload") or {})
                    except json.JSONDecodeError:
                        meta = {}

        source = meta.get("source")
        is_agent = isinstance(source, dict) and bool(source.get("subagent"))
        thread_id = str(meta.get("id") or path.stem)

        for event, payload in file_rows:
            timestamp = event.get("timestamp")
            date = local_date(timestamp)
            if (since and date < since) or (until and date > until):
                stats["skipped_window"] += 1
                continue
            stats["records"] += 1
            model = turn_models.get(str(payload.get("turn_id") or ""), "unknown")
            cost = None
            if pricing:
                cost = deepseek_pricing.cost_of(payload["usage"], model, timestamp,
                                                currency=currency)
                if cost is None and model != "unknown":
                    stats["unknown_models"].add(model)
            rows.append(
                {
                    "date": date,
                    "timestamp": timestamp,
                    "turn_id": str(payload.get("turn_id") or ""),
                    "thread_id": str(payload.get("thread_id") or thread_id),
                    "rollout_id": thread_id,
                    "is_agent": is_agent,
                    "model": model,
                    "usage": payload["usage"],
                    "cost": cost or 0.0,
                }
            )

    return rows, stats


def aggregate(rows: list[dict], dimension: str) -> dict:
    buckets: dict[str, dict] = defaultdict(empty_bucket)
    for row in rows:
        if dimension == "day":
            key = row["date"]
        elif dimension == "thread":
            key = ("[agent] " if row["is_agent"] else "") + row["thread_id"]
        elif dimension == "model":
            key = row["model"]
        elif dimension == "turn":
            # one turn = one user message plus everything the model did for it
            key = f"{local_time(row.get('timestamp'))} {row['thread_id'][:8]}"
        else:
            key = ("[agent] " if row["is_agent"] else "") + row["rollout_id"]
        bucket = buckets[key]
        add_usage(bucket, row["usage"])
        bucket["cost"] += row.get("cost") or 0.0
        bucket["threads"].add(row["thread_id"])
    return buckets


def totals_of(buckets: dict) -> dict:
    total = empty_bucket()
    for bucket in buckets.values():
        for key in total:
            if key == "threads":
                total["threads"] |= bucket["threads"]
            else:
                total[key] += bucket[key]
    return total


def money(tokens: int, per_million: float | None) -> float:
    return (tokens / 1_000_000) * (per_million or 0.0)


def cost_of(bucket: dict, prices: dict) -> float | None:
    if not prices:
        return None
    uncached = bucket["input_tokens"] - bucket["cached_input_tokens"]
    return (
        money(uncached, prices.get("input"))
        + money(bucket["cached_input_tokens"], prices.get("cache"))
        + money(bucket["output_tokens"], prices.get("output"))
    )


def fmt(number: int) -> str:
    return f"{number:,}"


def short(identifier: str, width: int = 22) -> str:
    return identifier if len(identifier) <= width else identifier[: width - 1] + "…"


def force_utf8_output() -> None:
    """Windows consoles default to a legacy code page and mangle CJK thread titles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def render_table(buckets: dict, dimension: str, top: int, titles: dict,
                 cost_label: str | None = None, cost_fn=None,
                 currency: str = deepseek_pricing.DEFAULT_CURRENCY) -> str:
    has_cost = cost_fn is not None
    header = ["Key", "Responses", "Input (new)", "Cache read", "Output", "Reasoning", "Total"]
    if has_cost:
        header.append(cost_label or "Cost")
    body = []
    ordered = sorted(buckets.items(), key=lambda item: item[1]["total_tokens"], reverse=True)
    if top:
        ordered = ordered[:top]
    for key, bucket in ordered:
        label = key
        if dimension == "thread":
            bare = key.replace("[agent] ", "")
            title = titles.get(bare)
            label = short(f"{bare[:8]} {title or ''}".strip(), 48)
        uncached = bucket["input_tokens"] - bucket["cached_input_tokens"]
        row = [
            label,
            fmt(bucket["responses"]),
            fmt(uncached),
            fmt(bucket["cached_input_tokens"]),
            fmt(bucket["output_tokens"]),
            fmt(bucket["reasoning_output_tokens"]),
            fmt(bucket["total_tokens"]),
        ]
        if has_cost:
            cost = cost_fn(bucket) or 0.0
            row.append(deepseek_pricing.money(cost, currency))
        body.append(row)

    total = totals_of(buckets)
    total_row = [
        "TOTAL",
        fmt(total["responses"]),
        fmt(total["input_tokens"] - total["cached_input_tokens"]),
        fmt(total["cached_input_tokens"]),
        fmt(total["output_tokens"]),
        fmt(total["reasoning_output_tokens"]),
        fmt(total["total_tokens"]),
    ]
    if has_cost:
        cost = cost_fn(total) or 0.0
        total_row.append(deepseek_pricing.money(cost, currency))
    body.append(total_row)

    rows = [header] + body
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = ["  ".join(cell.rjust(widths[i]) if i else cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    separator = "-" * len(lines[0])
    return "\n".join([lines[0], separator] + lines[1:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--by", choices=DIMENSIONS, default="day")
    parser.add_argument("--since", help="earliest local date, YYYY-MM-DD")
    parser.add_argument("--until", help="latest local date, YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=0, help="keep only the N largest rows (0 = all)")
    parser.add_argument(
        "--no-archived",
        action="store_true",
        help="ignore <codex-home>/archived_sessions (included by default)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--price-in", type=float, help="your rate per 1M uncached input tokens")
    parser.add_argument("--price-cache", type=float, help="your rate per 1M cached input tokens")
    parser.add_argument("--price-out", type=float, help="your rate per 1M output tokens")
    parser.add_argument("--no-price", action="store_true",
                        help="show tokens only; skip the built-in DeepSeek rate table")
    parser.add_argument("--usd", action="store_true",
                        help="price in DeepSeek's USD list prices instead of CNY")
    args = parser.parse_args(argv)
    force_utf8_output()
    currency = "USD" if args.usd else deepseek_pricing.DEFAULT_CURRENCY

    if not (args.codex_home / "sessions").is_dir():
        print(f"no sessions directory under {args.codex_home}", file=sys.stderr)
        return 2

    prices = {}
    if args.price_in is not None:
        prices["input"] = args.price_in
    if args.price_cache is not None:
        prices["cache"] = args.price_cache
    if args.price_out is not None:
        prices["output"] = args.price_out

    if prices:
        cost_label = f"Cost ({currency}, your rates)"
        cost_fn = lambda bucket: cost_of(bucket, prices)  # noqa: E731
        basis = "flat rates from --price-*"
    elif args.no_price:
        cost_label, cost_fn, basis = None, None, ""
    else:
        cost_label = f"Cost ({currency}, DeepSeek)"
        cost_fn = lambda bucket: bucket.get("cost")  # noqa: E731
        basis = deepseek_pricing.describe(currency)

    rows, stats = scan(args.codex_home, args.since, args.until,
                       include_archived=not args.no_archived,
                       pricing=cost_fn is not None, currency=currency)
    unknown_models = sorted(stats["unknown_models"])
    stats["unknown_models"] = unknown_models
    buckets = aggregate(rows, args.by)
    titles = read_thread_titles(args.codex_home)

    if args.as_json:
        payload = {
            "generated_from": str(args.codex_home / "sessions"),
            "dimension": args.by,
            "coverage": stats,
            "pricing": {"basis": basis or None, "currency": currency, "unit": "per 1M tokens"},
            "rows": [
                {**{k: v for k, v in bucket.items() if k != "threads"}, "threads": len(bucket["threads"]), "key": key}
                for key, bucket in sorted(buckets.items(), key=lambda item: -item[1]["total_tokens"])
            ],
            "total": {**{k: v for k, v in totals_of(buckets).items() if k != "threads"}},
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Codex token usage by {args.by} — source: {args.codex_home / 'sessions'}")
    print(render_table(buckets, args.by, args.top, titles, cost_label, cost_fn, currency))
    print(
        f"\nscanned {stats['files']} rollout files, counted {stats['records']} responses"
        f" ({stats['skipped_duplicates']} duplicate records skipped,"
        f" {stats['skipped_window']} outside the requested window)"
    )
    print("Input (new) excludes cache reads; Reasoning is a subset of Output; Total = Input + Output (as logged).")
    if cost_fn is None:
        print("Price table disabled, so no cost is shown — token counts are the fact.")
    elif prices:
        print(f"Cost uses {basis}: a flat rate per 1M tokens in {currency}, "
              "with no peak/off-peak split.")
    else:
        print(f"Cost uses DeepSeek list prices in {currency} ({basis}). 空闲时段为高峰价的一半；"
              "高峰是北京时间周一至周五 09:00-12:00 与 14:00-18:00（UTC 01:00-04:00、"
              "06:00-10:00，不含法定节假日），每条 response 按自己的时间戳定价，不按日汇总。")
    if unknown_models:
        print(f"No price known for: {', '.join(unknown_models)} — those responses are counted as 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
