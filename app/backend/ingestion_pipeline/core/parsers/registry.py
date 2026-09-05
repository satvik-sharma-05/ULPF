"""
parsers/registry.py - The ordered list of format detectors.

parser.py walks DETECTORS top to bottom and takes the first match. Order
matters: a specific/rare format must come before any generic pattern that
could otherwise shadow it (e.g. the NSX ESX-agent shape before the plain
`<proc>[<pid>]: <msg>` ESXi catch-all). generic_fallback is not in this
list - it always matches, so parser.py calls it only once nothing above
did.
"""

from .cef_leef import detect_cef, detect_leef
from .aria_automation import (
    detect_appliance_py_script_log,
    detect_aria_appliance_bracket,
    detect_aria_automation_springboot,
    detect_rabbitmq_log,
    detect_spring_actuator_access,
    detect_vracli,
    detect_vrlcm_classic_logback,
)
from .esxi import (
    detect_envoy_access,
    detect_esxi_bare_proc_message,
    detect_esxi_dump_continuation,
    detect_esxi_generic_proc,
    detect_esxi_originator,
    detect_esxupdate,
    detect_healthd_plugin_status,
    detect_vcenter_server,
    detect_vmkernel,
)
from .common_formats import (detect_bracket_level_app, detect_mysql_error,
                             detect_redis, detect_rfc5424, detect_sophos_kv,
                             detect_web_access, detect_zeek_conn)
from .logfmt import detect_logfmt
from .linux_syslog import detect_appliance_placeholder_syslog, detect_rfc3164_wrapper
from .cloud import (detect_aws_cloudtrail, detect_azure_activity,
                    detect_gcp_audit, detect_k8s_audit)
from .identity_endpoint import (detect_crowdstrike, detect_defender_alert,
                                detect_duo_auth, detect_okta_system_log)
from .network_devices import detect_fortinet_kv, detect_pri_syslog
from .nsx import detect_nsx_esx_agent, detect_nsx_manager
from .platform_services import (
    detect_bare_level_logger_log,
    detect_cfapi_trace,
    detect_coredns,
    detect_deployment_health_log,
    detect_jvm_gc,
    detect_k8s_access,
    detect_kubelet_glog,
    detect_app_logger,
    detect_envoy_upstream_access,
    detect_postgres_log,
    detect_postgres_native_log,
    detect_squid_access,
)
from .vrops_casa_horizon import (
    detect_apache_combined_bracket_first,
    detect_casa_ajp,
    detect_horizon_view_audit,
    detect_vcops_watchdog,
    detect_vrops_bridge,
    detect_vrops_profiling,
    detect_vrops_session_audit,
)
from .vmacore import detect_vmacore_appliance
from .windows_security import detect_windows_security_event

DETECTORS = [
    ('windows_security_event', detect_windows_security_event),
    # CEF/LEEF have an unambiguous 'CEF:'/'LEEF:' marker - safe to check before
    # anything else, and cheap since both bail on a simple substring miss.
    ('cef', detect_cef),
    ('leef', detect_leef),
    # Before every ESXi/syslog shape: an SRM record is an RFC3164 line whose
    # payload is a full vmacore record, so the generic wrapper would otherwise
    # claim it first and discard the inner severity/sub/opID.
    ('vmacore_appliance', detect_vmacore_appliance),
    ('nsx_manager', detect_nsx_manager),
    ('nsx_esx_agent', detect_nsx_esx_agent),
    ('esxi_originator', detect_esxi_originator),
    ('esxi_dump_continuation', detect_esxi_dump_continuation),
    ('healthd_plugin_status', detect_healthd_plugin_status),
    ('vmkernel', detect_vmkernel),
    ('envoy_access', detect_envoy_access),
    ('vcenter_server', detect_vcenter_server),
    ('esxupdate', detect_esxupdate),
    ('esxi_generic_proc', detect_esxi_generic_proc),
    ('esxi_bare_proc_message', detect_esxi_bare_proc_message),
    ('appliance_placeholder_syslog', detect_appliance_placeholder_syslog),
    ('vrops_bridge', detect_vrops_bridge),
    ('vrops_profiling', detect_vrops_profiling),
    ('casa_ajp', detect_casa_ajp),
    ('vrops_session_audit', detect_vrops_session_audit),
    ('horizon_view_audit', detect_horizon_view_audit),
    ('spring_actuator_access', detect_spring_actuator_access),
    ('aria_automation_springboot', detect_aria_automation_springboot),
    ('vrlcm_classic_logback', detect_vrlcm_classic_logback),
    ('vcops_watchdog', detect_vcops_watchdog),
    ('aria_appliance_bracket', detect_aria_appliance_bracket),
    ('rabbitmq_log', detect_rabbitmq_log),
    ('appliance_py_script_log', detect_appliance_py_script_log),
    ('vracli', detect_vracli),
    # Before jvm_gc, which also anchors on a bracketed timestamp.
    ('envoy_upstream_access', detect_envoy_upstream_access),
    ('jvm_gc', detect_jvm_gc),
    ('cfapi_trace', detect_cfapi_trace),
    ('coredns', detect_coredns),
    ('kubelet_glog', detect_kubelet_glog),
    ('squid_access', detect_squid_access),
    ('k8s_access', detect_k8s_access),
    ('postgres_log', detect_postgres_log),
    ('postgres_native_log', detect_postgres_native_log),
    ('web_access', detect_web_access),
    ('mysql_error', detect_mysql_error),
    ('redis', detect_redis),
    ('zeek_conn', detect_zeek_conn),
    ('apache_combined_bracket_first', detect_apache_combined_bracket_first),
    ('bare_level_logger_log', detect_bare_level_logger_log),
    # Broad by design - the stock Logback/Log4j2 layout - so it sits
    # below every application detector that knows its own product.
    ('app_logger', detect_app_logger),
    ('bracket_level_app', detect_bracket_level_app),
    ('deployment_health_log', detect_deployment_health_log),
    ('rfc3164_wrapper', detect_rfc3164_wrapper),
    # Cloud audit records. Placed early: each is identified by a field
    # unique to its provider, so they cannot steal another format's lines.
    ('aws_cloudtrail', detect_aws_cloudtrail),
    ('azure_activity', detect_azure_activity),
    ('gcp_audit', detect_gcp_audit),
    ('k8s_audit', detect_k8s_audit),
    # Identity providers and EDR. JSON, each identified by a field unique
    # to its vendor, so none can steal another's records.
    ('okta_system_log', detect_okta_system_log),
    ('duo_auth', detect_duo_auth),
    ('crowdstrike_falcon', detect_crowdstrike),
    ('defender_alert', detect_defender_alert),
    # Perimeter appliances, last: both match a broad shape, so every
    # platform-specific detector above keeps first refusal.
    # BEFORE pri_syslog: that detector matches an RFC 5424 line too, and
    # reads the version digit as the hostname. Ordering is the fix.
    ('rfc5424', detect_rfc5424),
    # Self-identifying key=value appliance bags, before the broader ones.
    ('sophos_kv', detect_sophos_kv),
    ('fortinet_kv', detect_fortinet_kv),
    # AFTER fortinet_kv, not before. Both match key=value lines and
    # Fortinet also emits a `msg=` field, so logfmt placed first claimed
    # FortiGate traffic logs - a named detector, the wrong one, and
    # invisible to a test that only asks whether something matched.
    ('logfmt', detect_logfmt),
    ('pri_syslog', detect_pri_syslog),
]


# Detectors that take a JSON record rather than a log LINE.
#
# parse() short-circuits anything that looks like JSON straight to the generic
# structural reader, which meant a registered JSON detector could never fire -
# CloudTrail, Azure, GCP and the Kubernetes audit log all came out as
# `json_structure` with no hostname and no severity. These are tried first for
# a JSON record; the structural reader stays as the fallback, which is the
# correct order everywhere else in this registry too.
JSON_DETECTORS = [
    (name, fn) for name, fn in DETECTORS
    if name in ('aws_cloudtrail', 'azure_activity', 'gcp_audit', 'k8s_audit',
                'okta_system_log', 'duo_auth', 'crowdstrike_falcon',
                'defender_alert')
]
