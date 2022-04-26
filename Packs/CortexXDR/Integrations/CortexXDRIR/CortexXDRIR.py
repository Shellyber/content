import copy
import hashlib
import secrets
import string
import traceback
from datetime import timezone
from operator import itemgetter
from typing import Any, Dict, Tuple

import dateparser
import demistomock as demisto  # noqa: F401
import urllib3
from CommonServerPython import *  # noqa: F401
from CoreIRApiModule import *

# Disable insecure warnings
urllib3.disable_warnings()

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
NONCE_LENGTH = 64
API_KEY_LENGTH = 128

INTEGRATION_CONTEXT_BRAND = 'PaloAltoNetworksXDR'
XDR_INCIDENT_TYPE_NAME = 'Cortex XDR Incident'
INTEGRATION_NAME = 'Cortex XDR - IR'

XDR_INCIDENT_FIELDS = {
    "status": {"description": "Current status of the incident: \"new\",\"under_"
                              "investigation\",\"resolved_known_issue\","
                              "\"resolved_duplicate\",\"resolved_false_positive\","
                              "\"resolved_true_positive\",\"resolved_security_testing\",\"resolved_other\"",
               "xsoar_field_name": 'xdrstatusv2'},
    "assigned_user_mail": {"description": "Email address of the assigned user.",
                           'xsoar_field_name': "xdrassigneduseremail"},
    "assigned_user_pretty_name": {"description": "Full name of the user assigned to the incident.",
                                  "xsoar_field_name": "xdrassigneduserprettyname"},
    "resolve_comment": {"description": "Comments entered by the user when the incident was resolved.",
                        "xsoar_field_name": "xdrresolvecomment"},
    "manual_severity": {"description": "Incident severity assigned by the user. "
                                       "This does not affect the calculated severity low medium high",
                        "xsoar_field_name": "severity"},
}

XDR_RESOLVED_STATUS_TO_XSOAR = {
    'resolved_known_issue': 'Other',
    'resolved_duplicate': 'Duplicate',
    'resolved_false_positive': 'False Positive',
    'resolved_true_positive': 'Resolved',
    'resolved_security_testing': 'Other',
    'resolved_other': 'Other'
}

XSOAR_RESOLVED_STATUS_TO_XDR = {
    'Other': 'resolved_other',
    'Duplicate': 'resolved_duplicate',
    'False Positive': 'resolved_false_positive',
    'Resolved': 'resolved_true_positive',
}

MIRROR_DIRECTION = {
    'None': None,
    'Incoming': 'In',
    'Outgoing': 'Out',
    'Both': 'Both'
}


def convert_epoch_to_milli(timestamp):
    if timestamp is None:
        return None
    if 9 < len(str(timestamp)) < 13:
        timestamp = int(timestamp) * 1000
    return int(timestamp)


def convert_datetime_to_epoch(the_time=0):
    if the_time is None:
        return None
    try:
        if isinstance(the_time, datetime):
            return int(the_time.strftime('%s'))
    except Exception as err:
        demisto.debug(err)
        return 0


def convert_datetime_to_epoch_millis(the_time=0):
    return convert_epoch_to_milli(convert_datetime_to_epoch(the_time=the_time))


def generate_current_epoch_utc():
    return convert_datetime_to_epoch_millis(datetime.now(timezone.utc))


def generate_key():
    return "".join([secrets.choice(string.ascii_letters + string.digits) for _ in range(API_KEY_LENGTH)])


def create_auth(api_key):
    nonce = "".join([secrets.choice(string.ascii_letters + string.digits) for _ in range(NONCE_LENGTH)])
    timestamp = str(generate_current_epoch_utc())  # Get epoch time utc millis
    hash_ = hashlib.sha256()
    hash_.update((api_key + nonce + timestamp).encode("utf-8"))
    return nonce, timestamp, hash_.hexdigest()


def clear_trailing_whitespace(res):
    index = 0
    while index < len(res):
        for key, value in res[index].items():
            if isinstance(value, str):
                res[index][key] = value.rstrip()
        index += 1
    return res


class Client(CoreClient):

    def test_module(self, first_fetch_time):
        """
            Performs basic get request to get item samples
        """
        last_one_day, _ = parse_date_range(first_fetch_time, TIME_FORMAT)
        try:
            self.get_incidents(lte_creation_time=last_one_day, limit=1)
        except Exception as err:
            if 'API request Unauthorized' in str(err):
                # this error is received from the XDR server when the client clock is not in sync to the server
                raise DemistoException(f'{str(err)} please validate that your both '
                                       f'XSOAR and XDR server clocks are in sync')
            else:
                raise

    def isolate_endpoint(self, endpoint_id, incident_id=None):
        request_data = {
            'endpoint_id': endpoint_id,
        }
        if incident_id:
            request_data['incident_id'] = incident_id

        self._http_request(
            method='POST',
            url_suffix='/endpoints/isolate',
            json_data={'request_data': request_data},
            timeout=self.timeout
        )

    def unisolate_endpoint(self, endpoint_id, incident_id=None):
        request_data = {
            'endpoint_id': endpoint_id,
        }
        if incident_id:
            request_data['incident_id'] = incident_id

        self._http_request(
            method='POST',
            url_suffix='/endpoints/unisolate',
            json_data={'request_data': request_data},
            timeout=self.timeout
        )


def get_incidents_command(client, args):
    """
    Retrieve a list of incidents from XDR, filtered by some filters.
    """

    # sometimes incident id can be passed as integer from the playbook
    incident_id_list = args.get('incident_id_list')
    if isinstance(incident_id_list, int):
        incident_id_list = str(incident_id_list)

    incident_id_list = argToList(incident_id_list)
    # make sure all the ids passed are strings and not integers
    for index, id_ in enumerate(incident_id_list):
        if isinstance(id_, (int, float)):
            incident_id_list[index] = str(id_)

    lte_modification_time = args.get('lte_modification_time')
    gte_modification_time = args.get('gte_modification_time')
    since_modification_time = args.get('since_modification_time')

    if since_modification_time and gte_modification_time:
        raise ValueError('Can\'t set both since_modification_time and lte_modification_time')
    if since_modification_time:
        gte_modification_time, _ = parse_date_range(since_modification_time, TIME_FORMAT)

    lte_creation_time = args.get('lte_creation_time')
    gte_creation_time = args.get('gte_creation_time')
    since_creation_time = args.get('since_creation_time')

    if since_creation_time and gte_creation_time:
        raise ValueError('Can\'t set both since_creation_time and lte_creation_time')
    if since_creation_time:
        gte_creation_time, _ = parse_date_range(since_creation_time, TIME_FORMAT)

    statuses = argToList(args.get('status', ''))

    sort_by_modification_time = args.get('sort_by_modification_time')
    sort_by_creation_time = args.get('sort_by_creation_time')

    page = int(args.get('page', 0))
    limit = int(args.get('limit', 100))

    # If no filters were given, return a meaningful error message
    if not incident_id_list and (not lte_modification_time and not gte_modification_time and not since_modification_time
                                 and not lte_creation_time and not gte_creation_time and not since_creation_time
                                 and not statuses):
        raise ValueError("Specify a query for the incidents.\nFor example:"
                         " !xdr-get-incidents since_creation_time=\"1 year\" sort_by_creation_time=\"desc\" limit=10")

    if statuses:
        raw_incidents = []

        for status in statuses:
            raw_incidents += client.get_incidents(
                incident_id_list=incident_id_list,
                lte_modification_time=lte_modification_time,
                gte_modification_time=gte_modification_time,
                lte_creation_time=lte_creation_time,
                gte_creation_time=gte_creation_time,
                sort_by_creation_time=sort_by_creation_time,
                sort_by_modification_time=sort_by_modification_time,
                page_number=page,
                limit=limit,
                status=status
            )

        if len(raw_incidents) > limit:
            raw_incidents[:limit]
    else:
        raw_incidents = client.get_incidents(
            incident_id_list=incident_id_list,
            lte_modification_time=lte_modification_time,
            gte_modification_time=gte_modification_time,
            lte_creation_time=lte_creation_time,
            gte_creation_time=gte_creation_time,
            sort_by_creation_time=sort_by_creation_time,
            sort_by_modification_time=sort_by_modification_time,
            page_number=page,
            limit=limit,
        )

    return (
        tableToMarkdown('Incidents', raw_incidents),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.Incident(val.incident_id==obj.incident_id)': raw_incidents
        },
        raw_incidents
    )


def create_endpoint_context(audit_logs):
    endpoints = []
    for log in audit_logs:
        endpoint_details = {
            'ID': log.get('ENDPOINTID'),
            'Hostname': log.get('ENDPOINTNAME'),
            'Domain': log.get('DOMAIN'),
        }
        remove_nulls_from_dictionary(endpoint_details)
        if endpoint_details:
            endpoints.append(endpoint_details)

    return endpoints


def create_account_context(endpoints):
    account_context = []
    for endpoint in endpoints:
        domain = endpoint.get('domain')
        if domain:
            users = endpoint.get('users', [])  # in case the value of 'users' is None
            if users and isinstance(users, list):
                for user in users:
                    account_context.append({
                        'Username': user,
                        'Domain': domain,
                    })

    return account_context


def get_process_context(alert, process_type):
    process_context = {
        'Name': alert.get(f'{process_type}_process_image_name'),
        'MD5': alert.get(f'{process_type}_process_image_md5'),
        'SHA256': alert.get(f'{process_type}_process_image_sha256'),
        'PID': alert.get(f'{process_type}_process_os_pid'),
        'CommandLine': alert.get(f'{process_type}_process_command_line'),
        'Path': alert.get(f'{process_type}_process_image_path'),
        'Start Time': alert.get(f'{process_type}_process_execution_time'),
        'Hostname': alert.get('host_name'),
    }

    remove_nulls_from_dictionary(process_context)

    # If the process contains only 'HostName' , don't create an indicator
    if len(process_context.keys()) == 1 and 'Hostname' in process_context.keys():
        return {}
    return process_context


def add_to_ip_context(alert, ip_context):
    action_local_ip = alert.get('action_local_ip')
    action_remote_ip = alert.get('action_remote_ip')
    if action_local_ip:
        ip_context.append({
            'Address': action_local_ip,
        })

    if action_remote_ip:
        ip_context.append({
            'Address': action_remote_ip,
        })


def create_context_from_network_artifacts(network_artifacts, ip_context):
    domain_context = []

    if network_artifacts:
        for artifact in network_artifacts:
            domain = artifact.get('network_domain')
            if domain:
                domain_context.append({
                    'Name': domain,
                })

            network_ip_details = {
                'Address': artifact.get('network_remote_ip'),
                'GEO': {
                    'Country': artifact.get('network_country')},
            }

            remove_nulls_from_dictionary(network_ip_details)

            if network_ip_details:
                ip_context.append(network_ip_details)

    return domain_context


def get_indicators_context(incident):
    file_context: List[Any] = []
    process_context: List[Any] = []
    ip_context: List[Any] = []
    for alert in incident.get('alerts', []):
        # file context
        file_details = {
            'Name': alert.get('action_file_name'),
            'Path': alert.get('action_file_path'),
            'SHA265': alert.get('action_file_sha256'),  # Here for backward compatibility
            'SHA256': alert.get('action_file_sha256'),
            'MD5': alert.get('action_file_md5'),
        }
        remove_nulls_from_dictionary(file_details)

        if file_details:
            file_context.append(file_details)

        # process context
        process_types = ['actor', 'os_actor', 'causality_actor', 'action']
        for process_type in process_types:
            single_process_context = get_process_context(alert, process_type)
            if single_process_context:
                process_context.append(single_process_context)

        # ip context
        add_to_ip_context(alert, ip_context)

    network_artifacts = incident.get('network_artifacts', [])

    domain_context = create_context_from_network_artifacts(network_artifacts, ip_context)

    file_artifacts = incident.get('file_artifacts', [])
    for file in file_artifacts:
        file_details = {
            'Name': file.get('file_name'),
            'SHA256': file.get('file_sha256'),
        }
        remove_nulls_from_dictionary(file_details)
        if file_details:
            file_context.append(file_details)

    return file_context, process_context, domain_context, ip_context


def check_if_incident_was_modified_in_xdr(incident_id, last_mirrored_in_time_timestamp, last_modified_incidents_dict):
    if incident_id in last_modified_incidents_dict:  # search the incident in the dict of modified incidents
        incident_modification_time_in_xdr = int(str(last_modified_incidents_dict[incident_id]))

        demisto.debug(f"XDR incident {incident_id}\n"
                      f"modified time:         {incident_modification_time_in_xdr}\n"
                      f"last mirrored in time: {last_mirrored_in_time_timestamp}")

        if incident_modification_time_in_xdr > last_mirrored_in_time_timestamp:  # need to update this incident
            demisto.info(f"Incident '{incident_id}' was modified. performing extra-data request.")
            return True
    # the incident was not modified
    return False


def get_last_mirrored_in_time(args):
    demisto_incidents = demisto.get_incidents()  # type: ignore

    if demisto_incidents:  # handling 5.5 version
        demisto_incident = demisto_incidents[0]
        last_mirrored_in_time = demisto_incident.get('CustomFields', {}).get('lastmirroredintime')
        if not last_mirrored_in_time:  # this is an old incident, update anyway
            return 0
        last_mirrored_in_timestamp = arg_to_timestamp(last_mirrored_in_time, 'last_mirrored_in_time')

    else:  # handling 6.0 version
        last_mirrored_in_time = arg_to_timestamp(args.get('last_update'), 'last_update')
        last_mirrored_in_timestamp = (last_mirrored_in_time - (120 * 1000))

    return last_mirrored_in_timestamp


def get_incident_extra_data_command(client, args):
    incident_id = args.get('incident_id')
    alerts_limit = int(args.get('alerts_limit', 1000))
    return_only_updated_incident = argToBoolean(args.get('return_only_updated_incident', 'False'))

    if return_only_updated_incident:
        last_mirrored_in_time = get_last_mirrored_in_time(args)
        last_modified_incidents_dict = get_integration_context().get('modified_incidents', {})

        if check_if_incident_was_modified_in_xdr(incident_id, last_mirrored_in_time, last_modified_incidents_dict):
            pass  # the incident was modified. continue to perform extra-data request

        else:  # the incident was not modified
            return "The incident was not modified in XDR since the last mirror in.", {}, {}

    demisto.debug(f"Performing extra-data request on incident: {incident_id}")
    raw_incident = client.get_incident_extra_data(incident_id, alerts_limit)

    incident = raw_incident.get('incident')
    incident_id = incident.get('incident_id')
    raw_alerts = raw_incident.get('alerts').get('data')
    context_alerts = clear_trailing_whitespace(raw_alerts)
    for alert in context_alerts:
        alert['host_ip_list'] = alert.get('host_ip').split(',') if alert.get('host_ip') else []
    file_artifacts = raw_incident.get('file_artifacts').get('data')
    network_artifacts = raw_incident.get('network_artifacts').get('data')

    readable_output = [tableToMarkdown('Incident {}'.format(incident_id), incident)]

    if len(context_alerts) > 0:
        readable_output.append(tableToMarkdown('Alerts', context_alerts,
                                               headers=[key for key in context_alerts[0] if key != 'host_ip']))
    else:
        readable_output.append(tableToMarkdown('Alerts', []))

    if len(network_artifacts) > 0:
        readable_output.append(tableToMarkdown('Network Artifacts', network_artifacts))
    else:
        readable_output.append(tableToMarkdown('Network Artifacts', []))

    if len(file_artifacts) > 0:
        readable_output.append(tableToMarkdown('File Artifacts', file_artifacts))
    else:
        readable_output.append(tableToMarkdown('File Artifacts', []))

    incident.update({
        'alerts': context_alerts,
        'file_artifacts': file_artifacts,
        'network_artifacts': network_artifacts
    })
    account_context_output = assign_params(**{
        'Username': incident.get('users', '')
    })
    endpoint_context_output = assign_params(**{
        'Hostname': incident.get('hosts', '')
    })

    context_output = {f'{INTEGRATION_CONTEXT_BRAND}.Incident(val.incident_id==obj.incident_id)': incident}
    if account_context_output:
        context_output['Account(val.Username==obj.Username)'] = account_context_output
    if endpoint_context_output:
        context_output['Endpoint(val.Hostname==obj.Hostname)'] = endpoint_context_output

    file_context, process_context, domain_context, ip_context = get_indicators_context(incident)

    if file_context:
        context_output[Common.File.CONTEXT_PATH] = file_context
    if domain_context:
        context_output[Common.Domain.CONTEXT_PATH] = domain_context
    if ip_context:
        context_output[Common.IP.CONTEXT_PATH] = ip_context
    if process_context:
        context_output['Process(val.Name && val.Name == obj.Name)'] = process_context

    return (
        '\n'.join(readable_output),
        context_output,
        raw_incident
    )


def update_incident_command(client, args):
    incident_id = args.get('incident_id')
    assigned_user_mail = args.get('assigned_user_mail')
    assigned_user_pretty_name = args.get('assigned_user_pretty_name')
    status = args.get('status')
    severity = args.get('manual_severity')
    unassign_user = args.get('unassign_user') == 'true'
    resolve_comment = args.get('resolve_comment')

    client.update_incident(
        incident_id=incident_id,
        assigned_user_mail=assigned_user_mail,
        assigned_user_pretty_name=assigned_user_pretty_name,
        unassign_user=unassign_user,
        status=status,
        severity=severity,
        resolve_comment=resolve_comment
    )

    return f'Incident {incident_id} has been updated', None, None


def endpoint_command(client, args):
    endpoint_id_list = argToList(args.get('id'))
    endpoint_ip_list = argToList(args.get('ip'))
    endpoint_hostname_list = argToList(args.get('hostname'))

    if not endpoint_id_list and not endpoint_ip_list and not endpoint_hostname_list:
        raise Exception(f'{INTEGRATION_NAME} - In order to run this command, please provide valid id, ip or hostname')

    # The `!endpoint` command should use an OR operator between filters. Since XDR API supports only AND, we handle it
    # by sending multiple requests with a single filter and appending the returned results to previous results.
    endpoints = []
    if endpoint_id_list:
        endpoints.extend(client.get_endpoints(endpoint_id_list=endpoint_id_list))
    if endpoint_ip_list:
        endpoints.extend(client.get_endpoints(ip_list=endpoint_ip_list))
    if endpoint_hostname_list:
        endpoints.extend(client.get_endpoints(hostname=endpoint_hostname_list))

    # Remove duplicates by taking entries with unique `endpoint_id`:
    if endpoints:
        endpoints = list({v['endpoint_id']: v for v in endpoints}.values())

    standard_endpoints = generate_endpoint_by_contex_standard(endpoints, True, INTEGRATION_NAME)
    command_results = []
    if standard_endpoints:
        for endpoint in standard_endpoints:
            endpoint_context = endpoint.to_context().get(Common.Endpoint.CONTEXT_PATH)
            hr = tableToMarkdown('Cortex XDR Endpoint', endpoint_context)

            command_results.append(CommandResults(
                readable_output=hr,
                raw_response=endpoints,
                indicator=endpoint
            ))

    else:
        command_results.append(CommandResults(
            readable_output="No endpoints were found",
            raw_response=endpoints,
        ))
    return command_results


def create_parsed_alert(product, vendor, local_ip, local_port, remote_ip, remote_port, event_timestamp, severity,
                        alert_name, alert_description):
    alert = {
        "product": product,
        "vendor": vendor,
        "local_ip": local_ip,
        "local_port": local_port,
        "remote_ip": remote_ip,
        "remote_port": remote_port,
        "event_timestamp": event_timestamp,
        "severity": severity,
        "alert_name": alert_name,
        "alert_description": alert_description
    }

    return alert


def insert_parsed_alert_command(client, args):
    product = args.get('product')
    vendor = args.get('vendor')
    local_ip = args.get('local_ip')
    local_port = arg_to_int(
        arg=args.get('local_port'),
        arg_name='local_port'
    )
    remote_ip = args.get('remote_ip')
    remote_port = arg_to_int(
        arg=args.get('remote_port'),
        arg_name='remote_port'
    )

    severity = args.get('severity')
    alert_name = args.get('alert_name')
    alert_description = args.get('alert_description', '')

    if args.get('event_timestamp') is None:
        # get timestamp now if not provided
        event_timestamp = int(round(time.time() * 1000))
    else:
        event_timestamp = int(args.get('event_timestamp'))

    alert = create_parsed_alert(
        product=product,
        vendor=vendor,
        local_ip=local_ip,
        local_port=local_port,
        remote_ip=remote_ip,
        remote_port=remote_port,
        event_timestamp=event_timestamp,
        severity=severity,
        alert_name=alert_name,
        alert_description=alert_description
    )

    client.insert_alerts([alert])

    return (
        'Alert inserted successfully',
        None,
        None
    )


def insert_cef_alerts_command(client, args):
    # parsing alerts list. the reason we don't use argToList is because cef_alerts could contain comma (,) so
    # we shouldn't split them by comma
    alerts = args.get('cef_alerts')
    if isinstance(alerts, list):
        pass
    elif isinstance(alerts, str):
        if alerts[0] == '[' and alerts[-1] == ']':
            # if the string contains [] it means it is a list and must be parsed
            alerts = json.loads(alerts)
        else:
            # otherwise it is a single alert
            alerts = [alerts]
    else:
        raise ValueError('Invalid argument "cef_alerts". It should be either list of strings (cef alerts), '
                         'or single string')

    client.insert_cef_alerts(alerts)

    return (
        'Alerts inserted successfully',
        None,
        None
    )


def get_audit_management_logs_command(client, args):
    email = argToList(args.get('email'))
    result = argToList(args.get('result'))
    _type = argToList(args.get('type'))
    sub_type = argToList(args.get('sub_type'))

    timestamp_gte = arg_to_timestamp(
        arg=args.get('timestamp_gte'),
        arg_name='timestamp_gte'
    )

    timestamp_lte = arg_to_timestamp(
        arg=args.get('timestamp_lte'),
        arg_name='timestamp_lte'
    )

    page_number = arg_to_int(
        arg=args.get('page', 0),
        arg_name='Failed to parse "page". Must be a number.',
        required=True
    )
    limit = arg_to_int(
        arg=args.get('limit', 20),
        arg_name='Failed to parse "limit". Must be a number.',
        required=True
    )
    search_from = page_number * limit
    search_to = search_from + limit

    sort_by = args.get('sort_by')
    sort_order = args.get('sort_order', 'asc')

    audit_logs = client.audit_management_logs(
        email=email,
        result=result,
        _type=_type,
        sub_type=sub_type,
        timestamp_gte=timestamp_gte,
        timestamp_lte=timestamp_lte,
        search_from=search_from,
        search_to=search_to,
        sort_by=sort_by,
        sort_order=sort_order
    )

    return (
        tableToMarkdown('Audit Management Logs', audit_logs, [
            'AUDIT_ID',
            'AUDIT_RESULT',
            'AUDIT_DESCRIPTION',
            'AUDIT_OWNER_NAME',
            'AUDIT_OWNER_EMAIL',
            'AUDIT_ASSET_JSON',
            'AUDIT_ASSET_NAMES',
            'AUDIT_HOSTNAME',
            'AUDIT_REASON',
            'AUDIT_ENTITY',
            'AUDIT_ENTITY_SUBTYPE',
            'AUDIT_SESSION_ID',
            'AUDIT_CASE_ID',
            'AUDIT_INSERT_TIME'
        ]),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.AuditManagementLogs(val.AUDIT_ID == obj.AUDIT_ID)': audit_logs
        },
        audit_logs
    )


def get_audit_agent_reports_command(client, args):
    endpoint_ids = argToList(args.get('endpoint_ids'))
    endpoint_names = argToList(args.get('endpoint_names'))
    result = argToList(args.get('result'))
    _type = argToList(args.get('type'))
    sub_type = argToList(args.get('sub_type'))

    timestamp_gte = arg_to_timestamp(
        arg=args.get('timestamp_gte'),
        arg_name='timestamp_gte'
    )

    timestamp_lte = arg_to_timestamp(
        arg=args.get('timestamp_lte'),
        arg_name='timestamp_lte'
    )

    page_number = arg_to_int(
        arg=args.get('page', 0),
        arg_name='Failed to parse "page". Must be a number.',
        required=True
    )
    limit = arg_to_int(
        arg=args.get('limit', 20),
        arg_name='Failed to parse "limit". Must be a number.',
        required=True
    )
    search_from = page_number * limit
    search_to = search_from + limit

    sort_by = args.get('sort_by')
    sort_order = args.get('sort_order', 'asc')

    audit_logs = client.get_audit_agent_reports(
        endpoint_ids=endpoint_ids,
        endpoint_names=endpoint_names,
        result=result,
        _type=_type,
        sub_type=sub_type,
        timestamp_gte=timestamp_gte,
        timestamp_lte=timestamp_lte,

        search_from=search_from,
        search_to=search_to,
        sort_by=sort_by,
        sort_order=sort_order
    )
    integration_context = {f'{INTEGRATION_CONTEXT_BRAND}.AuditAgentReports': audit_logs}
    endpoint_context = create_endpoint_context(audit_logs)
    if endpoint_context:
        integration_context[Common.Endpoint.CONTEXT_PATH] = endpoint_context
    return (
        tableToMarkdown('Audit Agent Reports', audit_logs),
        integration_context,
        audit_logs
    )


def get_distribution_url_command(client, args):
    distribution_id = args.get('distribution_id')
    package_type = args.get('package_type')

    url = client.get_distribution_url(distribution_id, package_type)

    return (
        f'[Distribution URL]({url})',
        {
            'PaloAltoNetworksXDR.Distribution(val.id == obj.id)': {
                'id': distribution_id,
                'url': url
            }
        },
        url
    )


def get_distribution_status_command(client, args):
    distribution_ids = argToList(args.get('distribution_ids'))

    distribution_list = []
    for distribution_id in distribution_ids:
        status = client.get_distribution_status(distribution_id)

        distribution_list.append({
            'id': distribution_id,
            'status': status
        })

    return (
        tableToMarkdown('Distribution Status', distribution_list, ['id', 'status']),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.Distribution(val.id == obj.id)': distribution_list
        },
        distribution_list
    )


def get_distribution_versions_command(client):
    versions = client.get_distribution_versions()

    readable_output = []
    for operation_system in versions.keys():
        os_versions = versions[operation_system]

        readable_output.append(
            tableToMarkdown(operation_system, os_versions or [], ['versions'])
        )

    return (
        '\n\n'.join(readable_output),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.DistributionVersions': versions
        },
        versions
    )


def create_distribution_command(client, args):
    name = args.get('name')
    platform = args.get('platform')
    package_type = args.get('package_type')
    description = args.get('description')
    agent_version = args.get('agent_version')
    if not platform == 'android' and not agent_version:
        # agent_version must be provided for all the platforms except android
        raise ValueError(f'Missing argument "agent_version" for platform "{platform}"')

    distribution_id = client.create_distribution(
        name=name,
        platform=platform,
        package_type=package_type,
        agent_version=agent_version,
        description=description
    )

    distribution = {
        'id': distribution_id,
        'name': name,
        'platform': platform,
        'package_type': package_type,
        'agent_version': agent_version,
        'description': description
    }

    return (
        f'Distribution {distribution_id} created successfully',
        {
            f'{INTEGRATION_CONTEXT_BRAND}.Distribution(val.id == obj.id)': distribution
        },
        distribution
    )


def blacklist_files_command(client, args):
    hash_list = argToList(args.get('hash_list'))
    comment = args.get('comment')
    incident_id = arg_to_number(args.get('incident_id'))

    res = client.blocklist_files(hash_list=hash_list, comment=comment, incident_id=incident_id)
    if isinstance(res, dict) and res.get('err_extra') != "All hashes have already been added to the allow or block list":
        raise ValueError(res)
    markdown_data = [{'fileHash': file_hash} for file_hash in hash_list]

    return (
        tableToMarkdown('Blacklist Files', markdown_data, headers=['fileHash'], headerTransform=pascalToSpace),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.blackList.fileHash(val.fileHash == obj.fileHash)': hash_list
        },
        argToList(hash_list)
    )


def whitelist_files_command(client, args):
    hash_list = argToList(args.get('hash_list'))
    comment = args.get('comment')
    incident_id = arg_to_number(args.get('incident_id'))

    client.allowlist_files(hash_list=hash_list, comment=comment, incident_id=incident_id)
    markdown_data = [{'fileHash': file_hash} for file_hash in hash_list]
    return (
        tableToMarkdown('Whitelist Files', markdown_data, ['fileHash'], headerTransform=pascalToSpace),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.whiteList.fileHash(val.fileHash == obj.fileHash)': hash_list
        },
        argToList(hash_list)
    )


def quarantine_files_command(client, args):
    endpoint_id_list = argToList(args.get("endpoint_id_list"))
    file_path = args.get("file_path")
    file_hash = args.get("file_hash")
    incident_id = arg_to_number(args.get('incident_id'))

    reply = client.quarantine_files(
        endpoint_id_list=endpoint_id_list,
        file_path=file_path,
        file_hash=file_hash,
        incident_id=incident_id
    )
    output = {
        'endpointIdList': endpoint_id_list,
        'filePath': file_path,
        'fileHash': file_hash,
        'actionId': reply.get("action_id")
    }

    return (
        tableToMarkdown('Quarantine files', output, headers=[*output],
                        headerTransform=pascalToSpace),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.quarantineFiles.actionIds(val.actionId === obj.actionId)': output
        },
        reply
    )


def restore_file_command(client, args):
    file_hash = args.get('file_hash')
    endpoint_id = args.get('endpoint_id')
    incident_id = arg_to_number(args.get('incident_id'))

    reply = client.restore_file(
        file_hash=file_hash,
        endpoint_id=endpoint_id,
        incident_id=incident_id
    )
    action_id = reply.get("action_id")

    return (
        tableToMarkdown('Restore files', {'Action Id': action_id}, ['Action Id']),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.restoredFiles.actionId(val.actionId == obj.actionId)': action_id
        },
        action_id
    )


def get_quarantine_status_command(client, args):
    file_path = args.get('file_path')
    file_hash = args.get('file_hash')
    endpoint_id = args.get('endpoint_id')

    reply = client.get_quarantine_status(
        file_path=file_path,
        file_hash=file_hash,
        endpoint_id=endpoint_id
    )
    output = {
        'status': reply['status'],
        'endpointId': reply['endpoint_id'],
        'filePath': reply['file_path'],
        'fileHash': reply['file_hash']
    }

    return (
        tableToMarkdown('Quarantine files', output, headers=[*output], headerTransform=pascalToSpace),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.quarantineFiles.status(val.fileHash === obj.fileHash &&'
            f'val.endpointId === obj.endpointId && val.filePath === obj.filePath)': output
        },
        reply
    )


def endpoint_scan_abort_command(client, args):
    endpoint_id_list = argToList(args.get('endpoint_id_list'))
    dist_name = argToList(args.get('dist_name'))
    gte_first_seen = args.get('gte_first_seen')
    gte_last_seen = args.get('gte_last_seen')
    lte_first_seen = args.get('lte_first_seen')
    lte_last_seen = args.get('lte_last_seen')
    ip_list = argToList(args.get('ip_list'))
    group_name = argToList(args.get('group_name'))
    platform = argToList(args.get('platform'))
    alias = argToList(args.get('alias'))
    isolate = args.get('isolate')
    hostname = argToList(args.get('hostname'))
    incident_id = arg_to_number(args.get('incident_id'))

    validate_args_scan_commands(args)

    reply = client.endpoint_scan(
        url_suffix='endpoints/abort_scan/',
        endpoint_id_list=argToList(endpoint_id_list),
        dist_name=dist_name,
        gte_first_seen=gte_first_seen,
        gte_last_seen=gte_last_seen,
        lte_first_seen=lte_first_seen,
        lte_last_seen=lte_last_seen,
        ip_list=ip_list,
        group_name=group_name,
        platform=platform,
        alias=alias,
        isolate=isolate,
        hostname=hostname,
        incident_id=incident_id
    )

    action_id = reply.get("action_id")

    context = {
        "actionId": action_id,
        "aborted": True
    }

    return (
        tableToMarkdown('Endpoint abort scan', {'Action Id': action_id}, ['Action Id']),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.endpointScan(val.actionId == obj.actionId)': context
        },
        reply
    )


def validate_args_scan_commands(args):
    endpoint_id_list = argToList(args.get('endpoint_id_list'))
    dist_name = argToList(args.get('dist_name'))
    gte_first_seen = args.get('gte_first_seen')
    gte_last_seen = args.get('gte_last_seen')
    lte_first_seen = args.get('lte_first_seen')
    lte_last_seen = args.get('lte_last_seen')
    ip_list = argToList(args.get('ip_list'))
    group_name = argToList(args.get('group_name'))
    platform = argToList(args.get('platform'))
    alias = argToList(args.get('alias'))
    hostname = argToList(args.get('hostname'))
    all_ = argToBoolean(args.get('all', 'false'))

    # to prevent the case where an empty filtered command will trigger by default a scan on all the endpoints.
    err_msg = 'To scan/abort scan all the endpoints run this command with the \'all\' argument as True ' \
              'and without any other filters. This may cause performance issues.\n' \
              'To scan/abort scan some of the endpoints, please use the filter arguments.'
    if all_:
        if endpoint_id_list or dist_name or gte_first_seen or gte_last_seen or lte_first_seen or lte_last_seen \
                or ip_list or group_name or platform or alias or hostname:
            raise Exception(err_msg)
    else:
        if not endpoint_id_list and not dist_name and not gte_first_seen and not gte_last_seen \
                and not lte_first_seen and not lte_last_seen and not ip_list and not group_name and not platform \
                and not alias and not hostname:
            raise Exception(err_msg)


def sort_by_key(list_to_sort, main_key, fallback_key):
    """Sorts a given list elements by main_key for all elements with the key,
    uses sorting by fallback_key on all elements that dont have the main_key"""
    list_elements_with_main_key = [element for element in list_to_sort if element.get(main_key)]
    sorted_list = sorted(list_elements_with_main_key, key=itemgetter(main_key))
    if len(list_to_sort) == len(sorted_list):
        return sorted_list

    list_elements_with_fallback_without_main = [element for element in list_to_sort
                                                if element.get(fallback_key) and not element.get(main_key)]
    sorted_list.extend(sorted(list_elements_with_fallback_without_main, key=itemgetter(fallback_key)))

    if len(sorted_list) == len(list_to_sort):
        return sorted_list

    list_elements_without_fallback_and_main = [element for element in list_to_sort
                                               if not element.get(fallback_key) and not element.get(main_key)]

    sorted_list.extend(list_elements_without_fallback_and_main)
    return sorted_list


def sort_all_list_incident_fields(incident_data):
    """Sorting all lists fields in an incident - without this, elements may shift which results in false
    identification of changed fields"""
    if incident_data.get('hosts', []):
        incident_data['hosts'] = sorted(incident_data.get('hosts', []))
        incident_data['hosts'] = [host.upper() for host in incident_data.get('hosts', [])]

    if incident_data.get('users', []):
        incident_data['users'] = sorted(incident_data.get('users', []))
        incident_data['users'] = [user.upper() for user in incident_data.get('users', [])]

    if incident_data.get('incident_sources', []):
        incident_data['incident_sources'] = sorted(incident_data.get('incident_sources', []))

    if incident_data.get('alerts', []):
        incident_data['alerts'] = sort_by_key(incident_data.get('alerts', []), main_key='alert_id', fallback_key='name')
        reformat_sublist_fields(incident_data['alerts'])

    if incident_data.get('file_artifacts', []):
        incident_data['file_artifacts'] = sort_by_key(incident_data.get('file_artifacts', []), main_key='file_name',
                                                      fallback_key='file_sha256')
        reformat_sublist_fields(incident_data['file_artifacts'])

    if incident_data.get('network_artifacts', []):
        incident_data['network_artifacts'] = sort_by_key(incident_data.get('network_artifacts', []),
                                                         main_key='network_domain', fallback_key='network_remote_ip')
        reformat_sublist_fields(incident_data['network_artifacts'])


def drop_field_underscore(section):
    section_copy = section.copy()
    for field in section_copy.keys():
        if '_' in field:
            section[field.replace('_', '')] = section.get(field)


def reformat_sublist_fields(sublist):
    for section in sublist:
        drop_field_underscore(section)


def sync_incoming_incident_owners(incident_data):
    if incident_data.get('assigned_user_mail') and demisto.params().get('sync_owners'):
        user_info = demisto.findUser(email=incident_data.get('assigned_user_mail'))
        if user_info:
            demisto.debug(f"Syncing incident owners: XDR incident {incident_data.get('incident_id')}, "
                          f"owner {user_info.get('username')}")
            incident_data['owner'] = user_info.get('username')

        else:
            demisto.debug(f"The user assigned to XDR incident {incident_data.get('incident_id')} "
                          f"is not registered on XSOAR")


def handle_incoming_user_unassignment(incident_data):
    incident_data['assigned_user_mail'] = ''
    incident_data['assigned_user_pretty_name'] = ''
    if demisto.params().get('sync_owners'):
        demisto.debug(f'Unassigning owner from XDR incident {incident_data.get("incident_id")}')
        incident_data['owner'] = ''


def handle_incoming_closing_incident(incident_data):
    closing_entry = {}  # type: Dict
    if incident_data.get('status') in XDR_RESOLVED_STATUS_TO_XSOAR:
        demisto.debug(f"Closing XDR issue {incident_data.get('incident_id')}")
        closing_entry = {
            'Type': EntryType.NOTE,
            'Contents': {
                'dbotIncidentClose': True,
                'closeReason': XDR_RESOLVED_STATUS_TO_XSOAR.get(incident_data.get("status")),
                'closeNotes': incident_data.get('resolve_comment')
            },
            'ContentsFormat': EntryFormat.JSON
        }
        incident_data['closeReason'] = XDR_RESOLVED_STATUS_TO_XSOAR.get(incident_data.get("status"))
        incident_data['closeNotes'] = incident_data.get('resolve_comment')

        if incident_data.get('status') == 'resolved_known_issue':
            closing_entry['Contents']['closeNotes'] = 'Known Issue.\n' + incident_data['closeNotes']
            incident_data['closeNotes'] = 'Known Issue.\n' + incident_data['closeNotes']

    return closing_entry


def get_mapping_fields_command():
    xdr_incident_type_scheme = SchemeTypeMapping(type_name=XDR_INCIDENT_TYPE_NAME)
    for field in XDR_INCIDENT_FIELDS:
        xdr_incident_type_scheme.add_field(name=field, description=XDR_INCIDENT_FIELDS[field].get('description'))

    mapping_response = GetMappingFieldsResponse()
    mapping_response.add_scheme_type(xdr_incident_type_scheme)

    return mapping_response


def get_modified_remote_data_command(client, args):
    remote_args = GetModifiedRemoteDataArgs(args)
    last_update = remote_args.last_update  # In the first run, this value will be set to 1 minute earlier

    demisto.debug(f'Performing get-modified-remote-data command. Last update is: {last_update}')

    last_update_utc = dateparser.parse(last_update, settings={'TIMEZONE': 'UTC'})  # convert to utc format
    if last_update_utc:
        last_update_without_ms = last_update_utc.isoformat().split('.')[0]

    raw_incidents = client.get_incidents(gte_modification_time=last_update_without_ms, limit=100)

    modified_incident_ids = list()
    for raw_incident in raw_incidents:
        incident_id = raw_incident.get('incident_id')
        modified_incident_ids.append(incident_id)

    return GetModifiedRemoteDataResponse(modified_incident_ids)


def get_remote_data_command(client, args):
    remote_args = GetRemoteDataArgs(args)
    demisto.debug(f'Performing get-remote-data command with incident id: {remote_args.remote_incident_id}')

    incident_data = {}
    try:
        # when Demisto version is 6.1.0 and above, this command will only be automatically executed on incidents
        # returned from get_modified_remote_data_command so we want to perform extra-data request on those incidents.
        return_only_updated_incident = not is_demisto_version_ge('6.1.0')  # True if version is below 6.1 else False

        incident_data = get_incident_extra_data_command(client, {"incident_id": remote_args.remote_incident_id,
                                                                 "alerts_limit": 1000,
                                                                 "return_only_updated_incident": return_only_updated_incident,
                                                                 "last_update": remote_args.last_update})
        if 'The incident was not modified' not in incident_data[0]:
            demisto.debug(f"Updating XDR incident {remote_args.remote_incident_id}")

            incident_data = incident_data[2].get('incident')
            incident_data['id'] = incident_data.get('incident_id')

            sort_all_list_incident_fields(incident_data)

            # deleting creation time as it keeps updating in the system
            del incident_data['creation_time']

            # handle unasignment
            if incident_data.get('assigned_user_mail') is None:
                handle_incoming_user_unassignment(incident_data)

            else:
                # handle owner sync
                sync_incoming_incident_owners(incident_data)

            # handle closed issue in XDR and handle outgoing error entry
            entries = [handle_incoming_closing_incident(incident_data)]

            reformatted_entries = []
            for entry in entries:
                if entry:
                    reformatted_entries.append(entry)

            incident_data['in_mirror_error'] = ''

            return GetRemoteDataResponse(
                mirrored_object=incident_data,
                entries=reformatted_entries
            )

        else:  # no need to update this incident
            incident_data = {
                'id': remote_args.remote_incident_id,
                'in_mirror_error': ""
            }

            return GetRemoteDataResponse(
                mirrored_object=incident_data,
                entries=[]
            )

    except Exception as e:
        demisto.debug(f"Error in XDR incoming mirror for incident {remote_args.remote_incident_id} \n"
                      f"Error message: {str(e)}")

        if "Rate limit exceeded" in str(e):
            return_error("API rate limit")

        if incident_data:
            incident_data['in_mirror_error'] = str(e)
            sort_all_list_incident_fields(incident_data)

            # deleting creation time as it keeps updating in the system
            del incident_data['creation_time']

        else:
            incident_data = {
                'id': remote_args.remote_incident_id,
                'in_mirror_error': str(e)
            }

        return GetRemoteDataResponse(
            mirrored_object=incident_data,
            entries=[]
        )


def handle_outgoing_incident_owner_sync(update_args):
    if 'owner' in update_args and demisto.params().get('sync_owners'):
        if update_args.get('owner'):
            user_info = demisto.findUser(username=update_args.get('owner'))
            if user_info:
                update_args['assigned_user_mail'] = user_info.get('email')
        else:
            # handle synced unassignment
            update_args['assigned_user_mail'] = None


def handle_user_unassignment(update_args):
    if ('assigned_user_mail' in update_args and update_args.get('assigned_user_mail') in ['None', 'null', '', None]) \
            or ('assigned_user_pretty_name' in update_args
                and update_args.get('assigned_user_pretty_name') in ['None', 'null', '', None]):
        update_args['unassign_user'] = 'true'
        update_args['assigned_user_mail'] = None
        update_args['assigned_user_pretty_name'] = None


def handle_outgoing_issue_closure(update_args, inc_status):
    if inc_status == 2:
        update_args['status'] = XSOAR_RESOLVED_STATUS_TO_XDR.get(update_args.get('closeReason', 'Other'))
        demisto.debug(f"Closing Remote XDR incident with status {update_args['status']}")
        update_args['resolve_comment'] = update_args.get('closeNotes', '')


def get_update_args(delta, inc_status):
    """Change the updated field names to fit the update command"""
    update_args = delta
    handle_outgoing_incident_owner_sync(update_args)
    handle_user_unassignment(update_args)
    if update_args.get('closingUserId'):
        handle_outgoing_issue_closure(update_args, inc_status)
    return update_args


def update_remote_system_command(client, args):
    remote_args = UpdateRemoteSystemArgs(args)

    if remote_args.delta:
        demisto.debug(f'Got the following delta keys {str(list(remote_args.delta.keys()))} to update XDR '
                      f'incident {remote_args.remote_incident_id}')
    try:
        if remote_args.incident_changed:
            update_args = get_update_args(remote_args.delta, remote_args.inc_status)

            update_args['incident_id'] = remote_args.remote_incident_id
            demisto.debug(f'Sending incident with remote ID [{remote_args.remote_incident_id}] to XDR\n')
            update_incident_command(client, update_args)

        else:
            demisto.debug(f'Skipping updating remote incident fields [{remote_args.remote_incident_id}] '
                          f'as it is not new nor changed')

        return remote_args.remote_incident_id

    except Exception as e:
        demisto.debug(f"Error in XDR outgoing mirror for incident {remote_args.remote_incident_id} \n"
                      f"Error message: {str(e)}")

        return remote_args.remote_incident_id


def fetch_incidents(client, first_fetch_time, integration_instance, last_run: dict = None, max_fetch: int = 10,
                    statuses: List = []):
    # Get the last fetch time, if exists
    last_fetch = last_run.get('time') if isinstance(last_run, dict) else None
    incidents_from_previous_run = last_run.get('incidents_from_previous_run', []) if isinstance(last_run,
                                                                                                dict) else []

    # Handle first time fetch, fetch incidents retroactively
    if last_fetch is None:
        last_fetch, _ = parse_date_range(first_fetch_time, to_timestamp=True)

    incidents = []
    if incidents_from_previous_run:
        raw_incidents = incidents_from_previous_run
    else:
        if statuses:
            raw_incidents = []
            for status in statuses:
                raw_incidents += client.get_incidents(gte_creation_time_milliseconds=last_fetch, status=status,
                                                      limit=max_fetch, sort_by_creation_time='asc')
            raw_incidents = sorted(raw_incidents, key=lambda inc: inc['creation_time'])
        else:
            raw_incidents = client.get_incidents(gte_creation_time_milliseconds=last_fetch, limit=max_fetch,
                                                 sort_by_creation_time='asc')

    # save the last 100 modified incidents to the integration context - for mirroring purposes
    client.save_modified_incidents_to_integration_context()

    # maintain a list of non created incidents in a case of a rate limit exception
    non_created_incidents: list = raw_incidents.copy()
    next_run = dict()
    try:
        # The count of incidents, so as not to pass the limit
        count_incidents = 0

        for raw_incident in raw_incidents:
            incident_id = raw_incident.get('incident_id')

            incident_data = get_incident_extra_data_command(client, {"incident_id": incident_id,
                                                                     "alerts_limit": 1000})[2].get('incident')

            sort_all_list_incident_fields(incident_data)

            incident_data['mirror_direction'] = MIRROR_DIRECTION.get(demisto.params().get('mirror_direction', 'None'),
                                                                     None)
            incident_data['mirror_instance'] = integration_instance
            incident_data['last_mirrored_in'] = int(datetime.now().timestamp() * 1000)

            description = raw_incident.get('description')
            occurred = timestamp_to_datestring(raw_incident['creation_time'], TIME_FORMAT + 'Z')
            incident = {
                'name': f'XDR Incident {incident_id} - {description}',
                'occurred': occurred,
                'rawJSON': json.dumps(incident_data),
            }

            if demisto.params().get('sync_owners') and incident_data.get('assigned_user_mail'):
                incident['owner'] = demisto.findUser(email=incident_data.get('assigned_user_mail')).get('username')

            # Update last run and add incident if the incident is newer than last fetch
            if raw_incident['creation_time'] > last_fetch:
                last_fetch = raw_incident['creation_time']

            incidents.append(incident)
            non_created_incidents.remove(raw_incident)

            count_incidents += 1
            if count_incidents == max_fetch:
                break

    except Exception as e:
        if "Rate limit exceeded" in str(e):
            demisto.info(f"Cortex XDR - rate limit exceeded, number of non created incidents is: "
                         f"'{len(non_created_incidents)}'.\n The incidents will be created in the next fetch")
        else:
            raise

    if non_created_incidents:
        next_run['incidents_from_previous_run'] = non_created_incidents
    else:
        next_run['incidents_from_previous_run'] = []

    next_run['time'] = last_fetch + 1

    return next_run, incidents


def delete_endpoints_command(client: Client, args: Dict[str, str]) -> Tuple[str, Any, Any]:
    endpoint_id_list: list = argToList(args.get('endpoint_ids'))

    client.delete_endpoints(endpoint_id_list)

    return f'Successfully deleted the following endpoints: {args.get("endpoint_ids")}', None, None


def get_policy_command(client: Client, args: Dict[str, str]) -> Tuple[str, dict, Any]:
    endpoint_id = args.get('endpoint_id')

    reply = client.get_policy(endpoint_id)
    context = {'endpoint_id': endpoint_id,
               'policy_name': reply.get('policy_name')}

    return (
        f'The policy name of endpoint: {endpoint_id} is: {reply.get("policy_name")}.',
        {
            f'{INTEGRATION_CONTEXT_BRAND}.Policy(val.endpoint_id == obj.endpoint_id)': context
        },
        reply
    )


def get_endpoint_device_control_violations_command(client: Client, args: Dict[str, str]) -> Tuple[str, dict, Any]:
    endpoint_ids: list = argToList(args.get('endpoint_ids'))
    type_of_violation = args.get('type')
    timestamp_gte: int = arg_to_timestamp(
        arg=args.get('timestamp_gte'),
        arg_name='timestamp_gte'
    )
    timestamp_lte: int = arg_to_timestamp(
        arg=args.get('timestamp_lte'),
        arg_name='timestamp_lte'
    )
    ip_list: list = argToList(args.get('ip_list'))
    vendor: list = argToList(args.get('vendor'))
    vendor_id: list = argToList(args.get('vendor_id'))
    product: list = argToList(args.get('product'))
    product_id: list = argToList(args.get('product_id'))
    serial: list = argToList(args.get('serial'))
    hostname: list = argToList(args.get('hostname'))
    violation_id_list: list = argToList(args.get('violation_id_list', ''))
    username: list = argToList(args.get('username'))

    violation_ids = [arg_to_int(arg=item, arg_name=str(item)) for item in violation_id_list]

    reply = client.get_endpoint_device_control_violations(
        endpoint_ids=endpoint_ids,
        type_of_violation=[type_of_violation],
        timestamp_gte=timestamp_gte,
        timestamp_lte=timestamp_lte,
        ip_list=ip_list,
        vendor=vendor,
        vendor_id=vendor_id,
        product=product,
        product_id=product_id,
        serial=serial,
        hostname=hostname,
        violation_ids=violation_ids,
        username=username
    )

    headers = ['date', 'hostname', 'platform', 'username', 'ip', 'type', 'violation_id', 'vendor', 'product',
               'serial']
    violations: list = copy.deepcopy(reply.get('violations'))  # type: ignore
    for violation in violations:
        timestamp: str = violation.get('timestamp')
        violation['date'] = timestamp_to_datestring(timestamp, TIME_FORMAT)

    return (
        tableToMarkdown(name='Endpoint Device Control Violation', t=violations, headers=headers,
                        headerTransform=string_to_table_header, removeNull=True),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.EndpointViolations(val.violation_id==obj.violation_id)': violations
        },
        reply
    )


def retrieve_file_details_command(client: Client, args):
    action_id_list = argToList(args.get('action_id', ''))
    action_id_list = [arg_to_int(arg=item, arg_name=str(item)) for item in action_id_list]

    result = []
    raw_result = []
    file_results = []
    endpoints_count = 0
    retrived_files_count = 0

    for action_id in action_id_list:
        data = client.retrieve_file_details(action_id)
        raw_result.append(data)

        for endpoint, link in data.items():
            endpoints_count += 1
            obj = {
                'action_id': action_id,
                'endpoint_id': endpoint
            }
            if link:
                retrived_files_count += 1
                obj['file_link'] = link
                file = client.get_file(file_link=link)
                file_results.append(fileResult(filename=f'{endpoint}_{retrived_files_count}.zip', data=file))
            result.append(obj)

    hr = f'### Action id : {args.get("action_id", "")} \n Retrieved {retrived_files_count} files from ' \
         f'{endpoints_count} endpoints. \n To get the exact action status run the xdr-action-status-get command'

    return_entry = {'Type': entryTypes['note'],
                    'ContentsFormat': formats['json'],
                    'Contents': raw_result,
                    'HumanReadable': hr,
                    'ReadableContentsFormat': formats['markdown'],
                    'EntryContext': {}
                    }
    return return_entry, file_results


def get_scripts_command(client: Client, args: Dict[str, str]) -> Tuple[str, dict, Any]:
    script_name: list = argToList(args.get('script_name'))
    description: list = argToList(args.get('description'))
    created_by: list = argToList(args.get('created_by'))
    windows_supported = args.get('windows_supported')
    linux_supported = args.get('linux_supported')
    macos_supported = args.get('macos_supported')
    is_high_risk = args.get('is_high_risk')
    offset = arg_to_int(arg=args.get('offset', 0), arg_name='offset')
    limit = arg_to_int(arg=args.get('limit', 50), arg_name='limit')

    result = client.get_scripts(
        name=script_name,
        description=description,
        created_by=created_by,
        windows_supported=[windows_supported],
        linux_supported=[linux_supported],
        macos_supported=[macos_supported],
        is_high_risk=[is_high_risk]
    )
    scripts = copy.deepcopy(result.get('scripts')[offset:(offset + limit)])  # type: ignore
    for script in scripts:
        timestamp = script.get('modification_date')
        script['modification_date_timestamp'] = timestamp
        script['modification_date'] = timestamp_to_datestring(timestamp, TIME_FORMAT)
    headers: list = ['name', 'description', 'script_uid', 'modification_date', 'created_by',
                     'windows_supported', 'linux_supported', 'macos_supported', 'is_high_risk']

    return (
        tableToMarkdown(name='Scripts', t=scripts, headers=headers, removeNull=True,
                        headerTransform=string_to_table_header),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.Scripts(val.script_uid == obj.script_uid)': scripts
        },
        result
    )


def get_script_metadata_command(client: Client, args: Dict[str, str]) -> Tuple[str, dict, Any]:
    script_uid = args.get('script_uid')

    reply = client.get_script_metadata(script_uid)
    script_metadata = copy.deepcopy(reply)

    timestamp = script_metadata.get('modification_date')
    script_metadata['modification_date_timestamp'] = timestamp
    script_metadata['modification_date'] = timestamp_to_datestring(timestamp, TIME_FORMAT)

    return (
        tableToMarkdown(name='Script Metadata', t=script_metadata, removeNull=True,
                        headerTransform=string_to_table_header),
        {
            f'{INTEGRATION_CONTEXT_BRAND}.ScriptMetadata(val.script_uid == obj.script_uid)': reply
        },
        reply
    )


def get_script_code_command(client: Client, args: Dict[str, str]) -> Tuple[str, dict, Any]:
    script_uid = args.get('script_uid')

    reply = client.get_script_code(script_uid)
    context = {
        'script_uid': script_uid,
        'code': reply
    }

    return (
        f'### Script code: \n ``` {str(reply)} ```',
        {
            f'{INTEGRATION_CONTEXT_BRAND}.ScriptCode(val.script_uid == obj.script_uid)': context
        },
        reply
    )


def run_script_command(client: Client, args: Dict) -> CommandResults:
    script_uid = args.get('script_uid')
    endpoint_ids = argToList(args.get('endpoint_ids'))
    timeout = arg_to_number(args.get('timeout', 600)) or 600
    incident_id = arg_to_number(args.get('incident_id'))
    if parameters := args.get('parameters'):
        try:
            parameters = json.loads(parameters)
        except json.decoder.JSONDecodeError as e:
            raise ValueError(f'The parameters argument is not in a valid JSON structure:\n{e}')
    else:
        parameters = {}
    response = client.run_script(script_uid, endpoint_ids, parameters, timeout, incident_id=incident_id)
    reply = response.get('reply')
    return CommandResults(
        readable_output=tableToMarkdown('Run Script', reply),
        outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptRun',
        outputs_key_field='action_id',
        outputs=reply,
        raw_response=response,
    )


def get_script_execution_status_command(client: Client, args: Dict) -> List[CommandResults]:
    action_ids = argToList(args.get('action_id', ''))
    command_results = []
    for action_id in action_ids:
        response = client.get_script_execution_status(action_id)
        reply = response.get('reply')
        reply['action_id'] = int(action_id)
        command_results.append(CommandResults(
            readable_output=tableToMarkdown(f'Script Execution Status - {action_id}', reply),
            outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptStatus',
            outputs_key_field='action_id',
            outputs=reply,
            raw_response=response,
        ))
    return command_results


def get_script_execution_results_command(client: Client, args: Dict) -> List[CommandResults]:
    action_ids = argToList(args.get('action_id', ''))
    command_results = []
    for action_id in action_ids:
        response = client.get_script_execution_results(action_id)
        results = response.get('reply', {}).get('results')
        context = {
            'action_id': int(action_id),
            'results': results,
        }
        command_results.append(CommandResults(
            readable_output=tableToMarkdown(f'Script Execution Results - {action_id}', results),
            outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptResult',
            outputs_key_field='action_id',
            outputs=context,
            raw_response=response,
        ))
    return command_results


def get_script_execution_result_files_command(client: Client, args: Dict) -> Dict:
    action_id = args.get('action_id', '')
    endpoint_id = args.get('endpoint_id')
    file_response = client.get_script_execution_result_files(action_id, endpoint_id)
    try:
        filename = file_response.headers.get('Content-Disposition').split('attachment; filename=')[1]
    except Exception as e:
        demisto.debug(f'Failed extracting filename from response headers - [{str(e)}]')
        filename = action_id + '.zip'
    return fileResult(filename, file_response.content)


def decode_dict_values(dict_to_decode: dict):
    """Decode JSON str values of a given dict.

    Args:
      dict_to_decode (dict): The dict to decode.

    """
    for key, value in dict_to_decode.items():
        # if value is a dictionary, we want to recursively decode it's values
        if isinstance(value, dict):
            decode_dict_values(value)
        # if value is a string, we want to try to decode it, if it cannot be decoded, we will move on.
        elif isinstance(value, str):
            try:
                dict_to_decode[key] = json.loads(value)
            except ValueError:
                continue


def filter_general_fields(alert: dict) -> dict:
    """filter only relevant general fields from a given alert.

    Args:
      alert (dict): The alert to filter

    Returns:
      dict: The filtered alert
    """

    updated_alert = {}
    updated_event = {}
    for field in ALERT_GENERAL_FIELDS:
        if field in alert:
            updated_alert[field] = alert.get(field)

    event = alert.get('raw_abioc', {}).get('event', {})
    if not event:
        return_warning('No XDR cloud analytics event.')
    else:
        for field in ALERT_EVENT_GENERAL_FIELDS:
            if field in event:
                updated_event[field] = event.get(field)
        updated_alert['event'] = updated_event
    return updated_alert


def filter_vendor_fields(alert: dict):
    """Remove non relevant fields from the alert event (filter by vendor: Amazon/google/Microsoft)

    Args:
      alert (dict): The alert to filter

    Returns:
      dict: The filtered alert
    """
    vendor_mapper = {
        'Amazon': ALERT_EVENT_AWS_FIELDS,
        'Google': ALERT_EVENT_GCP_FIELDS,
        'MSFT': ALERT_EVENT_AZURE_FIELDS,
    }
    event = alert.get('event', {})
    vendor = event.get('vendor')
    if vendor and vendor in vendor_mapper:
        raw_log = event.get('raw_log', {})
        if raw_log and isinstance(raw_log, dict):
            for key in list(raw_log):
                if key not in vendor_mapper[vendor]:
                    raw_log.pop(key)


def get_original_alerts_command(client: Client, args: Dict) -> CommandResults:
    alert_id_list = argToList(args.get('alert_ids', []))
    raw_response = client.get_original_alerts(alert_id_list)
    reply = copy.deepcopy(raw_response)
    alerts = reply.get('alerts', [])
    filtered_alerts = []
    for i, alert in enumerate(alerts):
        # decode raw_response
        try:
            alert['original_alert_json'] = safe_load_json(alert.get('original_alert_json', ''))
            # some of the returned JSON fields are double encoded, so it needs to be double-decoded.
            # example: {"x": "someValue", "y": "{\"z\":\"anotherValue\"}"}
            decode_dict_values(alert)
        except Exception:
            continue
        # remove original_alert_json field and add its content to alert.
        alert.update(
            alert.pop('original_alert_json', None))
        updated_alert = filter_general_fields(alert)
        if 'event' in updated_alert:
            filter_vendor_fields(updated_alert)
        filtered_alerts.append(updated_alert)

    return CommandResults(
        outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.OriginalAlert',
        outputs_key_field='internal_id',
        outputs=filtered_alerts,
        raw_response=raw_response,
    )


def run_script_execute_commands_command(client: Client, args: Dict) -> CommandResults:
    endpoint_ids = argToList(args.get('endpoint_ids'))
    incident_id = arg_to_number(args.get('incident_id'))
    timeout = arg_to_number(args.get('timeout', 600)) or 600
    parameters = {'commands_list': argToList(args.get('commands'))}
    response = client.run_script('a6f7683c8e217d85bd3c398f0d3fb6bf', endpoint_ids, parameters, timeout, incident_id)
    reply = response.get('reply')
    return CommandResults(
        readable_output=tableToMarkdown('Run Script Execute Commands', reply),
        outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptRun',
        outputs_key_field='action_id',
        outputs=reply,
        raw_response=response,
    )


def run_script_delete_file_command(client: Client, args: Dict) -> List[CommandResults]:
    endpoint_ids = argToList(args.get('endpoint_ids'))
    incident_id = arg_to_number(args.get('incident_id'))
    timeout = arg_to_number(args.get('timeout', 600)) or 600
    file_paths = argToList(args.get('file_path'))
    all_files_response = []
    for file_path in file_paths:
        parameters = {'file_path': file_path}
        response = client.run_script('548023b6e4a01ec51a495ba6e5d2a15d', endpoint_ids, parameters, timeout, incident_id)
        reply = response.get('reply')
        all_files_response.append(CommandResults(
            readable_output=tableToMarkdown(f'Run Script Delete File on {file_path}', reply),
            outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptRun',
            outputs_key_field='action_id',
            outputs=reply,
            raw_response=response,
        ))
    return all_files_response


def run_script_file_exists_command(client: Client, args: Dict) -> List[CommandResults]:
    endpoint_ids = argToList(args.get('endpoint_ids'))
    incident_id = arg_to_number(args.get('incident_id'))
    timeout = arg_to_number(args.get('timeout', 600)) or 600
    file_paths = argToList(args.get('file_path'))
    all_files_response = []
    for file_path in file_paths:
        parameters = {'path': file_path}
        response = client.run_script('414763381b5bfb7b05796c9fe690df46', endpoint_ids, parameters, timeout, incident_id)
        reply = response.get('reply')
        all_files_response.append(CommandResults(
            readable_output=tableToMarkdown(f'Run Script File Exists on {file_path}', reply),
            outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptRun',
            outputs_key_field='action_id',
            outputs=reply,
            raw_response=response,
        ))
    return all_files_response


def run_script_kill_process_command(client: Client, args: Dict) -> List[CommandResults]:
    endpoint_ids = argToList(args.get('endpoint_ids'))
    incident_id = arg_to_number(args.get('incident_id'))
    timeout = arg_to_number(args.get('timeout', 600)) or 600
    processes_names = argToList(args.get('process_name'))
    all_processes_response = []
    for process_name in processes_names:
        parameters = {'process_name': process_name}
        response = client.run_script('fd0a544a99a9421222b4f57a11839481', endpoint_ids, parameters, timeout, incident_id)
        reply = response.get('reply')
        all_processes_response.append(CommandResults(
            readable_output=tableToMarkdown(f'Run Script Kill Process on {process_name}', reply),
            outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.ScriptRun',
            outputs_key_field='action_id',
            outputs=reply,
            raw_response=response,
        ))

    return all_processes_response


def get_endpoints_by_status_command(client: Client, args: Dict) -> CommandResults:
    status = args.get('status')

    last_seen_gte = arg_to_timestamp(
        arg=args.get('last_seen_gte'),
        arg_name='last_seen_gte'
    )

    last_seen_lte = arg_to_timestamp(
        arg=args.get('last_seen_lte'),
        arg_name='last_seen_lte'
    )

    endpoints_count, raw_res = client.get_endpoints_by_status(status, last_seen_gte=last_seen_gte, last_seen_lte=last_seen_lte)

    ec = {'status': status, 'count': endpoints_count}

    return CommandResults(
        readable_output=f'{status} endpoints count: {endpoints_count}',
        outputs_prefix=f'{INTEGRATION_CONTEXT_BRAND}.EndpointsStatus',
        outputs_key_field='status',
        outputs=ec,
        raw_response=raw_res)


def main():
    """
    Executes an integration command
    """
    command = demisto.command()
    LOG(f'Command being called is {command}')

    api_key = demisto.params().get('apikey')
    api_key_id = demisto.params().get('apikey_id')
    first_fetch_time = demisto.params().get('fetch_time', '3 days')
    base_url = urljoin(demisto.params().get('url'), '/public_api/v1')
    proxy = demisto.params().get('proxy')
    verify_cert = not demisto.params().get('insecure', False)
    statuses = demisto.params().get('status')

    try:
        timeout = int(demisto.params().get('timeout', 120))
    except ValueError as e:
        demisto.debug(f'Failed casting timeout parameter to int, falling back to 120 - {e}')
        timeout = 120
    try:
        max_fetch = int(demisto.params().get('max_fetch', 10))
    except ValueError as e:
        demisto.debug(f'Failed casting max fetch parameter to int, falling back to 10 - {e}')
        max_fetch = 10

    nonce = "".join([secrets.choice(string.ascii_letters + string.digits) for _ in range(64)])
    timestamp = str(int(datetime.now(timezone.utc).timestamp()) * 1000)
    auth_key = "%s%s%s" % (api_key, nonce, timestamp)
    auth_key = auth_key.encode("utf-8")
    api_key_hash = hashlib.sha256(auth_key).hexdigest()

    headers = {
        "x-xdr-timestamp": timestamp,
        "x-xdr-nonce": nonce,
        "x-xdr-auth-id": str(api_key_id),
        "Authorization": api_key_hash
    }

    client = Client(
        base_url=base_url,
        proxy=proxy,
        verify=verify_cert,
        headers=headers,
        timeout=timeout
    )

    args = demisto.args()
    args["integration_context_brand"] = INTEGRATION_CONTEXT_BRAND
    args["integration_name"] = INTEGRATION_NAME

    try:
        if command == 'test-module':
            client.test_module(first_fetch_time)
            demisto.results('ok')

        elif command == 'fetch-incidents':
            integration_instance = demisto.integrationInstance()
            next_run, incidents = fetch_incidents(client, first_fetch_time, integration_instance, demisto.getLastRun(),
                                                  max_fetch, statuses)
            demisto.setLastRun(next_run)
            demisto.incidents(incidents)

        elif command == 'xdr-get-incidents':
            return_outputs(*get_incidents_command(client, args))

        elif command == 'xdr-get-incident-extra-data':
            return_outputs(*get_incident_extra_data_command(client, args))

        elif command == 'xdr-update-incident':
            return_outputs(*update_incident_command(client, args))

        elif command == 'xdr-get-endpoints':
            return_results(get_endpoints_command(client, args))

        elif command == 'xdr-insert-parsed-alert':
            return_outputs(*insert_parsed_alert_command(client, args))

        elif command == 'xdr-insert-cef-alerts':
            return_outputs(*insert_cef_alerts_command(client, args))

        elif command == 'xdr-isolate-endpoint':
            return_results(isolate_endpoint_command(client, args))

        elif command == 'xdr-endpoint-isolate':
            polling_args = {
                **args,
                "endpoint_id_list": args.get('endpoint_id')
            }
            return_results(run_polling_command(client=client,
                                               args=polling_args,
                                               cmd="xdr-endpoint-isolate",
                                               command_function=isolate_endpoint_command,
                                               command_decision_field="action_id",
                                               results_function=get_endpoints_command,
                                               polling_field="is_isolated",
                                               polling_value=["AGENT_ISOLATED"],
                                               stop_polling=True))

        elif command == 'xdr-unisolate-endpoint':
            return_results(unisolate_endpoint_command(client, args))

        elif command == 'xdr-endpoint-unisolate':
            polling_args = {
                **args,
                "endpoint_id_list": args.get('endpoint_id')
            }
            return_results(run_polling_command(client=client,
                                               args=polling_args,
                                               cmd="xdr-endpoint-unisolate",
                                               command_function=unisolate_endpoint_command,
                                               command_decision_field="action_id",
                                               results_function=get_endpoints_command,
                                               polling_field="is_isolated",
                                               polling_value=["AGENT_UNISOLATED",
                                                              "CANCELLED",
                                                              "ֿPENDING_ABORT",
                                                              "ABORTED",
                                                              "EXPIRED",
                                                              "COMPLETED_PARTIAL",
                                                              "COMPLETED_SUCCESSFULLY",
                                                              "FAILED",
                                                              "TIMEOUT"],
                                               stop_polling=True))

        elif command == 'xdr-get-distribution-url':
            return_outputs(*get_distribution_url_command(client, args))

        elif command == 'xdr-get-create-distribution-status':
            return_outputs(*get_distribution_status_command(client, args))

        elif command == 'xdr-get-distribution-versions':
            return_outputs(*get_distribution_versions_command(client))

        elif command == 'xdr-create-distribution':
            return_outputs(*create_distribution_command(client, args))

        elif command == 'xdr-get-audit-management-logs':
            return_outputs(*get_audit_management_logs_command(client, args))

        elif command == 'xdr-get-audit-agent-reports':
            return_outputs(*get_audit_agent_reports_command(client, args))

        elif command == 'xdr-blacklist-files':
            return_outputs(*blacklist_files_command(client, args))

        elif command == 'xdr-whitelist-files':
            return_outputs(*whitelist_files_command(client, args))

        elif command == 'xdr-quarantine-files':
            return_outputs(*quarantine_files_command(client, args))

        elif command == 'xdr-get-quarantine-status':
            return_outputs(*get_quarantine_status_command(client, args))

        elif command == 'xdr-restore-file':
            return_outputs(*restore_file_command(client, args))

        elif command == 'xdr-endpoint-scan':
            return_results(endpoint_scan_command(client, args))

        elif command == 'xdr-endpoint-scan-execute':
            return_results(run_polling_command(client=client,
                                               args=args,
                                               cmd="xdr-endpoint-scan-execute",
                                               command_function=endpoint_scan_command,
                                               command_decision_field="action_id",
                                               results_function=action_status_get_command,
                                               polling_field="status",
                                               polling_value=["PENDING",
                                                              "IN_PROGRESS",
                                                              "PENDING_ABORT"]))

        elif command == 'xdr-endpoint-scan-abort':
            return_outputs(*endpoint_scan_abort_command(client, args))

        elif command == 'get-mapping-fields':
            return_results(get_mapping_fields_command())

        elif command == 'get-remote-data':
            return_results(get_remote_data_command(client, args))

        elif command == 'update-remote-system':
            return_results(update_remote_system_command(client, args))

        elif command == 'xdr-delete-endpoints':
            return_outputs(*delete_endpoints_command(client, args))

        elif command == 'xdr-get-policy':
            return_outputs(*get_policy_command(client, args))

        elif command == 'xdr-get-endpoint-device-control-violations':
            return_outputs(*get_endpoint_device_control_violations_command(client, args))

        elif command == 'xdr-retrieve-files':
            return_results(retrieve_files_command(client, args))

        elif command == 'xdr-file-retrieve':
            return_results(run_polling_command(client=client,
                                               args=args,
                                               cmd="xdr-file-retrieve",
                                               command_function=retrieve_files_command,
                                               command_decision_field="action_id",
                                               results_function=action_status_get_command,
                                               polling_field="status",
                                               polling_value=["PENDING",
                                                              "IN_PROGRESS",
                                                              "PENDING_ABORT"]))

        elif command == 'xdr-retrieve-file-details':
            return_entry, file_results = retrieve_file_details_command(client, args)
            demisto.results(return_entry)
            if file_results:
                demisto.results(file_results)

        elif command == 'xdr-get-scripts':
            return_outputs(*get_scripts_command(client, args))

        elif command == 'xdr-get-script-metadata':
            return_outputs(*get_script_metadata_command(client, args))

        elif command == 'xdr-get-script-code':
            return_outputs(*get_script_code_command(client, args))

        elif command == 'xdr-action-status-get':
            return_results(action_status_get_command(client, args))

        elif command == 'get-modified-remote-data':
            return_results(get_modified_remote_data_command(client, demisto.args()))

        elif command == 'xdr-run-script':
            return_results(run_script_command(client, args))

        elif command == 'xdr-run-snippet-code-script':
            return_results(run_snippet_code_script_command(client, args))

        elif command == 'xdr-snippet-code-script-execute':
            return_results(run_polling_command(client=client,
                                               args=args,
                                               cmd="xdr-snippet-code-script-execute",
                                               command_function=run_snippet_code_script_command,
                                               command_decision_field="action_id",
                                               results_function=action_status_get_command,
                                               polling_field="status",
                                               polling_value=["PENDING",
                                                              "IN_PROGRESS",
                                                              "PENDING_ABORT"]))

        elif command == 'xdr-get-script-execution-status':
            return_results(get_script_execution_status_command(client, args))

        elif command == 'xdr-get-script-execution-results':
            return_results(get_script_execution_results_command(client, args))

        elif command == 'xdr-get-script-execution-result-files':
            return_results(get_script_execution_result_files_command(client, args))

        elif command == 'xdr-get-cloud-original-alerts':
            return_results(get_original_alerts_command(client, args))

        elif command == 'xdr-run-script-execute-commands':
            return_results(run_script_execute_commands_command(client, args))

        elif command == 'xdr-run-script-delete-file':
            return_results(run_script_delete_file_command(client, args))

        elif command == 'xdr-run-script-file-exists':
            return_results(run_script_file_exists_command(client, args))

        elif command == 'xdr-run-script-kill-process':
            return_results(run_script_kill_process_command(client, args))

        elif command == 'endpoint':
            return_results(endpoint_command(client, args))

        elif command == 'xdr-get-endpoints-by-status':
            return_results(get_endpoints_by_status_command(client, args))

    except Exception as err:
        demisto.error(traceback.format_exc())
        return_error(str(err))


if __name__ in ('__main__', '__builtin__', 'builtins'):
    main()
