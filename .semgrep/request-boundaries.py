"""Offline rule fixtures: never executed or imported by the application."""

def violations(row, client):
    # ruleid: aiac-request-state-owner
    row.request_status = 'FAILED'
    # ruleid: aiac-request-state-owner
    setattr(row, 'request_status', 'RESULT')
    # ruleid: aiac-request-state-owner
    ResearchCandidate(request_status='RESULT')
    # ruleid: aiac-direct-simulation-post
    client._safe_api_call('POST', '/simulations', json={})
    # ruleid: aiac-direct-simulation-post
    client.post('/simulations', json={})
    # ruleid: aiac-direct-simulation-post
    client._simulate_single_once('fixture', {})


def permitted_reads(row, client):
    # ok: aiac-request-state-owner
    pending = row.request_status == 'UNKNOWN'
    # ok: aiac-direct-simulation-post
    client._safe_api_call('GET', '/simulations/original')
    return pending
