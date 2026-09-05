"""
insights.py - Turns the raw aggregates from queries.py into plain-English
observations: "which source shows more errors", "is this trending worse",
"which host dominates the error count" - the "what does this data actually
tell me" layer on top of the "what is the data" layer.

Deliberately rule-based, not LLM-generated: every number quoted here is
computed directly from the same aggregates the dashboard's charts already
show, so an insight can never contradict the chart next to it, and this
module has zero extra runtime dependencies (no model to load, nothing that
can be "unreachable") - consistent with the rest of this pipeline being pure
Neo4j + FastAPI. If a richer narrative synthesis is ever wanted, it belongs
as an optional layer *on top* of these facts (same pattern as
chatbot_pipeline's LLM synthesis on top of retrieved rows), not a replacement
for them.
"""

from typing import Any, Dict, List, Optional


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def build_insights(
    overview: Dict[str, Any],
    severity_rows: List[Dict[str, Any]],
    source_rows: List[Dict[str, Any]],
    hosts_by_errors: List[Dict[str, Any]],
    daily: List[Dict[str, Any]],
    min_volume: int = 20,
    trend_window_days: int = 7,
    heatmap: Optional[List[Dict[str, Any]]] = None,
    top_messages: Optional[List[Dict[str, Any]]] = None,
    severity_by_source: Optional[List[Dict[str, Any]]] = None,
    entities: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    insights: List[Dict[str, Any]] = []
    total_logs = overview.get('total_logs') or 0
    total_errors = overview.get('total_errors') or 0
    total_alerts = overview.get('total_alerts') or 0

    if not total_logs:
        return [{
            'id': 'empty-graph',
            'level': 'info',
            'title': 'No logs in the graph yet',
            'detail': 'Run the ingestion pipeline before analytics has anything to summarize.',
        }]

    # -- top error source ----------------------------------------------------
    ranked_sources = sorted(
        (r for r in source_rows if (r.get('errors') or 0) > 0),
        key=lambda r: r['errors'], reverse=True,
    )
    if ranked_sources:
        top = ranked_sources[0]
        share = _pct(top['errors'], total_errors)
        insights.append({
            'id': 'top-error-source',
            'level': 'warning' if share >= 40 else 'info',
            'title': f"{top['source_type']} produces the most errors",
            'detail': (
                f"{top['source_type']} accounts for {top['errors']:,} errors "
                f"({share}% of all errors across every source type)."
            ),
        })

    # -- riskiest host (highest error rate, above a minimum volume) ----------
    eligible = [h for h in hosts_by_errors if (h.get('total') or 0) >= min_volume]
    if eligible:
        for h in eligible:
            h['_error_rate'] = _pct(h.get('errors') or 0, h['total'])
        riskiest = max(eligible, key=lambda h: h['_error_rate'])
        if riskiest['_error_rate'] > 0:
            insights.append({
                'id': 'riskiest-host',
                'level': 'critical' if riskiest['_error_rate'] >= 25 else 'warning',
                'title': f"{riskiest['host']} has the highest error rate",
                'detail': (
                    f"{riskiest['_error_rate']}% of {riskiest['host']}'s {riskiest['total']:,} logs "
                    f"are ERROR-or-worse (min. {min_volume} logs to qualify), "
                    f"the highest rate of any host."
                ),
            })

    # -- error concentration: does one host dominate the error count ---------
    if hosts_by_errors and total_errors:
        biggest = max(hosts_by_errors, key=lambda h: h.get('errors') or 0)
        share = _pct(biggest.get('errors') or 0, total_errors)
        if share >= 20:
            insights.append({
                'id': 'error-concentration',
                'level': 'warning' if share >= 40 else 'info',
                'title': f"{biggest['host']} concentrates a large share of all errors",
                'detail': (
                    f"{biggest['host']} alone produced {biggest.get('errors', 0):,} errors "
                    f"- {share}% of every error in the graph."
                ),
            })

    # -- signal-to-noise: how much of the corpus is actionable ---------------
    noise = sum(r['count'] for r in severity_rows if (r.get('score') or 0) >= 6)  # INFO/DEBUG
    actionable = sum(r['count'] for r in severity_rows if (r.get('score') or 0) <= 3)  # ERROR+
    if severity_rows and total_logs:
        insights.append({
            'id': 'signal-to-noise',
            'level': 'info',
            'title': 'Signal vs. noise across all severities',
            'detail': (
                f"{_pct(actionable, total_logs)}% of logs are ERROR-or-worse "
                f"({actionable:,} logs), while {_pct(noise, total_logs)}% are "
                f"informational/debug noise ({noise:,} logs)."
            ),
        })

    # -- alert volume ----------------------------------------------------------
    if total_alerts:
        insights.append({
            'id': 'alert-volume',
            'level': 'critical' if _pct(total_alerts, total_logs) >= 5 else 'warning',
            'title': f"{total_alerts:,} critical-or-worse alerts logged",
            'detail': (
                f"{total_alerts:,} logs ({_pct(total_alerts, total_logs)}% of the corpus) are "
                f"EMERGENCY, FATAL, ALERT or CRITICAL severity."
            ),
        })

    # -- busiest / worst day --------------------------------------------------
    if daily:
        busiest = max(daily, key=lambda d: d.get('total') or 0)
        insights.append({
            'id': 'busiest-day',
            'level': 'info',
            'title': f"{busiest['bucket']} was the busiest day",
            'detail': f"{busiest['total']:,} logs were written on {busiest['bucket']}, the most of any day in range.",
        })
        worst = max(daily, key=lambda d: d.get('alerts') or 0)
        if worst.get('alerts'):
            insights.append({
                'id': 'worst-day',
                'level': 'critical' if worst['alerts'] >= 10 else 'warning',
                'title': f"{worst['bucket']} had the most alerts",
                'detail': f"{worst['alerts']:,} critical-or-worse alerts were logged on {worst['bucket']}.",
            })

    # -- trend: error rate this window vs. the window before it --------------
    trend = _trend_insight(daily, trend_window_days)
    if trend:
        insights.append(trend)

    insights.extend(_extra_insights(
        overview=overview, daily=daily, heatmap=heatmap,
        top_messages=top_messages, severity_by_source=severity_by_source,
        entities=entities, hosts_by_errors=hosts_by_errors,
    ))
    return insights


def _extra_insights(overview, daily, heatmap, top_messages,
                    severity_by_source, entities, hosts_by_errors):
    """Observations that need the newer panels' data.

    Split out rather than inlined so the original rules keep working when a
    caller doesn't have these extra aggregates to hand - every argument is
    optional and each rule simply doesn't fire without its input.
    """
    out = []
    total_logs = (overview or {}).get('total_logs') or 0
    total_errors = (overview or {}).get('total_errors') or 0

    # -- the single noisiest repeated error --------------------------------
    if top_messages:
        top = top_messages[0]
        share = _pct(top.get('occurrences', 0), total_errors) if total_errors else 0
        if share >= 10:
            hosts = top.get('hosts') or []
            spread = (f"across {len(hosts)}+ hosts" if len(hosts) > 1
                      else f"all from {hosts[0]}" if hosts else '')
            out.append({
                'id': 'dominant-error-message',
                'level': 'warning' if share >= 25 else 'info',
                'title': 'One repeated message dominates the errors',
                'detail': (
                    f'"{(top.get("pattern") or "").strip()[:90]}…" occurs '
                    f'{top.get("occurrences", 0):,} times — {share}% of all errors {spread}. '
                    f'Fixing this one pattern would clear most of the error volume.'
                ),
            })

    # -- busiest hour of day, from the heatmap -----------------------------
    if heatmap:
        by_hour = {}
        for cell in heatmap:
            h = cell.get('hour')
            if h is None:
                continue
            slot = by_hour.setdefault(h, {'total': 0, 'errors': 0})
            slot['total'] += cell.get('total') or 0
            slot['errors'] += cell.get('errors') or 0
        if by_hour:
            peak_hour, peak = max(by_hour.items(), key=lambda kv: kv[1]['total'])
            grand = sum(v['total'] for v in by_hour.values())
            out.append({
                'id': 'peak-hour',
                'level': 'info',
                'title': f'Traffic peaks at {peak_hour:02d}:00 UTC',
                'detail': (
                    f'{peak["total"]:,} logs ({_pct(peak["total"], grand)}% of all volume) land in the '
                    f'{peak_hour:02d}:00 hour — the window to watch during an incident.'
                ),
            })
            # An hour whose ERROR share is well above the corpus average is a
            # different, more actionable signal than raw volume.
            overall_rate = _pct(total_errors, total_logs) if total_logs else 0
            worst = max(
                ((h, v) for h, v in by_hour.items() if v['total'] >= 50),
                key=lambda kv: _pct(kv[1]['errors'], kv[1]['total']),
                default=None,
            )
            if worst and overall_rate:
                h, v = worst
                rate = _pct(v['errors'], v['total'])
                if rate > overall_rate * 1.5:
                    out.append({
                        'id': 'worst-hour-rate',
                        'level': 'warning',
                        'title': f'{h:02d}:00 has a disproportionate error rate',
                        'detail': (
                            f'{rate}% of logs in the {h:02d}:00 hour are errors, against a '
                            f'{overall_rate}% average across the day.'
                        ),
                    })

    # -- a source type that is failing rather than merely chatty -----------
    if severity_by_source:
        eligible = [s for s in severity_by_source if (s.get('total') or 0) >= 100]
        if eligible:
            worst = max(eligible, key=lambda s: _pct((s.get('errors') or 0) + (s.get('critical') or 0), s['total']))
            rate = _pct((worst.get('errors') or 0) + (worst.get('critical') or 0), worst['total'])
            if rate >= 5:
                out.append({
                    'id': 'unhealthiest-source',
                    'level': 'warning' if rate >= 15 else 'info',
                    'title': f'{worst["source_type"]} is the least healthy source',
                    'detail': (
                        f'{rate}% of {worst["source_type"]}\'s {worst["total"]:,} logs are ERROR or worse — '
                        f'the highest failure rate of any source type with meaningful volume.'
                    ),
                })

    # -- entity hotspot ----------------------------------------------------
    if entities:
        for label in ('User', 'IPAddress', 'Device', 'VM'):
            rows = entities.get(label) or []
            if len(rows) >= 2 and rows[0].get('mentions'):
                top, second = rows[0], rows[1]
                if top['mentions'] >= max(20, second.get('mentions', 0) * 3):
                    out.append({
                        'id': f'entity-hotspot-{label.lower()}',
                        'level': 'info',
                        'title': f'One {label} dominates activity',
                        'detail': (
                            f'{label} "{top["value"]}" appears in {top["mentions"]:,} logs — '
                            f'{round(top["mentions"] / max(second.get("mentions", 1), 1), 1)}x the next '
                            f'most-referenced ({second["value"]}).'
                        ),
                    })
                    break

    # -- coverage gap: how much of the corpus has no usable timestamp ------
    if daily:
        dated = sum(d.get('total') or 0 for d in daily)
        if total_logs and dated < total_logs * 0.98:
            missing = total_logs - dated
            out.append({
                'id': 'timestamp-coverage',
                'level': 'info',
                'title': f'{_pct(missing, total_logs)}% of logs have no usable date',
                'detail': (
                    f'{missing:,} logs could not be placed on a calendar day, so they are absent '
                    f'from every time-based panel. Usually a format whose timestamp the parser '
                    f'did not recognise.'
                ),
            })
    return out


def _trend_insight(daily: List[Dict[str, Any]], window_days: int) -> Optional[Dict[str, Any]]:
    if len(daily) < window_days * 2:
        return None  # not enough history for a fair before/after comparison

    recent = daily[-window_days:]
    prior = daily[-window_days * 2:-window_days]

    def error_rate(rows: List[Dict[str, Any]]) -> float:
        total = sum(r.get('total') or 0 for r in rows)
        errors = sum(r.get('errors') or 0 for r in rows)
        return _pct(errors, total)

    recent_rate = error_rate(recent)
    prior_rate = error_rate(prior)
    if prior_rate == 0:
        return None

    change = round(recent_rate - prior_rate, 1)
    if abs(change) < 1:
        return None  # not a meaningful move

    direction = 'worsened' if change > 0 else 'improved'
    return {
        'id': 'error-trend',
        'level': 'warning' if change > 0 else 'good',
        'title': f"Error rate has {direction} over the last {window_days} days",
        'detail': (
            f"The error rate was {recent_rate}% over the most recent {window_days} days, "
            f"versus {prior_rate}% in the {window_days} days before that "
            f"({'+' if change > 0 else ''}{change} points)."
        ),
    }
