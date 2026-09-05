"""
core/graph_schema.py - Single source of truth for the Neo4j graph model.

The parser emits typed entities (``{'type': 'user', 'value': 'root'}``) and this
module is the only place that decides what a given entity *type* becomes in the
graph: which node label, which relationship, which property holds the key.
neo4j_writer.py builds the graph from these tables, so adding an entity kind is
a one-line change here plus wherever the parser extracts it.

Three layers of structure:

  CORE          - Host/Process/Component/SourceType/Severity/Day. Every log gets
                  all of them.
  ENTITY_SPECS  - (:Log)-[:REL]->(:TypedNode). What the log *mentions*.
  DERIVED       - entity-to-entity and entity-to-host edges. This is the layer
                  that makes the graph a graph rather than a star of logs: it
                  records that a device is attached to a host, a session belongs
                  to a user, a pod is in a namespace. Without it, every path
                  between two facts has to detour through a :Log node.

A deliberate omission: low-cardinality values such as HTTP status codes stay as
:Log properties rather than nodes. A (:HttpStatus {code:200}) over a corpus this
size would accumulate tens of millions of relationships - a supernode that slows
every traversal touching it, and answers nothing a property can't.
"""

from typing import Dict, List, NamedTuple, Tuple


class EntitySpec(NamedTuple):
    label: str          # Neo4j node label
    relationship: str   # (:Log)-[:REL]->(:Label)
    key: str            # property holding the unique value
    description: str    # used to build the text-to-Cypher schema prompt


# ---------------------------------------------------------------------------
# Entity types the parser can emit. Keys match the `type` field of the dicts in
# a parsed log's `entities` list.
# ---------------------------------------------------------------------------
ENTITY_SPECS: Dict[str, EntitySpec] = {
    # --- identity / access ---------------------------------------------------
    'user': EntitySpec('User', 'PERFORMED_BY', 'name',
                       'A user or service account that performed the logged action'),
    'security_id': EntitySpec('SecurityID', 'HAS_SECURITY_ID', 'sid',
                              'A Windows SID (e.g. S-1-5-18)'),
    'domain': EntitySpec('Domain', 'IN_DOMAIN', 'name',
                         'An authentication domain (NTRONET, VSPHERE.LOCAL)'),
    'session': EntitySpec('Session', 'IN_SESSION', 'id',
                          'A session id (ESXi sid=, auditd ses=, Windows Logon ID)'),

    # --- network -------------------------------------------------------------
    'ip': EntitySpec('IPAddress', 'INVOLVES_IP', 'address',
                     'An IPv4 address appearing in the log'),
    'port': EntitySpec('Port', 'USES_PORT', 'number', 'A TCP/UDP port number'),
    'endpoint': EntitySpec('Endpoint', 'REQUESTED', 'path',
                           'An HTTP path/endpoint that was requested'),
    'url': EntitySpec('URL', 'REQUESTED_URL', 'url', 'A full URL'),
    'dns_name': EntitySpec('DnsName', 'RESOLVED', 'name',
                           'A DNS name that was queried or resolved'),
    'mac_address': EntitySpec('MacAddress', 'INVOLVES_MAC', 'address',
                              'A hardware MAC address'),
    'network_interface': EntitySpec('NetworkInterface', 'USES_INTERFACE', 'name',
                                    'A NIC or HBA (vmnic0, vmk0, vmhba1)'),

    # --- vSphere / storage ---------------------------------------------------
    'device': EntitySpec('Device', 'REFERENCES_DEVICE', 'id',
                         'A storage device, usually a naa.* SCSI identifier'),
    'datastore': EntitySpec('Datastore', 'REFERENCES_DATASTORE', 'name',
                            'A vSphere datastore'),
    'vm': EntitySpec('VM', 'REFERENCES_VM', 'moref',
                     'A virtual machine, by managed-object reference or name'),
    'managed_object': EntitySpec('ManagedObject', 'REFERENCES_OBJECT', 'moref',
                                 'A vSphere managed object reference (host-74, vm-12)'),
    'api_method': EntitySpec('ApiMethod', 'INVOKED', 'name',
                             'A vSphere/vim API method that was invoked'),
    'package': EntitySpec('Package', 'REFERENCES_PACKAGE', 'name',
                          'An installed VIB/package name'),

    # --- work tracking -------------------------------------------------------
    'task': EntitySpec('Task', 'ABOUT_TASK', 'id',
                       'A task identifier (haTask-*, Task-123, task-456)'),
    'operation': EntitySpec('Operation', 'PART_OF_OPERATION', 'id',
                            'An operation/request correlation id (opID, operationID, reqId)'),
    'trace': EntitySpec('Trace', 'HAS_TRACE', 'id',
                        'A distributed-tracing id linking logs of one request'),
    'uuid': EntitySpec('UUID', 'REFERENCES_UUID', 'value',
                       'A UUID appearing in the log - often a VM, session or object id'),
    'thread': EntitySpec('Thread', 'ON_THREAD', 'name',
                         'The thread that produced the log line'),
    'logger': EntitySpec('Logger', 'FROM_LOGGER', 'name',
                         'The application logger/class that emitted the line'),

    # --- kubernetes / cloud --------------------------------------------------
    'pod': EntitySpec('Pod', 'ON_POD', 'name', 'A Kubernetes pod'),
    'namespace': EntitySpec('Namespace', 'IN_NAMESPACE', 'name', 'A Kubernetes namespace'),
    'k8s_service': EntitySpec('K8sService', 'TARGETS_SERVICE', 'name', 'A Kubernetes service'),
    'container': EntitySpec('Container', 'IN_CONTAINER', 'id', 'A container id'),

    # --- files / processes ---------------------------------------------------
    'file': EntitySpec('File', 'TOUCHED_FILE', 'path', 'A file path referenced by the log'),
    'executable': EntitySpec('Executable', 'EXECUTED', 'path', 'An executed binary'),
    'syscall': EntitySpec('Syscall', 'USED_SYSCALL', 'name',
                          'A Linux syscall from an audit record'),
    'apparmor_profile': EntitySpec('ApparmorProfile', 'UNDER_PROFILE', 'name',
                                   'An AppArmor profile named in a kernel audit record'),
    'systemd_unit': EntitySpec('SystemdUnit', 'ABOUT_UNIT', 'name',
                               'A systemd unit/service name'),

    # --- object storage / messaging -----------------------------------------
    'bucket': EntitySpec('Bucket', 'ACCESSED_BUCKET', 'name', 'An S3/MinIO bucket'),
    'object_key': EntitySpec('ObjectKey', 'ACCESSED_OBJECT', 'key', 'An S3/MinIO object key'),
    'queue': EntitySpec('Queue', 'USED_QUEUE', 'name', 'An AMQP/RabbitMQ queue or exchange'),

    # --- diagnostics ---------------------------------------------------------
    'error_code': EntitySpec('ErrorCode', 'HAS_ERROR_CODE', 'code',
                             'A vendor error code such as MPA11004'),
    'event_id': EntitySpec('EventID', 'HAS_EVENT_ID', 'id',
                           'A Windows Event Log event id (4624, 4658, ...)'),
    'gc_cycle': EntitySpec('GCCycle', 'IN_GC_CYCLE', 'id',
                           'A JVM garbage-collection cycle number'),
}


# ---------------------------------------------------------------------------
# Derived edges: entity -> entity, or entity -> the host that emitted the log.
# '@host' is the special target meaning "the (:Host) of the current log".
#
# Only the FIRST entity of each type in a log takes part in entity->entity
# edges. A log with 8 users and 8 devices would otherwise produce 64 edges of a
# single type, none of which the log actually supports - whereas the common case
# (one user, one session, one device per line) is exactly right.
# ---------------------------------------------------------------------------
DerivedRel = Tuple[str, str, str]   # (from_entity_type, RELATIONSHIP, to_entity_type|'@host')

DERIVED_RELATIONSHIPS: List[DerivedRel] = [
    # identity
    ('user',              'ACCESSED',      '@host'),
    ('user',              'MEMBER_OF',     'domain'),
    ('session',           'BELONGS_TO',    'user'),
    ('security_id',       'IDENTIFIES',    'user'),
    # infrastructure attachment
    ('device',            'ATTACHED_TO',   '@host'),
    ('datastore',         'MOUNTED_ON',    '@host'),
    ('vm',                'HOSTED_ON',     '@host'),
    ('network_interface', 'INTERFACE_OF',  '@host'),
    ('mac_address',       'ASSIGNED_TO',   'network_interface'),
    ('package',           'INSTALLED_ON',  '@host'),
    ('systemd_unit',      'MANAGED_ON',    '@host'),
    ('apparmor_profile',  'ENFORCED_ON',   '@host'),
    # execution
    ('task',              'EXECUTED_ON',   '@host'),
    ('executable',        'RAN_ON',        '@host'),
    # SEEN_ON, not STARTED_ON: one operation id legitimately spans several
    # hosts, so claiming it originated on any single one would be wrong.
    ('operation',         'SEEN_ON',       '@host'),
    # kubernetes
    ('pod',               'IN_NAMESPACE',  'namespace'),
    ('pod',               'OF_SERVICE',    'k8s_service'),
    ('container',         'RUNS_IN',       'pod'),
    # object storage
    ('object_key',        'STORED_IN',     'bucket'),
]


# ---------------------------------------------------------------------------
# Structural (non-entity) parts of the graph, written for every single log.
# ---------------------------------------------------------------------------
CORE_NODES = {
    'Log': 'id',
    'Host': 'name',
    'Process': 'name',
    'Component': 'name',
    'SourceType': 'name',
    'Severity': 'level',
    'Day': 'date',
}

CORE_RELATIONSHIPS = [
    ('Host', 'EMITTED', 'Log', 'the host that produced the log'),
    ('Log', 'HAS_SEVERITY', 'Severity', 'the normalized severity of the log'),
    ('Log', 'FROM_SOURCE', 'SourceType', 'the platform family the log came from'),
    ('Log', 'ON_DAY', 'Day', 'the calendar day the log was written'),
    ('Log', 'EMITTED_BY', 'Process', 'the daemon/service that wrote the log'),
    ('Log', 'FROM_COMPONENT', 'Component', 'the sub-component within the process'),
    ('Process', 'RUNS_ON', 'Host', 'the host a process runs on'),
    ('Component', 'PART_OF', 'Process', 'the process a component belongs to'),
    ('Host', 'IS_TYPE', 'SourceType', 'the platform family of a host'),
]

# Uniqueness constraints + lookup indexes created on connect. Every label the
# writer MERGEs on needs one: without a uniqueness constraint Neo4j has no index
# to look the node up by, so each MERGE degrades to a full label scan and bulk
# ingest slows down as the graph grows. ":Entity" is included because
# entity_spec() falls back to it for any type not declared above.
CONSTRAINTS = (
    [f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
     for label, key in CORE_NODES.items()]
    + [f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{spec.label}) REQUIRE n.{spec.key} IS UNIQUE"
       for spec in ENTITY_SPECS.values()]
    + ["CREATE CONSTRAINT IF NOT EXISTS FOR (n:Entity) REQUIRE n.name IS UNIQUE"]
)

INDEXES = [
    "CREATE INDEX IF NOT EXISTS FOR (l:Log) ON (l.timestamp)",
    "CREATE INDEX IF NOT EXISTS FOR (l:Log) ON (l.severity)",
    "CREATE INDEX IF NOT EXISTS FOR (l:Log) ON (l.source_type)",
    "CREATE INDEX IF NOT EXISTS FOR (l:Log) ON (l.hostname)",
    "CREATE INDEX IF NOT EXISTS FOR (l:Log) ON (l.matched_format)",
    "CREATE INDEX IF NOT EXISTS FOR (l:Log) ON (l.severity_score)",
    "CREATE FULLTEXT INDEX log_message_fulltext IF NOT EXISTS FOR (l:Log) ON EACH [l.message]",
]


def entity_spec(entity_type: str) -> EntitySpec:
    """Spec for a parser entity type; unknown types fall back to a generic
    :Entity node so a newly-added extractor can never silently drop data."""
    return ENTITY_SPECS.get(
        entity_type,
        EntitySpec('Entity', 'INVOLVES', 'name', 'An unclassified extracted entity'),
    )


def schema_description() -> str:
    """Human/LLM-readable schema summary. The chatbot can also introspect the
    live database; this is the offline description of what the writer builds."""
    lines = ["NODE LABELS:"]
    for label, key in CORE_NODES.items():
        lines.append(f"  (:{label} {{{key}}})")
    for spec in ENTITY_SPECS.values():
        lines.append(f"  (:{spec.label} {{{spec.key}}})  // {spec.description}")

    lines += [
        "",
        "Log node properties: id, timestamp, day, hostname, source_type, process, pid,",
        "  component, subcomponent, severity, severity_score, message, normalized_message,",
        "  attributes_json, confidence, matched_format, raw_message, embedding,",
        "  http_method, http_status  (the last two only on access-log records)",
        "",
        "RELATIONSHIPS:",
    ]
    for start, rel, end, doc in CORE_RELATIONSHIPS:
        lines.append(f"  (:{start})-[:{rel}]->(:{end})  // {doc}")

    seen = set()
    for spec in ENTITY_SPECS.values():
        if (spec.relationship, spec.label) in seen:
            continue
        seen.add((spec.relationship, spec.label))
        lines.append(f"  (:Log)-[:{spec.relationship}]->(:{spec.label})  // {spec.description}")

    lines.append("")
    lines.append("DERIVED RELATIONSHIPS (entity to entity / entity to host):")
    for from_type, rel, to_type in DERIVED_RELATIONSHIPS:
        from_label = entity_spec(from_type).label
        to_label = 'Host' if to_type == '@host' else entity_spec(to_type).label
        lines.append(f"  (:{from_label})-[:{rel}]->(:{to_label})")
    return "\n".join(lines)
