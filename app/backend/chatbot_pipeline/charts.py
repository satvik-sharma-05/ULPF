"""
charts.py - The chart contract, its validator, and native renderers.

One ChartSpec shape is shared by the chat, the .pptx and the .pdf, so a chart
looks the same wherever it ends up and there is one place to fix when it does
not.

Two rules this module exists to enforce:

**Charts are NATIVE, never images.** `add_chart` in python-pptx and reportlab's
chart flowables produce a real editable object - ppt/charts/chart1.xml in the
deck, vector graphics in the PDF. A rendered PNG is a screenshot of a chart, not
a chart, and the difference is whether the recipient can fix a label.

**Every spec is validated before it is rendered.** Spec content can come from a
language model, which makes it untrusted input. The checks below are the ones
that catch real failures rather than type errors - in particular that `x_key`
actually appears in the rows, because a spec naming a column that is not there
passes every other check and renders a chart with a blank category axis.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CHART_TYPES = ('bar', 'column', 'line', 'area', 'pie')

MIN_ROWS = 2
MAX_ROWS = 40

# Fixed slot order, validated for colour-vision deficiency. Assigned by slot and
# never cycled, so the same series is the same colour in the chat, the deck and
# the report. python-pptx wants bare hex with no '#'.
SERIES_HEX = ('2A78D6', 'EB6834', '1BAF7A', 'EDA100', 'E87BA4', '4A3AA7')

# Pie encodes magnitude, not category, so it gets one hue light to dark rather
# than six unrelated colours - a rainbow pie implies differences that are not
# in the data.
PIE_RAMP = ('9EC5F0', '7CB0E9', '5A9BE2', '3886DB', '2A78D6', '1F5CA3',
            '17457A', '102E52')


def spec(chart_type: str, title: str, x_key: str, series: List[Dict[str, str]],
         rows: List[Dict[str, Any]], y_label: Optional[str] = None) -> Dict[str, Any]:
    return {'type': chart_type, 'title': title, 'x_key': x_key,
            'series': series, 'data': rows, 'y_label': y_label}


def is_valid(candidate: Any) -> bool:
    """True when this spec will actually render something readable."""
    if not isinstance(candidate, dict):
        return False
    if candidate.get('type') not in CHART_TYPES:
        return False

    x_key = candidate.get('x_key')
    if not isinstance(x_key, str) or not x_key:
        return False

    series = candidate.get('series')
    if not isinstance(series, list) or not series:
        return False
    if not all(isinstance(s, dict) and isinstance(s.get('key'), str) and s['key']
               for s in series):
        return False

    rows = candidate.get('data')
    if not isinstance(rows, list) or not (MIN_ROWS <= len(rows) <= MAX_ROWS):
        return False
    if not all(isinstance(r, dict) for r in rows):
        return False

    # The trap worth checking for: a spec can name an x_key that appears in no
    # row at all. Everything else passes and the category axis renders blank.
    if not any(x_key in r for r in rows):
        return False

    # Pie shows one measure. More than one is a spec that cannot be drawn.
    if candidate['type'] == 'pie' and len(series) != 1:
        return False

    # At least one series has to resolve to a number somewhere, or the chart is
    # an empty frame with a title.
    return any(_num(r.get(s['key'])) is not None for s in series for r in rows)


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float('inf'), float('-inf')) else None


def _categories(chart: Dict[str, Any]) -> List[str]:
    return [str(r.get(chart['x_key'], ''))[:40] for r in chart['data']]


def _values(chart: Dict[str, Any], key: str) -> List[float]:
    # One None corrupts the chart XML, so every value is coerced. 0.0 is the
    # honest stand-in: the category exists, the measure is absent.
    return [(_num(r.get(key)) or 0.0) for r in chart['data']]


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------
_PPTX_TYPE = {'bar': 'BAR_CLUSTERED', 'column': 'COLUMN_CLUSTERED',
              'line': 'LINE_MARKERS', 'area': 'AREA', 'pie': 'PIE'}


def add_to_slide(slide, chart: Dict[str, Any], x, y, cx, cy) -> bool:
    """Adds a native, editable PowerPoint chart. Returns False if it could not.

    A chart that fails to build must not take the slide with it - the bullets
    are still worth having.
    """
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

    if not is_valid(chart):
        logger.warning(f"chart rejected by validation: {str(chart)[:160]}")
        return False

    try:
        data = CategoryChartData()
        data.categories = _categories(chart)
        for s in chart['series']:
            data.add_series(s.get('label') or s['key'], _values(chart, s['key']))

        kind = getattr(XL_CHART_TYPE, _PPTX_TYPE[chart['type']])
        frame = slide.shapes.add_chart(kind, x, y, cx, cy, data)
        obj = frame.chart

        # A legend for one series repeats the title. Two or more need it.
        many = len(chart['series']) > 1 or chart['type'] == 'pie'
        obj.has_legend = many
        if many:
            obj.legend.position = XL_LEGEND_POSITION.BOTTOM
            obj.legend.include_in_layout = False

        if chart['type'] == 'pie':
            # Magnitude ramp, not categorical hues - see PIE_RAMP.
            points = obj.plots[0].points
            for i, point in enumerate(points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = RGBColor.from_string(
                    PIE_RAMP[i % len(PIE_RAMP)])
        else:
            for i, plot_series in enumerate(obj.series):
                plot_series.format.fill.solid()
                plot_series.format.fill.fore_color.rgb = RGBColor.from_string(
                    SERIES_HEX[i % len(SERIES_HEX)])
        return True
    except Exception as e:
        logger.warning(f"native chart failed, slide keeps its bullets: {e}")
        return False


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def pdf_flowable(chart: Dict[str, Any], width: float = 430, height: float = 200):
    """A vector chart flowable for ReportLab, or None."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.colors import HexColor

    if not is_valid(chart):
        return None

    try:
        drawing = Drawing(width, height)
        drawing.add(String(0, height - 12, chart.get('title', '')[:70],
                           fontName='Helvetica-Bold', fontSize=10))
        cats = _categories(chart)
        rows = [_values(chart, s['key']) for s in chart['series']]

        if chart['type'] == 'pie':
            pie = Pie()
            pie.x, pie.y = width / 2 - 70, 10
            pie.width = pie.height = height - 46
            pie.data = rows[0]
            pie.labels = cats
            pie.sideLabels = True
            for i in range(len(rows[0])):
                pie.slices[i].fillColor = HexColor('#' + PIE_RAMP[i % len(PIE_RAMP)])
            drawing.add(pie)
        elif chart['type'] in ('line', 'area'):
            line = HorizontalLineChart()
            line.x, line.y = 34, 26
            line.width, line.height = width - 50, height - 62
            line.data = rows
            line.categoryAxis.categoryNames = cats
            line.categoryAxis.labels.angle = 30
            line.categoryAxis.labels.boxAnchor = 'ne'
            line.categoryAxis.labels.fontSize = 6
            line.valueAxis.labels.fontSize = 6
            for i in range(len(rows)):
                line.lines[i].strokeColor = HexColor('#' + SERIES_HEX[i % len(SERIES_HEX)])
                line.lines[i].strokeWidth = 1.6
            drawing.add(line)
        else:
            bar = VerticalBarChart()
            bar.x, bar.y = 34, 26
            bar.width, bar.height = width - 50, height - 62
            bar.data = rows
            bar.categoryAxis.categoryNames = cats
            bar.categoryAxis.labels.angle = 30
            bar.categoryAxis.labels.boxAnchor = 'ne'
            bar.categoryAxis.labels.fontSize = 6
            bar.valueAxis.labels.fontSize = 6
            bar.valueAxis.valueMin = 0
            for i in range(len(rows)):
                bar.bars[i].fillColor = HexColor('#' + SERIES_HEX[i % len(SERIES_HEX)])
            drawing.add(bar)
        return drawing
    except Exception as e:
        logger.warning(f"pdf chart failed, section keeps its bullets: {e}")
        return None


# ---------------------------------------------------------------------------
# Charts derived from the graph's own figures
# ---------------------------------------------------------------------------
def from_facts(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turns the grounded fact rows into chart specs.

    Derived here rather than asked of the model on purpose. These are the
    charts most worth having - the severity mix, the daily trend, the worst
    hosts - and building them from the query results directly means the bars
    cannot disagree with the numbers in the bullets beside them.
    """
    charted = []
    for fact in facts:
        rows = [r for r in fact['rows'] if r.get('key') is not None
                and _num(r.get('value')) is not None]
        if not (MIN_ROWS <= len(rows) <= MAX_ROWS):
            continue

        label = fact['label']
        low = label.lower()
        if 'per day' in low or 'daily' in low:
            # A time series reads as a trend only in date order; the query
            # returns newest first for the LIMIT to work.
            rows = sorted(rows, key=lambda r: str(r['key']))
            kind = 'line'
        elif 'severity' in low:
            kind = 'pie'
        else:
            kind = 'column'

        charted.append(spec(
            chart_type=kind,
            title=label,
            x_key='key',
            series=[{'key': 'value', 'label': 'Events'}],
            rows=[{'key': str(r['key'])[:40], 'value': _num(r['value'])} for r in rows],
            y_label='Events',
        ))
    return charted


def png_bytes(chart: Dict[str, Any], width: float = 460, height: float = 220,
              scale: int = 2) -> Optional[bytes]:
    """The same chart, rasterised - for Word, which has no native chart API.

    Reuses pdf_flowable rather than drawing anything new, so a chart is
    identical across the deck, the report and the document. Rendered at 2x and
    placed at half size, because a 96dpi PNG in Word looks like a screenshot
    the moment anyone zooms.

    Returns None if the chart is invalid or the renderer is unavailable; the
    caller keeps its bullets either way.
    """
    drawing = pdf_flowable(chart, width=width, height=height)
    if drawing is None:
        return None
    try:
        from reportlab.graphics import renderPM
        drawing.scale(scale, scale)
        drawing.width *= scale
        drawing.height *= scale
        return renderPM.drawToString(drawing, fmt='PNG', dpi=72 * scale, bg=0xFFFFFF)
    except Exception as e:
        logger.warning(f"chart rasterisation failed, section keeps its bullets: {e}")
        return None


def from_rows(rows: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Infers a chart from an arbitrary query result, or None.

    Mirrors the rule the chat's RowsChart uses on the client, so exporting an
    answer produces the same chart the user is looking at rather than a second
    opinion about their data.

    Returns None readily. Most query results are not chartable - one row is a
    fact, a result with no numeric column has nothing to measure, and past
    MAX_ROWS the bars are thinner than their labels. A missing chart is fine;
    a misleading one is not.
    """
    if not rows or not (MIN_ROWS <= len(rows) <= MAX_ROWS):
        return None
    columns = list(rows[0].keys())
    if len(columns) < 2:
        return None

    numeric = [c for c in columns if all(_num(r.get(c)) is not None for r in rows)]
    # A label column has to be non-numeric AND reasonably distinct - a column
    # repeating three values is a category, not an axis.
    labels = [c for c in columns
              if c not in numeric and len({str(r.get(c)) for r in rows}) >= min(len(rows), MIN_ROWS)]
    if not numeric or not labels:
        return None

    # Prefer a time-like column so a per-day result charts as a trend in date
    # order rather than in whatever order the query returned.
    timeish = next((c for c in labels
                    if c.lower() in ('day', 'date', 'bucket', 'timestamp', 'hour', 'month')), None)
    x_key = timeish or labels[0]
    measures = numeric[:3]

    data = sorted(rows, key=lambda r: str(r.get(x_key))) if timeish else \
        sorted(rows, key=lambda r: -(_num(r.get(measures[0])) or 0))

    return spec(
        chart_type='line' if timeish else 'column',
        title=f"{measures[0].replace('_', ' ').title()} by {x_key.replace('_', ' ')}",
        x_key=x_key,
        series=[{'key': m, 'label': m.replace('_', ' ').title()} for m in measures],
        rows=[{k: r.get(k) for k in [x_key] + measures} for r in data],
    )
