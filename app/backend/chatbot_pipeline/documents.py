"""
documents.py - Decks and reports written on request.

"Create a 7 slide PPT on the last 7 days of logs", "a 6 slide deck on global
warming", "a 3 page PDF on AI versus the market". The topic can be the graph or
it can be anything at all - the difference is only whether the content gets
grounded in real data before the model writes it.

Three stages, deliberately separate:

  1. parse()      what was asked for: kind, how many slides/pages, topic.
  2. compose()    the content, as structured JSON from the LLM. When the topic
                  is about the logs, real figures are pulled from the graph
                  FIRST and handed to the model as facts, so a deck about "last
                  week's errors" contains this system's actual numbers rather
                  than plausible-looking invented ones. That distinction is the
                  whole point: an invented incident count in a report someone
                  forwards is worse than no report.
  3. render()     JSON -> .pptx (python-pptx) or .pdf (ReportLab).

The model never renders and never counts. It writes prose from figures it is
given, which keeps the arithmetic out of the part that hallucinates.
"""

import io
import json
import os
import logging
import re
from typing import Any, Dict, List, Optional

import charts

logger = logging.getLogger(__name__)

MIN_SECTIONS = 1
MAX_SECTIONS = 20
DEFAULT_SECTIONS = 6

# Words that mean "this is about our own log data", which turns on grounding.
_LOG_TOPIC = re.compile(
    r'\b(log|logs|error|errors|alert|alerts|severity|host|hosts|incident|'
    r'ingest|graph|neo4j|esxi|nsx|kubernetes|coredns|syslog|siem|anomal)',
    re.IGNORECASE)

_KIND = re.compile(r'\b(pptx?|powerpoint|deck|slide|slides|presentation)\b', re.IGNORECASE)
_PDF = re.compile(r'\b(pdf|report|document|whitepaper|write-?up)\b', re.IGNORECASE)
_COUNT = re.compile(r'(\d{1,2})\s*[- ]?\s*(?:slide|slides|page|pages|section|sections)', re.IGNORECASE)

# Recognises a request for a document at all. Kept broad on purpose - a false
# positive costs one wasted LLM call, a false negative silently sends "make me
# a deck" to the Cypher generator, which answers something unrelated.
_ASK = re.compile(
    r'\b(create|make|generate|build|draft|prepare|give me|write)\b[^.?!]{0,60}?'
    r'\b(pptx?|powerpoint|deck|slides?|presentation|pdf|report|document)\b',
    re.IGNORECASE)


def looks_like_document_request(message: str) -> bool:
    return bool(_ASK.search(message or ''))


def parse(message: str, llm=None) -> Dict[str, Any]:
    """What kind of document, how long, and about what.

    Regex first, LLM only to clean up the topic. The count and the format are
    things a regex gets right and a model sometimes rounds off; the topic is
    the part worth a model's judgement, because "the recent 7 days logs" should
    become a title, not be echoed verbatim.
    """
    text = (message or '').strip()

    if _KIND.search(text):
        kind = 'pptx'
    elif re.search(r'\bpdf\b', text, re.IGNORECASE):
        kind = 'pdf'
    elif _PDF.search(text):
        # "report", "document", "write-up" -> Word. It is the format people
        # circulate and edit; a PDF is the print-ready copy, asked for by name.
        kind = 'docx'
    else:
        kind = 'pptx'

    m = _COUNT.search(text)
    count = int(m.group(1)) if m else DEFAULT_SECTIONS
    count = max(MIN_SECTIONS, min(MAX_SECTIONS, count))

    # Strip the instruction and the format words, keep the subject. Without
    # this the topic came out as "PPT on Global Warming", which the model then
    # dutifully used as the document's title.
    topic = _ASK.sub('', text)
    topic = _COUNT.sub('', topic)
    topic = re.sub(r'\b(pptx?|powerpoint|deck|slides?|presentation|pdf|report|document)\b',
                   '', topic, flags=re.IGNORECASE)
    # Now drop any leading connectives the removals exposed.
    for _ in range(3):
        topic = re.sub(r'^\s*(?:on|about|for|of|regarding|the|a|an|me|us)\b\s*',
                       '', topic.strip(), flags=re.IGNORECASE)
    topic = re.sub(r'\s+', ' ', topic).strip(' .!?,-')
    if not topic:
        topic = text

    return {'kind': kind, 'sections': count, 'topic': topic,
            'grounded': bool(_LOG_TOPIC.search(topic))}


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------
_FACT_QUERIES = [
    ('Total events in the graph',
     "MATCH (l:Log) RETURN count(l) AS value"),
    ('Events by severity',
     "MATCH (l:Log) WHERE l.severity IS NOT NULL "
     "RETURN l.severity AS key, count(l) AS value ORDER BY value DESC"),
    ('Events by source technology',
     "MATCH (l:Log) WHERE l.source_type IS NOT NULL "
     "RETURN l.source_type AS key, count(l) AS value ORDER BY value DESC LIMIT 10"),
    ('Events per day',
     "MATCH (l:Log) WHERE l.day IS NOT NULL "
     "RETURN l.day AS key, count(l) AS value ORDER BY key DESC LIMIT 14"),
    ('Hosts with the most errors',
     "MATCH (l:Log) WHERE l.severity_score <= 3 AND l.hostname IS NOT NULL "
     "RETURN l.hostname AS key, count(l) AS value ORDER BY value DESC LIMIT 10"),
    ('Most repeated error messages',
     "MATCH (l:Log) WHERE l.severity_score <= 3 AND l.message IS NOT NULL "
     "RETURN substring(l.message, 0, 90) AS key, count(l) AS value "
     "ORDER BY value DESC LIMIT 8"),
]


def gather_facts(driver, database: str = 'neo4j') -> List[Dict[str, Any]]:
    """Real figures from the graph, for a grounded document.

    Read-only and individually guarded: one slow or failing query costs its own
    section, not the whole document.
    """
    if driver is None:
        return []
    facts = []
    for label, query in _FACT_QUERIES:
        try:
            with driver.session(database=database) as session:
                rows = [dict(r) for r in session.run(query)]
            if rows:
                facts.append({'label': label, 'rows': rows})
        except Exception as e:
            logger.warning(f"fact query failed ({label}): {e}")
    return facts


def _facts_block(facts: List[Dict[str, Any]]) -> str:
    if not facts:
        return ''
    lines = ['REAL FIGURES FROM THE SYSTEM. Use these exact numbers; do not invent others.']
    for f in facts:
        rows = f['rows']
        if len(rows) == 1 and 'value' in rows[0] and 'key' not in rows[0]:
            lines.append(f"- {f['label']}: {rows[0]['value']:,}")
        else:
            inner = ', '.join(f"{r.get('key')} = {r.get('value'):,}" for r in rows[:10]
                              if r.get('value') is not None)
            lines.append(f"- {f['label']}: {inner}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You write concise, factual presentation and report content. You return "
    "ONLY JSON, no commentary and no code fences.\n"
    "Every section needs a specific title and 3 to 5 short bullets. A bullet is "
    "one clause, not a paragraph.\n"
    "If you are given REAL FIGURES, every number you write must come from them. "
    "Do not round them into vagueness and do not invent a figure that is not "
    "listed - a fabricated number in a document someone forwards is worse than "
    "an omitted one.\n"
    "If you are given no figures, write from general knowledge and do not "
    "pretend to cite data you do not have."
)


def _schema_prompt(topic: str, sections: int, kind: str, facts_block: str) -> str:
    unit = 'slides' if kind == 'pptx' else 'pages'
    return f"""Write the content for a {sections}-{unit[:-1]} {'presentation' if kind == 'pptx' else 'report'} on:

{topic}

{facts_block}

Return exactly this JSON shape:
{{
  "title": "<short title>",
  "subtitle": "<one line>",
  "sections": [
    {{"title": "<section title>", "bullets": ["<bullet>", "<bullet>", "<bullet>"]}}
  ]
}}

Exactly {sections} entries in "sections"."""


def _coerce(raw: Any, topic: str, sections: int) -> Dict[str, Any]:
    """Makes whatever the model returned into a renderable document.

    Models return JSON wrapped in fences, or a bare list, or the right shape
    with the wrong number of sections. Rendering is not the place to discover
    that, so it is all normalised here.
    """
    if isinstance(raw, str):
        text = raw.strip()
        text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.MULTILINE).strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        try:
            raw = json.loads(m.group(0) if m else text)
        except Exception:
            raw = None

    if not isinstance(raw, dict):
        raw = {}

    out_sections = []
    for s in (raw.get('sections') or [])[:sections]:
        if not isinstance(s, dict):
            continue
        bullets = [str(b).strip() for b in (s.get('bullets') or []) if str(b).strip()]
        out_sections.append({'title': str(s.get('title') or 'Section').strip(),
                             'bullets': bullets[:6] or ['(no content generated)']})
    while len(out_sections) < sections:
        out_sections.append({'title': f'Section {len(out_sections) + 1}',
                             'bullets': ['(no content generated)']})

    return {
        'title': str(raw.get('title') or topic)[:120],
        'subtitle': str(raw.get('subtitle') or '')[:200],
        'sections': out_sections,
    }


# Writing seven slides is a far longer generation than answering a question,
# and the chat timeout is sized for the latter. A 14B model on CPU needs
# minutes for this - measured: qwen3:14b exceeded the 120s chat timeout and
# returned nothing, which surfaced as a deck full of "(no content generated)".
COMPOSE_TIMEOUT = int(os.getenv('DOCUMENT_TIMEOUT', '600'))


def compose(topic: str, sections: int, kind: str, llm,
            facts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    facts_block = _facts_block(facts or [])
    prompt = _schema_prompt(topic, sections, kind, facts_block)
    raw = None

    if llm is not None:
        previous = getattr(llm, 'timeout', None)
        try:
            if previous is not None:
                llm.timeout = max(previous, COMPOSE_TIMEOUT)
            # json_mode is NOT used. Ollama's `format: json` makes qwen3 return
            # a bare "{}" for a prompt this size - it satisfies the format and
            # says nothing. Asking for JSON in the prompt and extracting it in
            # _coerce (which already strips fences and finds the object) gets
            # real content from the same model.
            raw = llm.complete(prompt, system=_SYSTEM, json_mode=False, temperature=0.4)
        except Exception as e:
            logger.warning(f"document composition failed: {e}")
        finally:
            if previous is not None:
                llm.timeout = previous

    doc = _coerce(raw, topic, sections)
    doc['grounded'] = bool(facts)

    # Charts are built from the QUERY RESULTS, not asked of the model. These
    # are the ones worth having - severity mix, the daily trend, the worst
    # hosts - and deriving them from the same rows the bullets were written
    # from means the bars cannot contradict the text beside them.
    #
    # They are attached round-robin so a 7-slide deck spreads its charts out
    # rather than stacking them all on slide two.
    derived = charts.from_facts(facts or [])
    if derived:
        for i, section in enumerate(doc['sections']):
            if i < len(derived):
                section['chart'] = derived[i]
    doc['charts'] = len(derived[:len(doc['sections'])])
    # Says plainly whether the model actually wrote anything. Without it a
    # timeout produces a structurally perfect, entirely empty document and
    # nothing anywhere admits that is what happened.
    doc['composed'] = bool(raw)
    return doc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
import re as _re

_MD = _re.compile(r'(\*\*|__|`)')


def _plain(text: str) -> str:
    """Strips inline markdown. A slide bullet is plain text, so leaving the
    markers in prints literal asterisks onto the slide."""
    return _MD.sub('', str(text or '')).strip()


def render_pptx(doc: Dict[str, Any]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = doc['title']
    title_slide.placeholders[1].text = doc.get('subtitle') or ''

    for section in doc['sections']:
        chart = section.get('chart')

        if chart:
            # Blank layout, so the bullets and the chart can be placed side by
            # side. The bulleted layout's body placeholder spans the slide and
            # would sit underneath the chart.
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = section['title']
            box = slide.shapes.add_textbox(Inches(0.5), Inches(1.6),
                                           Inches(4.2), Inches(4.4))
            frame = box.text_frame
            frame.word_wrap = True
            placed = charts.add_to_slide(
                slide, chart, Inches(5.0), Inches(1.5), Inches(4.6), Inches(4.6))
            if not placed:
                # The chart was rejected or failed to build; give the bullets
                # the whole slide back rather than leaving half of it empty.
                box.width = Inches(9.0)
        else:
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = section['title']
            frame = slide.placeholders[1].text_frame

        frame.clear()
        for i, bullet in enumerate(section['bullets']):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            para.text = _plain(bullet)
            para.level = 0
            for run in para.runs:
                run.font.size = Pt(14 if chart else 18)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def render_pdf(doc: Dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('ULPFTitle', parent=styles['Title'], fontSize=22, leading=26)
    h2 = ParagraphStyle('ULPFHead', parent=styles['Heading2'], fontSize=14, leading=18,
                        spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle('ULPFBody', parent=styles['BodyText'], fontSize=10.5, leading=15)

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=doc['title'])
    flow = [Paragraph(doc['title'], h1)]
    if doc.get('subtitle'):
        flow += [Spacer(1, 4), Paragraph(doc['subtitle'], body)]
    flow.append(Spacer(1, 10))

    for i, section in enumerate(doc['sections']):
        # One section per page, matching how the same content paginates as a
        # deck - a "3 page PDF" should be three pages, not three headings.
        if i:
            flow.append(PageBreak())
        flow.append(Paragraph(section['title'], h2))
        flow.append(ListFlowable(
            [ListItem(Paragraph(b, body), leftIndent=10) for b in section['bullets']],
            bulletType='bullet', start='•'))
        drawing = charts.pdf_flowable(section['chart']) if section.get('chart') else None
        if drawing is not None:
            flow += [Spacer(1, 10), drawing]

    pdf.build(flow)
    return buf.getvalue()


_RENDERERS = {'pptx': lambda d: render_pptx(d),
              'pdf': lambda d: render_pdf(d),
              'docx': lambda d: render_docx(d)}


def render(doc: Dict[str, Any], kind: str) -> bytes:
    return _RENDERERS[kind](doc)


MEDIA_TYPES = {
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}


def filename(doc: Dict[str, Any], kind: str) -> str:
    stem = re.sub(r'[^A-Za-z0-9]+', '-', doc.get('title') or 'document').strip('-').lower()
    return f"{stem[:60] or 'document'}.{kind}"


def render_docx(doc: Dict[str, Any]) -> bytes:
    """The same document as an editable Word file.

    Word is the format people actually circulate a report in - it gets edited,
    commented and pasted into, which a PDF does not. So the report format is
    .docx and the PDF becomes the print-ready copy rather than the only one.

    Charts are embedded as images here, unlike the deck and the PDF where they
    are native objects. python-docx has no chart API at all, and the honest
    trade is a picture of the right chart over no chart - the underlying
    figures are in the bullets beside it either way. They are rendered at 2x
    and placed at half size so they stay sharp when someone zooms.
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    document = Document()

    heading = document.add_heading(doc['title'], level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
    if doc.get('subtitle'):
        sub = document.add_paragraph(doc['subtitle'])
        sub.runs[0].font.size = Pt(11)
        sub.runs[0].font.color.rgb = RGBColor(0x6B, 0x5B, 0x4D)

    for i, section in enumerate(doc['sections']):
        # A page break per section, so a "4 page report" really is four pages -
        # matching how the same content paginates as a PDF and a deck.
        if i:
            document.add_page_break()
        document.add_heading(section['title'], level=1)
        for bullet in section['bullets']:
            document.add_paragraph(_plain(bullet), style='List Bullet')

        chart = section.get('chart')
        if chart:
            png = charts.png_bytes(chart)
            if png:
                document.add_picture(io.BytesIO(png), width=Inches(5.8))

    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()
