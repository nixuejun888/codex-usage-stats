---
name: codex-usage-stats
description: Report this machine's Codex token usage and spend — by day, thread, model, session, or single exchange, in yuan (¥) from DeepSeek's own price list — from local session logs, and run the locked usage widget docked into the Codex client's corner. Use when asked how many tokens, credits, quotas, or 人民币/费用/花了多少钱, which thread or task consumed the most, for a usage report over a date range, or for the usage 挂件/悬浮窗/desktop widget with token counts, cache-hit rate, cost, and live readouts.
---

# Codex Usage Stats

This skill ships inside the `codex-usage-stats` plugin. This file lives at
`<plugin>/skills/codex-usage-stats/SKILL.md`, so the bundled scripts are two
levels up: `<plugin>/scripts/codex_usage.py` and `<plugin>/scripts/usage_widget.py`.
The plugin root is the folder holding `.codex-plugin/plugin.json` — for this
machine `C:\Users\Administrator\plugins\codex-usage-stats`.

Getting the panel on screen: the plugin's SessionStart hook brings it up on the
next session, and anything can start it by hand — `start-widget.cmd` in the
plugin root (double-click), `pythonw hooks/ensure_widget.py`, or
`pythonw hooks/ensure_widget.py --status` to check whether it is already up.
Every path is idempotent: a second panel is refused, never stacked.

Taking it off screen is deliberate, not a click: the panel is **locked**, so
there is no close button, the menu has no quit entry, and Alt+F4/WM_CLOSE is
refused. Use `pythonw hooks/ensure_widget.py --stop` (it signals the panel's
named stop event; the panel exits on its next tick). Only mention `--allow-close`
if you are debugging the panel itself.

Answer usage questions from the local rollout logs, which record every model
response. Run the bundled script; do not read raw logs into context (single
files reach ~90 MB and would cost far more tokens than the answer).

```bash
python scripts/codex_usage.py                     # per local day
python scripts/codex_usage.py --by thread --top 10
python scripts/codex_usage.py --by model --since 2026-10-01
python scripts/codex_usage.py --by turn --since 2026-10-11   # cost per exchange
python scripts/codex_usage.py --json              # machine-readable
python scripts/codex_usage.py --no-price          # tokens only, no cost column
```

`--by` accepts `day`, `thread`, `model`, `file`, or `turn`. A turn row is one
user message plus everything the model did for it, labelled with local time and
the thread, which is the answer to "what did this one exchange cost".
`--codex-home` overrides the default (`$CODEX_HOME`, else `~/.codex`).

## Desktop widget

The plugin's SessionStart hook brings the panel up on its own, but only once the
hooks are trusted; if it is not on screen, start it by hand. Either path is
idempotent — a second panel is refused, never stacked.

```bash
pythonw "<plugin>/hooks/ensure_widget.py"              # same code path as the hook
pythonw "<plugin>/scripts/usage_widget.py"             # foreground, for debugging
"<plugin>/start-widget.cmd"                            # double-clickable
pythonw "<plugin>/hooks/ensure_widget.py" --status     # "running" or "idle"
pythonw "<plugin>/hooks/ensure_widget.py" --stop       # the only way to close it
```

It docks into the bottom-right corner *inside* the Codex client window and shows
today's total, the cache-hit share with a bar, the new/cache/output split, the
session you are in, the heaviest session today, a 7-day chart, and the cost —
today's figure next to the headline, this conversation's figure on the session
line.

- **Pinned to the client.** The panel is an *owned* window of the client's
  HWND: it rides above the app, hides when the app is minimised, and withdraws
  when another application takes focus (`--stay-visible` disables that). Every
  position — dragging, client move, client resize — is clamped to the client's
  content rect, so it cannot be dragged out of the app window.
- **Live numbers.** An append-only tail reads the growing rollout logs every
  `--live` seconds (default 1.0) and adds only records it has not counted; a
  full rescan every `--rescan` seconds (default 60) re-bases it. The headline
  figure moves while a conversation is running, not only after a rescan.
- **Locked on purpose.** No ✕ in the header, no quit entry in the menu, and
  `WM_DELETE_WINDOW` is refused (Alt+F4 included) — a close request flashes the
  footer line `挂件已锁定，不会被关闭` instead. To shut the panel down run
  `pythonw hooks/ensure_widget.py --stop`; to get it back, run
  `ensure_widget.py` or wait for the next session. `--allow-close` restores the
  old closable behaviour and exists only for debugging.
- Drag to move, double-click to rescan, right-click for the menu (立即重算 /
  贴回客户端右下角 / 置顶开关 / 价目说明 / 已锁定：不可关闭).
- Flags: `--client-exe` (process to dock to; default `ChatGPT.exe`, then
  `Codex.exe`; repeatable), `--margin` (gap inside the corner, 14px),
  `--opacity`, `--follow-ms` (client tracking, 250ms), `--no-topmost`,
  `--live`, `--rescan`, `--stay-visible`, `--codex-home`, `--no-price`
  (hide cost), `--usd` (price in dollars instead of yuan).
- `--selftest` prints the geometry plus a drag-clamp check; `--watch N` runs
  headless for N seconds and prints the numbers plus cost whenever they change.

This is a desktop overlay owned by the Codex window; the app has no extension
point for a corner widget, so nothing is injected into it. The hook starts it
per session rather than at Windows logon; to start it with Windows instead, drop
a shortcut to `<plugin>/start-widget.cmd` in `shell:startup` — ask before doing
that, and only if the user wants it.

## Cost

`scripts/deepseek_pricing.py` holds DeepSeek's list prices for the models this
machine routes to, so both the widget and the CLI can price usage without asking
for rates. **CNY is the default unit** (`¥`, 元/每百万 tokens), taken from
DeepSeek's Chinese pricing page — the numbers are official yuan prices, not a
dollar table converted at some invented exchange rate. `--usd` switches both
tools to DeepSeek's English dollar table. Two rules make the number trustworthy:

- Price every response on its own timestamp. Off-peak is **exactly half** of
  peak, and peak is 09:00-12:00 and 14:00-18:00 Beijing time (01:00-04:00 and
  06:00-10:00 UTC), Monday to Friday; weekends and Chinese public holidays are
  off-peak all day. A day's total cannot be multiplied by one factor — a
  Saturday is all off-peak, a weekday morning is not.
- Split input into cache hit and cache miss. The logged `input_tokens` already
  contains the cached part; `cached_input_tokens` bills at the cache-hit rate
  and the remainder at the cache-miss rate. `cache_write_input_tokens` is not
  billed by DeepSeek and is ignored. `reasoning_output_tokens` is already inside
  `output_tokens`, so it is not charged twice.

Prices live in one table; when DeepSeek changes them, edit `PEAK_RATES` there
(one sub-table per currency, peak rates only — off-peak is computed as half).
Cross-check a figure before quoting it: `codex_usage.py --by thread` and the
widget's session line read the same rows and must agree.

Three sizes of the same question, pick by what was asked: **one exchange**
(`--by turn`, e.g. "¥0.14 to answer this message"), **this conversation**
(the widget's `本次会话` line, or `--by thread`), **today** (the widget headline
and `--by day`). They differ by orders of magnitude, so say which one you are
quoting.

Caveats to pass on with the number:

- It is a **list-price estimate in CNY**, not an invoice. Balance, grants and
  any account-level discount are not visible from the logs.
- An unpriced model would silently count as 0, so the CLI names any model it
  does not know, and the widget marks the model in its footer (or says
  `价目未知`).
- Chinese public holidays are not shipped as data — the dates change yearly, and
  a stale list would misprice. Set `CODEX_USAGE_HOLIDAYS` to `YYYY-MM-DD` values
  (comma separated) to apply the exemption; without it those days are charged at
  peak rates.

## Invariants behind the numbers

- Logs live in `<codex-home>/sessions/YYYY/MM/DD/rollout-*.jsonl` **and**
  `<codex-home>/archived_sessions/`. Archived sessions are moved aside, not
  deleted; scanning only `sessions/` under-reported one day here by 87M tokens
  (29.3M vs 116.5M). The script scans both unless `--no-archived` is passed.
- Usage comes from `token_usage_record` events (one per model response);
  `turn_context` supplies the model per turn and `session_meta` marks subagent
  rollouts. `token_count` events carry cumulative thread totals — useful for
  spot-checking, wrong for summing.
- Log timestamps are UTC; `turn_context.timezone` is the user's zone. Bucket
  days in local time or evening sessions land on the wrong date.
- `input_tokens` **includes** `cached_input_tokens`, `reasoning_output_tokens`
  is a **subset** of `output_tokens`, and `total_tokens = input + output`.
  Never add the columns together.
- Deduplicate on `response_id`: resumed threads and archived copies can repeat
  records.
- Subagent rollouts (`session_meta.source.subagent`, plus `parent_thread_id`)
  spend real tokens. Keep them in totals and say when a number is mostly them.
- A live rollout keeps growing, so a total for today changes between runs.
- Live numbers rest on two Windows behaviours that defeat the obvious approach.
  While a process holds a log open, a bulk directory listing reports stale size
  **and** mtime — a log being written right now can look hours idle — and even a
  targeted `stat()` refreshes the size but not the mtime. So the widget picks
  the live log by the timestamp of its **last record**, and detects growth by
  comparing per-file sizes between polls, never by mtime.

## Reporting

Lead with the token facts and the split (new input vs cache read vs output),
then the ranking the user asked for. Cache reads usually dominate; a large
total is not automatically expensive.

Money figures come from the built-in DeepSeek table (see *Cost*), because this
machine's `config.toml` sends the `deepseek` provider to `https://api.deepseek.com`.
Never substitute OpenAI list prices: this machine does not call OpenAI. For any
other provider or a negotiated rate, pass `--price-in`, `--price-cache`,
`--price-out` (per 1M tokens, in whichever unit you are quoting), which switch
the table off and label the column as your rates; label the result an estimate
either way.

## Second opinion

`npx -y ccusage@latest codex daily` (ryoppippi/ccusage) reads the same logs. It
agreed exactly with this script on every closed day tested, and is a reasonable
cross-check or chart source when the network is available. Its USD column uses
OpenAI pricing, so present it as such, never as this machine's cost.

Do not install the marketplace plugin `codex-usage-tracker` on Windows: its
setup fails with `WinError 32` (upstream issues #418, #419, #452) and the
version its README pins (0.28.0) is not published on PyPI.
