"""Vorherige Kununu-Ergebnisse erhalten, ohne alte Bewertungen als neue auszugeben."""
import hashlib
import json
from copy import deepcopy

from .research_inputs import write_json
from .output_contract import prepare, score


def restore_factor_scores(snapshot, previous, source):
    """Nur Lücken unveränderter Reviews füllen, mit Herkunft je ergänzter Teilnote."""
    result = deepcopy(snapshot)
    if snapshot.get('status') not in ('ok', 'partial') or previous.get('status') not in ('ok', 'partial'):
        return result
    profile = snapshot.get('profile') or {}
    old_profile = previous.get('profile') or {}
    if not profile.get('url') or profile.get('url') != old_profile.get('url'):
        return result
    originals = {row.get('id'): row for row in previous.get('reviews', []) if isinstance(row, dict)}
    identity_fields = ('id', 'published_at', 'rating', 'recommended', 'review_type', 'title', 'content')
    for row in result.get('reviews', []):
        old = originals.get(row.get('id'))
        if not old or any(key not in row or key not in old or row[key] != old[key] for key in identity_fields):
            continue
        scores, old_scores = row.get('factor_scores', {}), old.get('factor_scores', {})
        if not isinstance(scores, dict) or not isinstance(old_scores, dict):
            continue
        # Ein abweichender bekannter Wert kann eine bearbeitete Bewertung anzeigen.
        if any(score(v) and score(old_scores.get(k)) and v != old_scores[k] for k, v in scores.items()):
            continue
        for factor, value in old_scores.items():
            if scores.get(factor) is None and score(value):
                scores[factor] = value
                row.setdefault('factor_score_sources', {})[factor] = deepcopy(
                    old.get('factor_score_sources', {}).get(factor) or
                    dict(snapshot=str(source), observed_at=old_profile.get('observed_at')))
        row['factor_scores'] = scores
    return result


def save_snapshot(path, snapshot):
    previous = None
    if path.is_file():
        previous = path.read_bytes()
        digest = hashlib.sha256(previous).hexdigest()
        history = path.parent / 'history' / (digest + '.json')
        try:
            old = json.loads(previous)
        except (ValueError, UnicodeError):
            old = None
        if isinstance(old, dict):
            snapshot = restore_factor_scores(snapshot, old, history)
    snapshot = prepare('kununu', snapshot)
    if previous is not None:
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists():
            history.write_bytes(previous)
        snapshot = dict(snapshot, previous_snapshot_path=str(history))
    write_json(path, snapshot)
    return snapshot
