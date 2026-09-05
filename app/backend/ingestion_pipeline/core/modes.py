"""
core/modes.py - The three operating modes, and the single place that decides
what each one means.

    sample      ONLINE demo. The bundled corpus against a local Neo4j, on a
                machine that has internet - so cloud LLM providers (Groq,
                OpenRouter) are available alongside a local Ollama. This is
                the "show me the product working" path.

    production  AIRGAPPED deployment. Real Kafka brokers + a real Neo4j on a
                machine with NO outbound internet, running the realtime
                DNIF -> Kafka -> parse -> embed -> Neo4j path. Everything
                must be local: the LLM is Ollama on the VM itself, and the
                embedding/reranker weights are baked into the images.

    custom      the user uploads their own file (Syslog, JSON, XML, CSV, CEF,
                LEEF, or a proprietary/plain-text shape), it's parsed with the
                same core/ parser + entity extraction + embeddings, and the
                same chatbot and analytics then run against it.

Why a module rather than three separate entry points: all three modes share
the entire core/ pipeline - identical parser, identical entity extraction,
identical embeddings, identical graph. What actually differs is only (a)
where records come from and (b) which Neo4j/Kafka they point at. Encoding
that as *configuration* keeps the one code path that's been tested, instead
of forking three near-copies of it that would drift.

APP_MODE picks the mode; every mode's connection settings still come from
the same env vars core/config.py already reads, so a mode is a set of
defaults and a policy, never a second config system.
"""

import os
from typing import Any, Dict, List, NamedTuple, Optional

SAMPLE = 'sample'
PRODUCTION = 'production'
CUSTOM = 'custom'

ALL_MODES = (SAMPLE, PRODUCTION, CUSTOM)


class ModeSpec(NamedTuple):
    name: str
    label: str
    description: str
    # Where records come from in this mode.
    source: str
    # Does this mode need Kafka/DNIF wired up before it can run at all?
    requires_kafka: bool
    # Does this deployment have outbound internet? This is the real axis
    # between the modes: it decides whether a CLOUD LLM (Groq/OpenRouter) is
    # usable at all, or whether everything has to run on the box itself.
    network_available: bool
    # Sensible Neo4j default when the operator hasn't set NEO4J_URI. Sample and
    # custom both target a local sandbox; production has no safe default, so it
    # deliberately has none and must be supplied.
    default_neo4j_uri: Optional[str]
    embeddings_default: bool


MODE_SPECS: Dict[str, ModeSpec] = {
    SAMPLE: ModeSpec(
        name=SAMPLE,
        label='Sample (online)',
        description=(
            'The bundled VMware SDDC corpus in a local Neo4j, on a machine with '
            'internet access. Cloud LLMs (Groq, OpenRouter) are available as well '
            'as a local Ollama. Nothing to configure - the demo path.'
        ),
        source='bundled corpus (ingestion_pipeline/logs or a parsed output.json[.gz])',
        requires_kafka=False,
        network_available=True,
        default_neo4j_uri='bolt://localhost:7687',
        embeddings_default=True,
    ),
    PRODUCTION: ModeSpec(
        name=PRODUCTION,
        label='Production (airgapped)',
        description=(
            'Real Kafka brokers and a real Neo4j on an airgapped VM with no '
            'outbound internet. Runs the DNIF -> Kafka -> parse -> embed -> Neo4j '
            'realtime path. The LLM must be a local Ollama; cloud APIs are '
            'unreachable by definition.'
        ),
        source='DNIF API via Kafka (realtime producer/consumer roles)',
        requires_kafka=True,
        network_available=False,
        default_neo4j_uri=None,   # must be supplied - never guess a prod address
        embeddings_default=True,
    ),
    CUSTOM: ModeSpec(
        name=CUSTOM,
        label='Custom logs (online)',
        description=(
            'Upload your own log file - Syslog, JSON/JSONL, XML, CSV, CEF, LEEF, '
            'or any plain-text vendor format - and the same parser, entity '
            'extraction and embeddings build a graph from it.'
        ),
        source='user-uploaded file (see core/structured_readers.py)',
        requires_kafka=False,
        network_available=True,
        default_neo4j_uri='bolt://localhost:7687',
        embeddings_default=True,
    ),
}


def current_mode() -> str:
    """The active mode, from APP_MODE. An unrecognized value falls back to
    'sample' rather than raising: a typo in an env var should degrade to the
    safe, self-contained mode, not stop the service from starting."""
    raw = os.getenv('APP_MODE', SAMPLE).strip().lower()
    return raw if raw in ALL_MODES else SAMPLE


def set_mode(mode: str) -> str:
    """Switches the active mode for THIS PROCESS, at runtime.

    Sets APP_MODE in the process environment, which is the same source
    current_mode() reads - so the switch is immediately visible to every
    caller without a restart, and without a second source of truth to drift.

    Deliberately not persisted to .env: writing to a config file from an HTTP
    handler would mean a button press permanently changes what the service
    does on its next boot, including repointing an airgapped deployment at a
    Kafka/DNIF topology it cannot reach. A restart returns to whatever the
    operator actually configured, which is the safer default; the UI says so.

    Note this changes which mode the *control API* reports and validates.
    Long-running realtime producer/consumer processes read their own config
    at startup, so switching to 'production' here does not itself start them -
    the Modes page is explicit about that.
    """
    mode = (mode or '').strip().lower()
    if mode not in ALL_MODES:
        raise ValueError(f"Unknown mode {mode!r}. Valid: {sorted(ALL_MODES)}")
    os.environ['APP_MODE'] = mode
    return mode


def mode_spec(mode: Optional[str] = None) -> ModeSpec:
    return MODE_SPECS[mode if mode in MODE_SPECS else current_mode()]


def embeddings_enabled(mode: Optional[str] = None) -> bool:
    """Embeddings are on by default in every mode - vector search is what
    makes the chatbot's "why did X fail" questions work, and a graph ingested
    without them can only be fixed by a full re-embed pass. ENABLE_EMBEDDINGS
    still overrides explicitly, for the case where someone deliberately wants
    a fast structure-only load."""
    override = os.getenv('ENABLE_EMBEDDINGS')
    if override is not None and override.strip() != '':
        return override.strip().lower() in ('true', '1', 'yes')
    return mode_spec(mode).embeddings_default


def validate(mode: Optional[str] = None) -> List[str]:
    """Returns a list of human-readable problems that would stop this mode
    from working. Empty list means "good to go".

    This is what the API's /api/modes endpoint surfaces, so an operator sees
    "KAFKA_BOOTSTRAP_SERVERS is not set" in the UI instead of a container that
    starts fine and then silently ingests nothing.
    """
    spec = mode_spec(mode)
    problems: List[str] = []

    if not (os.getenv('NEO4J_URI') or spec.default_neo4j_uri):
        problems.append(
            'NEO4J_URI is not set, and production mode has no default - '
            'set it to your Neo4j address (bolt://host:7687).'
        )

    if spec.requires_kafka:
        if not os.getenv('KAFKA_BOOTSTRAP_SERVERS'):
            problems.append(
                'KAFKA_BOOTSTRAP_SERVERS is not set - production mode reads from '
                'Kafka (comma-separated host:port list).'
            )
        if not os.getenv('DNIF_API_URL') or 'your-dnif-server' in os.getenv('DNIF_API_URL', ''):
            problems.append(
                'DNIF_API_URL is unset or still the placeholder - the producer '
                'has nothing real to poll (it will replay local files instead).'
            )

    if spec.name == SAMPLE:
        # Sample mode is meant to be self-contained; a remote Neo4j still
        # works, it just isn't what this mode is for.
        uri = os.getenv('NEO4J_URI', spec.default_neo4j_uri or '')
        if uri and not any(h in uri for h in ('localhost', '127.0.0.1', 'sandbox-neo4j')):
            problems.append(
                f'Sample mode is pointed at a non-local Neo4j ({uri}). That works, '
                f'but sample mode is intended for a local sandbox - double-check '
                f'you are not about to write the sample corpus into production.'
            )

    return problems


# Which LLM providers each mode can actually reach. Ollama runs on the box, so
# it is always available; the cloud providers need outbound internet, which an
# airgapped deployment does not have.
LOCAL_LLM_PROVIDERS = ('ollama',)
CLOUD_LLM_PROVIDERS = ('groq', 'openrouter')


def allowed_llm_providers(mode: Optional[str] = None) -> List[str]:
    """Providers that are usable in this mode.

    In an airgapped deployment the cloud APIs are unreachable as a matter of
    network topology, not policy - this list is what lets the UI say so up
    front instead of offering a button that produces a timeout. The chatbot's
    own availability probe is still the backstop: if a cloud provider is
    selected anyway and cannot be reached, every stage degrades to its
    deterministic fallback exactly as before.
    """
    spec = mode_spec(mode)
    return list(LOCAL_LLM_PROVIDERS) + (list(CLOUD_LLM_PROVIDERS) if spec.network_available else [])


def describe(mode: Optional[str] = None) -> Dict[str, Any]:
    """Serializable description of a mode, for the API/frontend."""
    spec = mode_spec(mode)
    return {
        'mode': spec.name,
        'label': spec.label,
        'description': spec.description,
        'source': spec.source,
        'requires_kafka': spec.requires_kafka,
        'network_available': spec.network_available,
        'allowed_llm_providers': allowed_llm_providers(spec.name),
        'embeddings_enabled': embeddings_enabled(spec.name),
        'neo4j_uri': os.getenv('NEO4J_URI', spec.default_neo4j_uri),
        'problems': validate(spec.name),
    }


def describe_all() -> List[Dict[str, Any]]:
    return [describe(name) for name in ALL_MODES]
