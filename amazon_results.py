"""Count only explicitly acknowledged Amazon batch operations."""
def batch_outcome(response, key, submitted):
    body = response.get(key, {}) if isinstance(response, dict) else {}
    successes = body.get('success', []) if isinstance(body, dict) else []
    errors = body.get('error', body.get('errors', [])) if isinstance(body, dict) else []
    successes = successes if isinstance(successes, list) else []
    errors = errors if isinstance(errors, list) else []
    count = min(submitted, len(successes))
    unresolved = max(0, submitted - count - len(errors))
    return {'submitted': submitted, 'accepted': count, 'failed': len(errors) + unresolved,
            'errors': errors, 'unconfirmed': unresolved, 'success': count == submitted}


def create_keywords_verified(client, rows):
    result = {'submitted': len(rows), 'accepted': 0, 'failed': 0, 'errors': [], 'unconfirmed': 0}
    for offset in range(0, len(rows), 100):
        chunk = rows[offset:offset + 100]
        outcome = batch_outcome(client.create_keywords(chunk), 'keywords', len(chunk))
        for key in ('accepted', 'failed', 'unconfirmed'):
            result[key] += outcome[key]
        result['errors'].extend(outcome['errors'])
    result['success'] = result['accepted'] == len(rows)
    return result
