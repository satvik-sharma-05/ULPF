"""
filters.py - One filter definition, shared by every aggregation in queries.py.

The dashboard used to be all-or-nothing: every panel aggregated the entire
corpus, so there was no way to ask "what did last Tuesday look like" or "show
me only NSX" without leaving the page. This is the single place that turns a
set of user-chosen constraints into a Cypher fragment, so a filter applied to
the headline counters is by construction the same filter applied to the
heatmap, the error rate and the rankings - they cannot disagree.

Two deliberate choices:

* The date range filters on `l.day` ('YYYY-MM-DD'), not on `l.timestamp`.
  `day` is indexed and is a plain lexicographic string, so `day >= $start AND
  day <= $end` is an index-backed range scan and the comparison is exact.
  Filtering on the full ISO timestamp would need string slicing per row and
  would defeat the index.

* Everything is a query PARAMETER. Nothing the user types is ever concatenated
  into the Cypher text - the values here reach Neo4j through the driver's
  parameter map, so a search box cannot become an injection vector.
"""

from typing import Any, Dict, NamedTuple, Optional


class LogFilter(NamedTuple):
    """User-chosen constraints on which :Log nodes an aggregation sees."""

    start: Optional[str] = None        # inclusive 'YYYY-MM-DD'
    end: Optional[str] = None          # inclusive 'YYYY-MM-DD'
    severity: Optional[str] = None     # exact level, e.g. 'ERROR'
    source_type: Optional[str] = None
    hostname: Optional[str] = None
    search: Optional[str] = None       # case-insensitive substring of message

    @property
    def active(self) -> bool:
        """True when at least one constraint is set. Callers use this to label
        a panel as filtered, and to skip caching a one-off filtered result
        under the same key as the unfiltered dashboard."""
        return any(v not in (None, '') for v in self)

    def clause(self, var: str = 'l') -> str:
        """Cypher predicates, each already prefixed with AND.

        Every query in queries.py opens with a WHERE of its own (`WHERE
        l.day IS NOT NULL`, and so on), so this is spliced directly after it
        and returns '' when nothing is set.
        """
        parts = []
        if self.start:
            parts.append(f"{var}.day >= $flt_start")
        if self.end:
            parts.append(f"{var}.day <= $flt_end")
        if self.severity:
            parts.append(f"{var}.severity = $flt_severity")
        if self.source_type:
            parts.append(f"{var}.source_type = $flt_source_type")
        if self.hostname:
            parts.append(f"{var}.hostname = $flt_hostname")
        if self.search:
            # toLower on both sides rather than a regex: a user-supplied regex
            # can be catastrophically slow, and CONTAINS is what people mean
            # when they type into a search box.
            parts.append(f"toLower({var}.message) CONTAINS $flt_search")
        return ''.join(f"\n          AND {p}" for p in parts)

    def params(self) -> Dict[str, Any]:
        """The parameter map matching clause(). Keys are prefixed `flt_` so
        they can never collide with a query's own $limit/$days."""
        out: Dict[str, Any] = {}
        if self.start:
            out['flt_start'] = self.start
        if self.end:
            out['flt_end'] = self.end
        if self.severity:
            out['flt_severity'] = self.severity.upper()
        if self.source_type:
            out['flt_source_type'] = self.source_type
        if self.hostname:
            out['flt_hostname'] = self.hostname
        if self.search:
            out['flt_search'] = self.search.lower()
        return out

    def cache_key(self) -> str:
        """Stable key for the response cache. An unfiltered request returns ''
        so it keeps sharing the existing warm cache entries."""
        if not self.active:
            return ''
        return '|'.join(f"{k}={v}" for k, v in sorted(self.params().items()))

    def describe(self) -> Dict[str, Any]:
        """Echoed back to the client so the UI can show what is in force -
        and so a screenshot of a filtered dashboard is self-describing."""
        return {k.replace('flt_', ''): v for k, v in self.params().items()}


# The "no constraints" filter. Every query defaults to this, so an unfiltered
# call is unchanged from before this module existed.
EMPTY = LogFilter()
