"""Amazon creates these four AUTO clauses with the ad group; never POST them.

Reference: Amazon Ads SP automatic-targeting guide, /sp/targets/list and PUT.
CLOSE_MATCH is a recommendation enum, not a targeting-clause expression enum.
"""
from auto_launch_coordinator import LaunchBlocked

AUTO_EXPRESSIONS = {'QUERY_HIGH_REL_MATCHES', 'QUERY_BROAD_REL_MATCHES',
                    'ASIN_ACCESSORY_RELATED', 'ASIN_SUBSTITUTE_RELATED'}

def verified_auto_targets(client, cid, gid):
    rows = [t for t in client.list_targets(str(cid)) if t.get('state') != 'ARCHIVED']
    found = {}
    for target in rows:
        expression = target.get('expression') or []
        kind = expression[0].get('type') if len(expression) == 1 else None
        if (kind not in AUTO_EXPRESSIONS or kind in found or not target.get('targetId')
                or str(target.get('campaignId')) != str(cid) or str(target.get('adGroupId')) != str(gid)
                or target.get('expressionType') != 'AUTO' or target.get('state') != 'ENABLED'):
            raise LaunchBlocked('Automatic targeting identity mismatch')
        found[kind] = target
    if set(found) != AUTO_EXPRESSIONS or len({str(t['targetId']) for t in found.values()}) != 4:
        raise LaunchBlocked('Four automatic target IDs not yet verified')
    return found
