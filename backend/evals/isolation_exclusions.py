"""Validate read-only production exclusions before an independent lab sends."""
import re

from backend.services.ppa_feedback import digest
from backend.services.ppa_isolation import isolated_signature


def validate_exclusions(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'manifests', 'exclusions_sha256'}
            or value.get('version') != 1 or not isinstance(value.get('manifests'), list)
            or value.get('exclusions_sha256') != digest({k: v for k, v in value.items()
                                                       if k != 'exclusions_sha256'})):
        raise ValueError('ISOLATION_EXCLUSIONS_INVALID')
    identities = set()
    for record in value['manifests']:
        if (not isinstance(record, dict)
                or set(record) != {'task_id', 'run_id', 'manifest_sha256', 'blocked_signatures'}
                or any(type(record.get(key)) is not int or record[key] <= 0 for key in ('task_id', 'run_id'))
                or not re.fullmatch('[0-9a-f]{64}', str(record.get('manifest_sha256')))
                or not isinstance(record.get('blocked_signatures'), list)
                or not record['blocked_signatures']):
            raise ValueError('ISOLATION_EXCLUSIONS_INVALID')
        if record['run_id'] in identities:
            raise ValueError('ISOLATION_EXCLUSIONS_INVALID')
        identities.add(record['run_id'])
        for signature in record['blocked_signatures']:
            if (not isinstance(signature, dict) or set(signature) != {'expression_hash', 'settings_hash'}
                    or any(not re.fullmatch('[0-9a-f]{64}', str(item)) for item in signature.values())):
                raise ValueError('ISOLATION_EXCLUSIONS_INVALID')
    return value


def blocked(exclusions, expression, settings):
    key = isolated_signature(expression, settings)
    return any(key in record['blocked_signatures'] for record in validate_exclusions(exclusions)['manifests'])


def exclusion_gate(frozen, live, expression=None, settings=None):
    try:
        validate_exclusions(frozen)
        validate_exclusions(live)
    except ValueError:
        return 'ISOLATION_EXCLUSIONS_INVALID'
    if frozen['exclusions_sha256'] != live['exclusions_sha256']:
        return 'ISOLATION_EXCLUSIONS_CHANGED'
    if expression is not None and blocked(live, expression, settings):
        return 'ISOLATED_SIGNATURE'
    return 'READY'
