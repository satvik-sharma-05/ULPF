"""
core/parsers/entities.py - Turns a parsed log into typed graph entities.

A flat list of strings under one :Entity label only ever supports "this log
mentions this string". Everything interesting in this corpus is *typed*: an
opID is a correlation id, a naa.* string is a storage device, `user=vpxuser:
VSPHERE.LOCAL\\svc` is an account in a domain, `ebs-app-6f577dd6c-ttv86` is a
k8s pod, `vmhba1` is an HBA. This module recovers those types so
neo4j_writer.py can build a graph you can actually ask questions of (see
core/graph_schema.py for the type -> label map).

Two extraction passes, cheapest first:
  1. attribute-driven - the format detectors already parsed structured bags
     (nsx@6876 k="v", Originator@6876 sub=/opID=/sid=/user=, auditd msg='...',
     Windows liagent sections). A dict lookup beats re-scanning text, and the
     key names carry the meaning, so this is both faster and more precise.
  2. text sweeps - only the patterns that genuinely only appear in free text
     (naa.* devices, morefs, vim API methods, task ids, UUIDs, paths, ...).

Everything is deduplicated and capped per log: a single Windows event or an
ESXi fault dump can name the same SID or path dozens of times, and that noise
would otherwise dominate the graph.

Precision matters more than recall here. A wrong edge is worse than a missing
one - it produces a confident, wrong answer downstream - so each pattern below
is anchored to a shape that only ever means one thing in this corpus, and
anything ambiguous is deliberately left out.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Caps per log. A fault dump can name hundreds of devices; the first handful
# carries the signal and the rest only inflate the graph.
MAX_ENTITIES_PER_LOG = 60
MAX_PER_TYPE = 8

# ---------------------------------------------------------------------------
# 1. Attribute-key -> entity-type map. Matched case-insensitively against the
#    `attributes` bag every detector produces, so one entry covers NSX's reqId,
#    ESXi's opID, Aria's trace, auditd's acct, and so on.
# ---------------------------------------------------------------------------
_ATTR_ENTITY_MAP: Dict[str, Optional[str]] = {
    # correlation / work tracking
    'opid': 'operation',
    'operationid': 'operation',
    'reqid': 'operation',
    'requestid': 'operation',
    'request_id': 'operation',
    'trace': 'trace',
    'request-trace': 'trace',
    'traceid': 'trace',
    'tid': 'thread',
    'thread': 'thread',
    'logger': 'logger',
    # identity
    'user': 'user',
    'username': 'user',
    'acct': 'user',
    'target_user': 'user',
    'by_user': 'user',
    'parent_user': 'user',
    'access_key': 'user',
    'accesskey': 'user',
    'userid': 'security_id',
    'subj': 'apparmor_profile',
    'sid': 'session',
    'ses': 'session',
    'sessionid': 'session',
    'session': 'session',
    # network
    'client': 'ip',
    'client_ip': 'ip',
    'remotehost': 'ip',
    'addr': 'ip',
    'src': 'ip',
    # CEF/LEEF common extension keys (src/suser already covered above/below;
    # these are the destination-side and identity counterparts every CEF/LEEF
    # producer uses - QRadar, ArcSight, Splunk CIM, most vendor SIEM exports).
    'dst': 'ip',
    'dvc': 'ip',
    'suser': 'user',
    'duser': 'user',
    'usrname': 'user',
    'spt': 'port',
    'dpt': 'port',
    'srcport': 'port',
    'dstport': 'port',
    'fname': 'file',
    'filepath': 'file',
    'request': 'url',
    'http_path': 'endpoint',
    'qname': 'dns_name',
    # storage / vsphere
    'bucket': 'bucket',
    'object': 'object_key',
    # linux audit
    'exe': 'executable',
    'command': None,          # kept as a Log attribute; the exe is the entity
    'syscall': 'syscall',
    'profile': 'apparmor_profile',
    'comm': 'executable',
    'pam_service': 'systemd_unit',
    # diagnostics
    'errorcode': 'error_code',
    'error_code': 'error_code',
    'event_id': 'event_id',
}

# Values that are placeholders rather than data. auditd writes 4294967295 for an
# unset uid, '?' for an absent hostname, and vRLI writes '-'.
_NULL_VALUES = {'', '-', '?', 'none', 'null', 'unknown', '(null)', 'n/a',
                '4294967295', 'unset', '<none>', 'unconfined', 'true', 'false'}

# ---------------------------------------------------------------------------
# 2. Text-sweep patterns.
# ---------------------------------------------------------------------------
# naa.60014380343f89ed00f1000000000000 / eui.* / t10.* / mpx.vmhba0:C0:T0:L0
_RE_DEVICE = re.compile(r'\b((?:naa|eui|t10|mpx)\.[0-9a-zA-Z:._\-]{4,})')
# vSphere managed object references: host-74, vm-1203, task-189715, group-d1
_RE_MOREF = re.compile(
    r'\b((?:host|vm|task|group|datastore|network|resgroup|dvportgroup)-[a-zA-Z]?\d+)\b'
)
# haTask--vim.vslm.host.CatalogSyncManager.queryCatalogChange-10516006 / Task-25792508
_RE_TASK = re.compile(r'\b(haTask[\w.\-]*-\d+|Task-\d+|TaskQueue-\d+)\b')
# vim.vslm.host.CatalogSyncManager.queryCatalogChange (dotted vim/vmodl API path)
_RE_API_METHOD = re.compile(r'\b((?:vim|vmodl|vpxapi|internalvim)\.[A-Za-z][\w.]{4,})')
# ESXi NICs/HBAs/vmkernel ports: vmnic0, vmk3, vmhba64
_RE_INTERFACE = re.compile(r'\b((?:vmnic|vmk|vmhba|eth|ens|eno|bond)\d{1,3})\b')
# MAC addresses, colon- or hyphen-separated
_RE_MAC = re.compile(r'\b((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})\b')
# Canonical 8-4-4-4-12 UUID
_RE_UUID = re.compile(
    r'\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b'
)
# systemd units: sshd.service, docker.socket, vmware-vpxd.target
_RE_SYSTEMD_UNIT = re.compile(
    r'\b([\w@.\-]+\.(?:service|socket|target|timer|mount|path|slice))\b'
)
# ESXi VIB names: VMware_bootbank_esx-base_8.0.2-0.0.12345
_RE_VIB = re.compile(r'\b([A-Za-z][\w\-]*_(?:bootbank|locker)_[\w\-.]+)\b')
# Absolute unix paths under directories that actually appear in this corpus
_RE_UNIX_PATH = re.compile(
    r'(?<![\w:])(/(?:vmfs|var|etc|usr|opt|tmp|proc|home|bin|sbin|lib|dev)/[^\s,;:"\'\]\)]{2,120})'
)
_RE_WINDOWS_PATH = re.compile(r'\b([A-Za-z]:\\\\?[^\s,;"\'\]\)]{3,120})')
# Datastore names: "[DatastoreName] vmname.vmx" or /vmfs/volumes/<name>
_RE_DATASTORE_BRACKET = re.compile(r'\[([A-Za-z][\w \-.]{2,40})\]\s+\S+\.vmx')
_RE_DATASTORE_VOLUME = re.compile(r'/vmfs/volumes/([\w\-.]{4,60})')
_RE_URL = re.compile(r'\b((?:https?|amqp|bolt|ftp)://[^\s,;"\'\]\)<>]{4,200})')
# Kubernetes DNS: redis-node-1.redis-headless.<namespace>.svc.cluster.local
_RE_K8S_DNS = re.compile(r'\b([\w\-.]+)\.([\w\-]+)\.svc\.cluster\.local\b')
# k8s pod names: ebs-app-6f577dd6c-ttv86, abx-service-app-7f9fcf8-mfbf2
_RE_POD_NAME = re.compile(r'^([a-z0-9]([a-z0-9\-]{2,50})-[a-z0-9]{5,10}-[a-z0-9]{5})$')
_RE_SID = re.compile(r'\b(S-1-(?:\d+-){1,8}\d+)\b')
_RE_IP_PORT = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?\b')
_RE_GC = re.compile(r'\bGC\((\d+)\)')
# AMQP queue names Aria uses: com.vmware.automation.<...>-qq
_RE_QUEUE = re.compile(r'\b((?:com\.vmware\.[\w.\-]+|[\w.\-]+)-(?:qq|exchange|queue))\b')
# Vendor error codes: MPA11004, NSX12345
_RE_ERROR_CODE = re.compile(r'\b([A-Z]{2,6}\d{4,6})\b')
# Container ids as CRI logs them: containerd://<hex> or docker://<hex>
_RE_CONTAINER = re.compile(r'\b(?:containerd|docker)://([0-9a-f]{12,64})\b')

# Loopback/broadcast carry no cross-host meaning, so they are not graph edges.
_UNINTERESTING_IPS = {'0.0.0.0', '127.0.0.1', '255.255.255.255', '::1'}
# An all-zero or broadcast MAC is a placeholder, not a device.
_UNINTERESTING_MACS = {'00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff'}


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip('"\'')
    if not text or text.lower() in _NULL_VALUES or len(text) > 300:
        return None
    return text


def _split_user(raw: str) -> List[Tuple[str, str]]:
    """ESXi writes `vpxuser:VSPHERE.LOCAL\\vpxd-extension-5bba...`, Windows
    writes `NTRONET\\MEGH-SCAN-WS19$`, Aria writes `user@domain`. All three mean
    "account, in a domain" - so yield the account and the domain separately
    rather than one opaque blob no query could ever join on."""
    out: List[Tuple[str, str]] = []
    text = raw
    # Strip an ESXi "authenticator:" prefix (vpxuser:DOMAIN\user).
    if ':' in text and '\\' in text.split(':', 1)[1]:
        authenticator, text = text.split(':', 1)
        if authenticator.strip():
            out.append(('user', authenticator.strip()))
    if '\\' in text:
        domain, _, account = text.rpartition('\\')
        domain = domain.strip().lstrip('\\')
        if domain:
            out.append(('domain', domain))
        if account.strip():
            out.append(('user', account.strip()))
    elif '@' in text and text.count('@') == 1:
        account, _, domain = text.partition('@')
        if account:
            out.append(('user', account))
        if domain:
            out.append(('domain', domain))
    elif text:
        out.append(('user', text))
    return out


# Windows Security section keys ("Account Name", "Logon ID", ...) normalized to
# the same lowercase, separator-free form the map above uses.
_WINDOWS_KEY_ALIASES: Dict[str, Optional[str]] = {
    'accountname': 'user',
    'accountdomain': 'domain',
    'securityid': 'security_id',
    'logonid': 'session',
    'processname': 'executable',
    'processid': None,        # a bare pid is already a Log property
    'objectserver': None,
    'objectname': 'file',
    'sourcenetworkaddress': 'ip',
    'sourceport': 'port',
    'workstationname': 'user',
    'targetusername': 'user',
    'targetdomainname': 'domain',
    'subjectusername': 'user',
    'subjectdomainname': 'domain',
    'servicename': 'systemd_unit',
    'sharename': 'file',
}


def _map_attribute(key_lower: str, value: Any) -> Iterable[Tuple[str, str]]:
    cleaned = _clean(value)
    if cleaned is None:
        return

    compact = key_lower.replace(' ', '').replace('_', '').replace('-', '')
    if key_lower in _ATTR_ENTITY_MAP:
        entity_type = _ATTR_ENTITY_MAP[key_lower]
    elif compact in _WINDOWS_KEY_ALIASES:
        entity_type = _WINDOWS_KEY_ALIASES[compact]
    else:
        entity_type = _ATTR_ENTITY_MAP.get(compact)
    if entity_type is None:
        return

    if entity_type == 'user':
        yield from _split_user(cleaned)
        return
    if entity_type == 'ip':
        # `client` can be "10.101.26.3:55658", a bare IP, or a hostname.
        match = _RE_IP_PORT.search(cleaned)
        if match:
            if match.group(1) not in _UNINTERESTING_IPS:
                yield ('ip', match.group(1))
            if match.group(2):
                yield ('port', match.group(2))
        return
    if entity_type == 'apparmor_profile' and cleaned.lower().startswith('unconfined'):
        return
    yield (entity_type, cleaned)


def _walk_attributes(attributes: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
    """Yields (entity_type, value) from the detector's attribute bag, including
    one level of nesting - Windows events nest their Subject:/Object:/Process
    Information: blocks under `sections`."""
    for key, value in (attributes or {}).items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                yield from _map_attribute(str(sub_key).lower(), sub_value)
        elif not isinstance(value, (list, tuple)):
            yield from _map_attribute(str(key).lower(), value)


def _sweep_text(text: str) -> Iterable[Tuple[str, str]]:
    """Regex passes over the message body for what no detector put in a bag."""
    for match in _RE_DEVICE.finditer(text):
        yield ('device', match.group(1))
    for match in _RE_TASK.finditer(text):
        yield ('task', match.group(1))
    for match in _RE_MOREF.finditer(text):
        moref = match.group(1)
        # A vm-NNN moref is specifically a virtual machine; everything else in
        # this family (host-74, group-d1, ...) stays a generic managed object.
        yield ('vm' if moref.startswith('vm-') else 'managed_object', moref)
    for match in _RE_API_METHOD.finditer(text):
        yield ('api_method', match.group(1))
    for match in _RE_SID.finditer(text):
        yield ('security_id', match.group(1))
    for match in _RE_UUID.finditer(text):
        yield ('uuid', match.group(1))
    for match in _RE_INTERFACE.finditer(text):
        yield ('network_interface', match.group(1))
    for match in _RE_MAC.finditer(text):
        mac = match.group(1).lower().replace('-', ':')
        if mac not in _UNINTERESTING_MACS:
            yield ('mac_address', mac)
    for match in _RE_SYSTEMD_UNIT.finditer(text):
        yield ('systemd_unit', match.group(1))
    for match in _RE_VIB.finditer(text):
        yield ('package', match.group(1))
    for match in _RE_CONTAINER.finditer(text):
        yield ('container', match.group(1))
    for match in _RE_GC.finditer(text):
        yield ('gc_cycle', match.group(1))
    for match in _RE_ERROR_CODE.finditer(text):
        yield ('error_code', match.group(1))
    for match in _RE_QUEUE.finditer(text):
        yield ('queue', match.group(1))
    for match in _RE_URL.finditer(text):
        yield ('url', match.group(1).rstrip('.,;'))
    for match in _RE_DATASTORE_VOLUME.finditer(text):
        yield ('datastore', match.group(1))
    for match in _RE_DATASTORE_BRACKET.finditer(text):
        yield ('datastore', match.group(1))
    for match in _RE_UNIX_PATH.finditer(text):
        yield ('file', match.group(1).rstrip('.,;'))
    for match in _RE_WINDOWS_PATH.finditer(text):
        yield ('file', match.group(1).rstrip('.,;'))
    for match in _RE_K8S_DNS.finditer(text):
        yield ('dns_name', match.group(0))
        yield ('namespace', match.group(2))
        service = match.group(1).split('.')[-1]
        if service:
            yield ('k8s_service', service)
    for match in _RE_IP_PORT.finditer(text):
        if match.group(1) not in _UNINTERESTING_IPS:
            yield ('ip', match.group(1))


def extract_entities(
    hostname: Optional[str],
    process: Optional[str],
    message: str,
    attributes: Dict[str, Any],
    detector_entities: Optional[List[Any]] = None,
) -> List[Dict[str, str]]:
    """Builds the typed entity list attached to every parsed log.

    `detector_entities` is whatever a format detector already flagged as
    notable; entries may be bare strings (legacy style) or typed dicts.
    """
    found: List[Tuple[str, str]] = []

    # For the Aria/k8s logs `hostname` is the pod, not a machine. The process
    # name is then the workload behind it (event-broker, abx-service), which is
    # what links replicas together - pod names are per-replica and churn on
    # every redeploy, so the service is the durable join key.
    if hostname and _RE_POD_NAME.match(hostname):
        found.append(('pod', hostname))
        if process:
            found.append(('k8s_service', process))

    for item in (detector_entities or []):
        if isinstance(item, dict):
            item_type = item.get('type')
            item_value = _clean(item.get('value'))
            if item_type and item_value:
                found.append((item_type, item_value))
            continue
        # Legacy untyped output: only unambiguously-shaped values are kept.
        # Guessing a type from a bare string would file an opID as a username,
        # and everything these lists hold is already captured - correctly typed
        # - from the attribute bag by _walk_attributes below.
        cleaned = _clean(item)
        if not cleaned:
            continue
        ip_port_match = _RE_IP_PORT.fullmatch(cleaned)
        if ip_port_match:
            # _RE_IP_PORT's own pattern includes an optional ":port" suffix, so
            # fullmatch succeeds on "10.244.0.230:55000" as a WHOLE string, not
            # just on a bare IP - group(1)/group(2) must be used to split them,
            # or the ip entity ends up storing "address:port" together, which
            # fails validate_extraction's shape check for :IPAddress and can
            # never join with the same host's IP recorded elsewhere as bare.
            ip_value = ip_port_match.group(1)
            if ip_value not in _UNINTERESTING_IPS:
                found.append(('ip', ip_value))
            if ip_port_match.group(2):
                found.append(('port', ip_port_match.group(2)))
        elif _RE_SID.fullmatch(cleaned):
            found.append(('security_id', cleaned))

    found.extend(_walk_attributes(attributes))
    if message:
        found.extend(_sweep_text(message))
    # A few detectors (esxi.py's envoy-access, aria_automation.py) stash
    # leftover free text in attributes['tail'] rather than folding it into
    # `message` - e.g. envoy-access's source/destination IP:PORT pairs live
    # only here. _walk_attributes only maps attribute *keys* to types, so
    # without this, `tail`'s own content (IPs, paths, UUIDs, ...) was never
    # swept and those entities silently never made it into the graph.
    tail = attributes.get('tail')
    if tail:
        found.extend(_sweep_text(tail))

    # Deduplicate, cap per type, then cap overall. Stable ordering matters:
    # ingestion must be idempotent, so the same log has to yield the same
    # entity list (and therefore the same edges) on every run.
    seen: Set[Tuple[str, str]] = set()
    per_type: Dict[str, int] = {}
    result: List[Dict[str, str]] = []
    for entity_type, value in found:
        value = value.strip()
        if not value or value.lower() in _NULL_VALUES or len(value) > 200:
            continue
        key = (entity_type, value)
        if key in seen or per_type.get(entity_type, 0) >= MAX_PER_TYPE:
            continue
        seen.add(key)
        per_type[entity_type] = per_type.get(entity_type, 0) + 1
        result.append({'type': entity_type, 'value': value})
        if len(result) >= MAX_ENTITIES_PER_LOG:
            break
    return result
