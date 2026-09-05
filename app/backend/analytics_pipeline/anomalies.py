"""
anomalies.py - Statistical outlier detection over the normalized events.

The problem statement asks the unified schema to enable "anomaly detection".
The rule-based insights next door answer "what is the state of things"; this
answers a different question - "what is unlike itself" - and the two are not
substitutes. A host that always emits 400 errors a day is not interesting; the
same host emitting 4,000 today is, and no threshold rule that works across an
estate can express that. The baseline has to come from the data.

Three detectors, each on a different axis:

    volume_spike     a day whose event count departs from the corpus baseline
    error_rate_spike a day whose error RATE departs from the baseline - which
                     separates "busier" from "sicker", the distinction a raw
                     count cannot make
    noisy_host       a host whose error share is far above its peers

Method: modified z-score on the median absolute deviation, not mean and
standard deviation. Log volumes are skewed and frequently contain the very
outliers being looked for, and a single 10x day drags the mean far enough to
hide itself. The median and MAD are unmoved by a handful of extreme points,
which is the entire reason to use them here.

Everything is computed in Cypher aggregates plus a little arithmetic - no
model, no training, nothing to ship. That keeps it honest on an air-gapped VM
and means the result can always be traced back to the query that produced it.
"""

import logging
from typing import Any, Dict, List, Optional

from filters import EMPTY, LogFilter
from neo4j_client import run_read

logger = logging.getLogger(__name__)

# 3.5 is the conventional cut-off for the modified z-score. Lower floods the
# feed with ordinary variation, which trains people to ignore it.
DEFAULT_THRESHOLD = 3.5

# Below this there is no baseline worth measuring against - with five days of
# data every day is either "normal" or "an outlier" depending on which one you
# leave out.
MIN_POINTS = 7

# 0.6745 is the 75th percentile of the standard normal; it rescales MAD so the
# resulting score is comparable to a standard z-score.
_MAD_SCALE = 0.6745


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def modified_z(values: List[float]) -> List[float]:
    """Modified z-score per point. See the module note on why not mean/stdev."""
    if len(values) < 2:
        return [0.0] * len(values)
    med = _median(values)
    mad = _median([abs(v - med) for v in values])
    if mad == 0:
        # Every point identical except a few: MAD collapses to zero and the
        # score would divide by it. Fall back to mean absolute deviation, which
        # degrades gracefully instead of raising.
        mean_dev = sum(abs(v - med) for v in values) / len(values)
        if mean_dev == 0:
            return [0.0] * len(values)
        return [(v - med) / (1.253314 * mean_dev) for v in values]
    return [_MAD_SCALE * (v - med) / mad for v in values]


def _finding(kind: str, subject: str, score: float, observed: float,
             baseline: float, detail: str) -> Dict[str, Any]:
    return {
        'kind': kind,
        'subject': subject,
        'score': round(abs(score), 2),
        'observed': round(observed, 2),
        'baseline': round(baseline, 2),
        'direction': 'above' if observed >= baseline else 'below',
        'severity': 'high' if abs(score) >= 6 else 'medium',
        'detail': detail,
    }


def detect(driver, flt: LogFilter = EMPTY,
           threshold: float = DEFAULT_THRESHOLD) -> Dict[str, Any]:
    """Every anomaly the current data supports, most extreme first."""
    if driver is None:
        return {'findings': [], 'baseline_points': 0,
                'note': 'Not connected to the graph.'}

    where = flt.clause()
    params = flt.params()
    findings: List[Dict[str, Any]] = []

    # --- volume and error rate, per day -----------------------------------
    daily = run_read(driver, f"""
        MATCH (l:Log)
        WHERE l.day IS NOT NULL{where}
        WITH l.day AS day, count(l) AS total,
             sum(CASE WHEN l.severity_score <= 3 THEN 1 ELSE 0 END) AS errors
        RETURN day, total, errors
        ORDER BY day
    """, **params)

    if len(daily) >= MIN_POINTS:
        totals = [float(r['total']) for r in daily]
        for row, score in zip(daily, modified_z(totals)):
            if abs(score) >= threshold:
                findings.append(_finding(
                    'volume_spike', row['day'], score, row['total'], _median(totals),
                    f"{row['total']:,} events against a typical {_median(totals):,.0f}"))

        rates = [(float(r['errors']) / r['total'] * 100 if r['total'] else 0.0) for r in daily]
        for row, rate, score in zip(daily, rates, modified_z(rates)):
            if abs(score) >= threshold:
                findings.append(_finding(
                    'error_rate_spike', row['day'], score, rate, _median(rates),
                    f"{rate:.1f}% of events were errors against a typical {_median(rates):.1f}%"))

    # --- hosts that are unlike their peers ---------------------------------
    hosts = run_read(driver, f"""
        MATCH (l:Log)
        WHERE l.hostname IS NOT NULL{where}
        WITH l.hostname AS host, count(l) AS total,
             sum(CASE WHEN l.severity_score <= 3 THEN 1 ELSE 0 END) AS errors
        WHERE total >= 50
        RETURN host, total, errors
    """, **params)

    if len(hosts) >= MIN_POINTS:
        rates = [(float(h['errors']) / h['total'] * 100) for h in hosts]
        for host, rate, score in zip(hosts, rates, modified_z(rates)):
            # Only ABOVE the baseline: a host with no errors is not a finding,
            # and reporting it as one is how an anomaly feed loses its audience.
            if score >= threshold and rate > 0:
                findings.append(_finding(
                    'noisy_host', host['host'], score, rate, _median(rates),
                    f"{host['errors']:,} of {host['total']:,} events are errors "
                    f"({rate:.1f}%) against a typical {_median(rates):.1f}%"))

    findings.sort(key=lambda f: f['score'], reverse=True)

    note = None
    if len(daily) < MIN_POINTS:
        note = (f"Only {len(daily)} day(s) of data - at least {MIN_POINTS} are needed "
                f"before a daily baseline means anything.")

    return {
        'findings': findings[:25],
        'baseline_points': len(daily),
        'hosts_considered': len(hosts),
        'threshold': threshold,
        'method': 'modified z-score on median absolute deviation',
        'note': note,
    }
