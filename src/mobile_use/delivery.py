"""Pure response views and snapshot-bound pagination. Never reads a device."""
from base64 import b64decode, urlsafe_b64encode
from dataclasses import asdict
import hashlib
import json


def _filter_key(focus):
    query = {k: v for k, v in asdict(focus).items() if k != 'max_elements'} if focus else {}
    return hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:16]


def page_options(limit, cursor, observation_id, focus):
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250):
        raise ValueError('limit must be an integer in 1..250')
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
                raise ValueError()
            binding, query, offset = json.loads(b64decode(cursor, altchars=b'-_', validate=True))
            if (binding != observation_id or query != _filter_key(focus) or
                    type(offset) is not int or not 0 <= offset <= 2**31 - 1):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError('Cursor must belong to this observation and the same focus text/bounds') from exc
    return min(limit or 250, focus.max_elements if focus else 250), offset


def _value(element, key):
    return element.get(key, '') if isinstance(element, dict) else getattr(element, key)


def element_page(elements, observation_id, focus=None, limit=None, cursor=None):
    """Select original records/Elements before serialization; never truncate the search."""
    page_limit, offset = page_options(limit, cursor, observation_id, focus)
    matches = []
    for e in elements:
        if focus:
            if focus.text is not None and not any(focus.text in _value(e, k) for k in ('text', 'description')):
                continue
            if focus.bounds is not None:
                l, t, r, b = _value(e, 'bounds')
                x1, y1, x2, y2 = focus.bounds
                if not (l < x2 and x1 < r and t < y2 and y1 < b):
                    continue
        matches.append(e)
    if cursor is not None and (offset > len(matches) or (offset == len(matches) and offset != 0)):
        raise ValueError('Cursor offset is outside the matching cached elements')
    selected = matches[offset:offset + page_limit]
    end = offset + len(selected)
    next_cursor = None
    if end < len(matches):
        next_cursor = urlsafe_b64encode(json.dumps(
            [observation_id, _filter_key(focus), end], separators=(',', ':')).encode()).decode()
    selection = ({**asdict(focus), 'matched_elements': len(matches),
                  'matches_truncated': len(selected) < len(matches)} if focus else None)
    pagination = ({'offset': offset, 'limit': page_limit, 'matched_elements': len(matches),
                   'returned_elements': len(selected), 'next_cursor': next_cursor}
                  if cursor is not None or limit is not None or len(matches) > page_limit else None)
    return selected, selection, pagination, len(selected) < len(matches)


def brief_observation(metadata):
    # Keep every delivered element and all actionable/consistency evidence. No delta.
    result = {k: v for k, v in metadata.items() if k not in (
        'summary', 'tree_diagnostics', 'stability_scope', 'coordinate_space', 'image_reasons')}
    result['capture'] = {k: v for k, v in metadata['capture'].items()
                         if k in ('tree_at', 'image_at', 'checked_at')}
    # Legacy motion regions affect which actions are allowed; never discard them.
    if metadata.get('validation_mode') == 'target':
        result.pop('stable', None)
        if not result.get('unstable_regions'):
            result.pop('unstable_regions', None)
    return result


def brief_result(data):
    result = {k: v for k, v in data.items() if k not in ('timings', 'reads', 'execution_wall_ms')}
    if 'wait' in result:
        result['wait'] = {k: v for k, v in result['wait'].items() if k != 'scope'}
    # Failures, diagnostic observations and dry-run previews keep their evidence.
    if ('error' not in data and not data.get('dry_run') and
            data.get('input_status') == 'sent' and data.get('observation_status') != 'inconsistent'):
        result.pop('action', None)
        result.pop('result', None)
        result.pop('observation_status', None)
        if result.get('validation'):
            result['validation'] = {k: v for k, v in result['validation'].items()
                                    if k in ('kind', 'status', 'previous_bounds', 'resolved_bounds', 'context_binding')}
        result.pop('settle', None)
    if 'continuation' in result:
        result['continuation'] = dict(result['continuation'])
    for parent, key in ((result, 'navigation'), (result.get('continuation', {}), 'navigation')):
        if key in parent:
            nav = parent[key]
            parent[key] = {**nav, 'steps': [{k: v for k, v in step.items() if k not in ('action', 'execution_ms')}
                                           for step in nav['steps']]}
    return result
