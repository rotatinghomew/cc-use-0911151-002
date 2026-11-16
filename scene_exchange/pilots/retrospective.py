from __future__ import annotations

"""Effect review for a pilot round and renewal opinions.

Selection into a pilot is not a random experiment.  The outcomes of selected
demands cannot by themselves show that the matching rules worked, because the
pre-selection conditions may already explain a high landing rate.  This module
therefore keeps the pre-selection demand snapshot and compares selected demands
with unselected demands from the same sector and a similar pre-selection score.

The review deliberately distinguishes three things:

* demands whose observation window has not elapsed are ``pending_observation``
  and are never counted as failures;
* demands with an elapsed window but no reported result are ``no_result`` and
  are disclosed as a data gap rather than a failure;
* only observed demands enter the start / exit / benefit comparison.

The resulting renewal opinion has a verifiable basis: the observation batches,
raw observation fingerprints, effective observations and comparison rules are
all fingerprinted.  A renewal decision copies that basis and is immutable, so a
later monthly report never rewrites an already-made decision.  Supplemental
results, corrections, retractions and pilot exits refresh only opinions that
have not yet been reviewed.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from scene_exchange.intake.demand_contract import (
    ContractError,
    DomainRecord,
    canonical_json,
    finite_number,
    normalized_text,
    stable_hash,
    utc_now,
)
from scene_exchange.portfolio.selection import record_score

ALGORITHM_VERSION = 'effect_review.v1'
DEFAULT_OBSERVATION_DAYS = 90
DEFAULT_SCORE_BAND = 0.12
OBSERVATION_KINDS = ('initial', 'supplemental', 'correction', 'retraction')
RECOMMENDATIONS = ('renew', 'conditional_renew', 'do_not_renew', 'insufficient_evidence')
RENEWAL_OUTCOMES = ('renew', 'conditional_renew', 'do_not_renew', 'defer')

DEFAULT_RULES: dict[str, Any] = {
    'require_same_sector': True,
    'score_band': DEFAULT_SCORE_BAND,
    'min_selected_observed': 1,
    'min_unselected_observed': 1,
    'benefit_margin': 0.02,
    'exit_margin': 0.05,
    'start_margin': 0.10,
    'pre_selection_gap_warning': DEFAULT_SCORE_BAND,
}

RETROSPECTIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_selections(
    demand_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    sector TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK(selected IN (0,1)),
    score REAL NOT NULL,
    intake_json TEXT NOT NULL,
    intake_fingerprint TEXT NOT NULL,
    observation_days INTEGER NOT NULL CHECK(observation_days > 0),
    selected_at TEXT NOT NULL,
    observation_start TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(round_id, demand_id)
);
CREATE INDEX IF NOT EXISTS idx_pilot_selections_round
    ON pilot_selections(round_id, selected, sector);
CREATE TABLE IF NOT EXISTS pilot_observation_batches(
    batch_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    period TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    observation_fingerprints_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_observation_batches_round
    ON pilot_observation_batches(round_id, recorded_at);
CREATE TABLE IF NOT EXISTS pilot_observations(
    observation_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    demand_id TEXT NOT NULL,
    period TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('initial','supplemental','correction','retraction')),
    supersedes TEXT,
    started INTEGER,
    exited INTEGER,
    exit_reason TEXT,
    benefit REAL,
    note TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES pilot_observation_batches(batch_id)
);
CREATE INDEX IF NOT EXISTS idx_pilot_observations_batch
    ON pilot_observations(batch_id, sequence);
CREATE INDEX IF NOT EXISTS idx_pilot_observations_round
    ON pilot_observations(round_id, demand_id, sequence);
CREATE TABLE IF NOT EXISTS renewal_opinions(
    opinion_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','superseded','decided')),
    recommendation TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    basis_fingerprint TEXT NOT NULL,
    comparison_fingerprint TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    decided_at TEXT,
    decision_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_renewal_opinions_round
    ON renewal_opinions(round_id, status, created_at);
CREATE TABLE IF NOT EXISTS renewal_decisions(
    decision_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    opinion_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    approver TEXT NOT NULL,
    note TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    basis_fingerprint TEXT NOT NULL,
    comparison_fingerprint TEXT NOT NULL,
    observation_batch_ids_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_renewal_decisions_round
    ON renewal_decisions(round_id, decided_at);
"""


def prepare_retrospective(connection: Any) -> None:
    # Split statements manually: executescript() would implicitly commit a
    # caller's open transaction and break the repository's atomicity guarantees.
    for statement in RETROSPECTIVE_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


# ---------------------------------------------------------------------------
# time and small helpers


def parse_time(value: Any, field: str = 'time') -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = normalized_text(value)
        if not text:
            raise ContractError(f'{field} is required')
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ContractError(f'invalid {field}: {value!r}') from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def normalize_time(value: Any, field: str = 'time') -> str:
    return format_time(parse_time(value, field))


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = normalized_text(value)
    if text in {'true', 'yes', 'y', '1', 'started', 'exited', 'withdrawn'}:
        return True
    if text in {'false', 'no', 'n', '0', 'not-started', 'none'}:
        return False
    raise ContractError(f'invalid boolean value: {value!r}')


def _optional_number(value: Any) -> float | None:
    if value is None or value == '':
        return None
    return finite_number(value)


def _bool_int(value: Any) -> int | None:
    parsed = _optional_bool(value)
    return None if parsed is None else int(parsed)


def _iso_or_none(value: Any) -> str | None:
    if value is None or value == '':
        return None
    return normalize_time(value)


def merge_rules(rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(DEFAULT_RULES)
    for key, value in dict(rules or {}).items():
        if key not in merged:
            raise ContractError(f'unknown review rule: {key}')
        merged[key] = value
    if merged['require_same_sector'] is not True:
        raise ContractError('cross-sector comparison requires separate authorization')
    for key in ('score_band', 'benefit_margin', 'exit_margin', 'start_margin', 'pre_selection_gap_warning'):
        merged[key] = finite_number(merged[key])
        if merged[key] < 0 or merged[key] > 1:
            raise ContractError(f'review rule {key} must be between 0 and 1')
    for key in ('min_selected_observed', 'min_unselected_observed'):
        value = merged[key]
        if type(value) is not int or value < 0:
            raise ContractError(f'review rule {key} must be a non-negative integer')
        merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# immutable input fingerprints


def score_intake(demand_id: str, intake: Mapping[str, Any]) -> float:
    record = DomainRecord(demand_id, dict(intake))
    return float(record_score(record, {})['score'])


def selection_semantic(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'demand_id': row['demand_id'],
        'round_id': row['round_id'],
        'sector': normalized_text(row['sector']),
        'selected': bool(row['selected']),
        'score': finite_number(row['score']),
        'intake_fingerprint': row['intake_fingerprint'],
        'observation_days': int(row['observation_days']),
        'selected_at': normalize_time(row['selected_at'], 'selected_at'),
        'observation_start': _iso_or_none(row.get('observation_start')),
    }


def selection_fingerprint(row: Mapping[str, Any]) -> str:
    body = selection_semantic(row) | {'batch_id': row['batch_id']}
    return stable_hash(body)


def selection_batch_fingerprint(batch_id: str, selections: Sequence[Mapping[str, Any]]) -> str:
    return stable_hash({
        'batch_id': batch_id,
        'selections': sorted(
            (row['demand_id'], selection_fingerprint(row)) for row in selections
        ),
    })


def observation_semantic(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'demand_id': row['demand_id'],
        'period': normalized_text(row['period']),
        'kind': row['kind'],
        'supersedes': row.get('supersedes') or None,
        'started': _optional_bool(row.get('started')),
        'exited': _optional_bool(row.get('exited')),
        'exit_reason': normalized_text(row.get('exit_reason')) or None,
        'benefit': _optional_number(row.get('benefit')),
        'note': normalized_text(row.get('note')),
    }


def observation_id_for(row: Mapping[str, Any]) -> str:
    return stable_hash(observation_semantic(row) | {'round_id': row['round_id'], 'batch_id': row['batch_id']})


def observation_fingerprint(row: Mapping[str, Any]) -> str:
    return stable_hash(observation_semantic(row) | {
        'observation_id': row['observation_id'],
        'batch_id': row['batch_id'],
        'round_id': row['round_id'],
        'recorded_at': normalize_time(row['recorded_at'], 'recorded_at'),
    })


def batch_fingerprint(row: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]) -> str:
    return stable_hash({
        'batch_id': row['batch_id'],
        'round_id': row['round_id'],
        'period': normalized_text(row['period']),
        'kind': row['kind'],
        'note': normalized_text(row.get('note')),
        'recorded_at': normalize_time(row['recorded_at'], 'recorded_at'),
        'observations': sorted(
            (observation['observation_id'], observation['fingerprint'])
            for observation in observations
        ),
    })


# ---------------------------------------------------------------------------
# effective observation set (supplemental merge, correction, retraction)


def effective_observations(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the merged active observation per demand.

    Observations are append-only and ordered by ``sequence``.  A correction or
    supplemental row can supersede an earlier row; a retraction removes its
    target and reveals any row the target had replaced.  Supersede/retract
    chains are resolved per demand across periods, because a result reported in
    a later month may still correct or retract an earlier month's row.  Active
    rows of the chosen period are merged in sequence order so a supplemental
    benefit report inherits the start/exit facts reported earlier instead of
    erasing them.  The latest period with an active row is the cumulative
    outcome for the demand.
    """

    by_demand: dict[str, list[Mapping[str, Any]]] = {}
    for row in observations:
        by_demand.setdefault(row['demand_id'], []).append(row)

    latest_by_demand: dict[str, dict[str, Any]] = {}
    for demand_id, rows in by_demand.items():
        active = _active_rows(rows)
        if not active:
            continue
        by_period: dict[str, list[Mapping[str, Any]]] = {}
        for row in active:
            by_period.setdefault(normalized_text(row['period']), []).append(row)
        period = max(by_period, key=lambda key: (
            key, max(int(row.get('sequence') or 0) for row in by_period[key])))
        latest_by_demand[demand_id] = _merge_active(by_period[period])
    return [latest_by_demand[key] for key in sorted(latest_by_demand)]


def _merge_active(active: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(active, key=lambda item: (int(item.get('sequence') or 0), item['observation_id']))
    merged: dict[str, Any] = {
        'demand_id': ordered[0]['demand_id'],
        'period': normalized_text(ordered[0]['period']),
        'started': None,
        'exited': None,
        'exit_reason': None,
        'benefit': None,
        'recorded_at': normalized_text(ordered[0]['recorded_at']),
        'sources': [],
        'last_sequence': 0,
    }
    for row in ordered:
        started = _optional_bool(row.get('started'))
        if started is not None:
            merged['started'] = started
        exited = _optional_bool(row.get('exited'))
        if exited is not None:
            merged['exited'] = exited
            merged['exit_reason'] = (normalized_text(row.get('exit_reason')) or None) if exited else None
        benefit = _optional_number(row.get('benefit'))
        if benefit is not None:
            merged['benefit'] = benefit
        merged['sources'].append({'observation_id': row['observation_id'],
                                  'fingerprint': observation_fingerprint(row)})
        merged['last_sequence'] = max(merged['last_sequence'], int(row.get('sequence') or 0))
        merged['recorded_at'] = max(merged['recorded_at'], normalized_text(row['recorded_at']))
    merged['observation_id'] = stable_hash({'period': merged['period'],
                                            'sources': [item['observation_id'] for item in merged['sources']]})
    merged['fingerprint'] = stable_hash({'period': merged['period'],
                                         'started': merged['started'], 'exited': merged['exited'],
                                         'exit_reason': merged['exit_reason'], 'benefit': merged['benefit'],
                                         'sources': merged['sources']})
    return merged


def _active_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Resolve the supersede/retract history for one demand and period.

    Every correction or supplemental row records which row it replaced.  A
    retraction hides its target and restores that target's immediate
    predecessor, so retracting a correction reveals the earlier result instead
    of erasing the demand's whole history.  Retraction rows are never
    effective on their own.
    """

    ordered = sorted(rows, key=lambda item: (int(item.get('sequence') or 0), item['observation_id']))
    by_id = {row['observation_id']: row for row in ordered}
    active: dict[str, Mapping[str, Any]] = {}
    predecessor: dict[str, str] = {}
    retracted: set[str] = set()
    for row in ordered:
        target = row.get('supersedes')
        if row['kind'] == 'retraction':
            retracted.add(target)
            if target in active:
                del active[target]
                prior = predecessor.get(target)
                if prior and prior not in retracted and prior in by_id:
                    active[prior] = by_id[prior]
            continue
        if target:
            active.pop(target, None)
            predecessor[row['observation_id']] = target
        active[row['observation_id']] = row
    return list(active.values())


# ---------------------------------------------------------------------------
# outcome classification


def classify_outcome(
    selection: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    reservation: Mapping[str, Any] | None,
    as_of: str,
) -> dict[str, Any]:
    as_of_time = parse_time(as_of, 'as_of')
    selected_at = parse_time(selection['selected_at'], 'selected_at')
    observation_days = int(selection['observation_days'])
    reservation = dict(reservation or {})
    status = reservation.get('status')
    accepted_at = reservation.get('accepted_at')
    closed_at = reservation.get('closed_at')
    if accepted_at and parse_time(accepted_at, 'accepted_at') > as_of_time:
        accepted_at = None
        status = None
        closed_at = None
    if closed_at and parse_time(closed_at, 'closed_at') > as_of_time:
        closed_at = None
        status = 'active' if accepted_at else None

    if parse_time(as_of) < selected_at:
        # A demand cannot have an outcome before the selection was recorded.
        observation = None

    observation_started = _optional_bool(observation.get('started')) if observation else None
    observation_exited = _optional_bool(observation.get('exited')) if observation else None
    benefit = _optional_number(observation.get('benefit')) if observation else None

    start_candidates = [value for value in
                        (selection.get('observation_start'), accepted_at,
                         observation.get('recorded_at') if observation_started else None)
                        if value]
    # Be conservative about maturity: use the latest plausible start so that a
    # demand is not declared "past its observation window" too early.
    effective_start = max(parse_time(value) for value in start_candidates) if start_candidates else selected_at
    due_at = effective_start + timedelta(days=observation_days)

    started = observation_started if observation_started is not None else bool(accepted_at)
    if observation_exited:
        started = True
    exited = bool(observation_exited)
    if observation_exited is None and status == 'withdrawn':
        exited = True
    if exited:
        started = True

    if observation is not None:
        observed = True
        maturity = 'closed' if closed_at else ('mature' if as_of_time >= due_at else 'early')
        status_code = 'observed'
    elif status in ('completed', 'withdrawn'):
        observed = True
        maturity = 'closed'
        status_code = 'observed'
    else:
        observed = False
        maturity = None
        status_code = 'pending_observation' if as_of_time < due_at else 'no_result'

    return {
        'demand_id': selection['demand_id'],
        'selected': bool(selection['selected']),
        'sector': normalized_text(selection['sector']),
        'score': finite_number(selection['score']),
        'observed': observed,
        'status': status_code,
        'maturity': maturity,
        'started': bool(started),
        'exited': bool(exited),
        'exit_reason': (normalized_text(observation.get('exit_reason')) or None)
        if observation is not None and exited else None,
        'benefit': benefit,
        'observation_id': observation['observation_id'] if observation else None,
        'observation_period': normalized_text(observation['period']) if observation else None,
        'observation_recorded_at': normalize_time(observation['recorded_at']) if observation else None,
        'observation_sources': list(observation.get('sources') or []) if observation else [],
        'reservation_status': status,
        'effective_start': format_time(effective_start),
        'due_at': format_time(due_at),
        'maturity_basis': 'observation' if observation is not None
        else ('reservation' if status in ('completed', 'withdrawn') else 'clock'),
    }


# ---------------------------------------------------------------------------
# comparison scope


def _condition_metrics(selections: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scores = [finite_number(row['score']) for row in selections]
    numeric: dict[str, list[float]] = {}
    for row in selections:
        for key, value in (row.get('intake') or {}).items():
            if isinstance(value, bool) or value is None or not isinstance(value, (int, float)):
                continue
            numeric.setdefault(key, []).append(finite_number(value))
    return {
        'demand_count': len(selections),
        'pre_selection_score_mean': sum(scores) / len(scores) if scores else None,
        'pre_selection_score_min': min(scores) if scores else None,
        'pre_selection_score_max': max(scores) if scores else None,
        'pre_selection_conditions': {key: sum(values) / len(values) for key, values in sorted(numeric.items()) if values},
    }


def _outcome_metrics(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observed = [row for row in outcomes if row['observed']]
    started = [row for row in observed if row['started']]
    exited = [row for row in observed if row['exited']]
    benefits = [finite_number(row['benefit']) for row in observed if row.get('benefit') is not None]
    maturity: dict[str, int] = {}
    for row in observed:
        key = row.get('maturity') or 'unknown'
        maturity[key] = maturity.get(key, 0) + 1
    return {
        'demand_count': len(outcomes),
        'observed_count': len(observed),
        'pending_observation_count': sum(row['status'] == 'pending_observation' for row in outcomes),
        'no_result_count': sum(row['status'] == 'no_result' for row in outcomes),
        'started_count': len(started),
        'start_rate': len(started) / len(observed) if observed else None,
        'exited_count': len(exited),
        'exit_rate_among_started': len(exited) / len(started) if started else None,
        'exit_rate_among_observed': len(exited) / len(observed) if observed else None,
        'benefit_n': len(benefits),
        'benefit_mean': sum(benefits) / len(benefits) if benefits else None,
        'maturity': dict(sorted(maturity.items())),
        'observation_ids': sorted(str(row['observation_id']) for row in observed if row.get('observation_id')),
    }


def _difference(left: Any, right: Any) -> float | None:
    if left is None or right is None:
        return None
    return float(left) - float(right)


def compare_outcomes(selected: Sequence[Mapping[str, Any]],
                     unselected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected_metrics = _outcome_metrics(selected)
    unselected_metrics = _outcome_metrics(unselected)
    return {
        'selected': selected_metrics,
        'unselected': unselected_metrics,
        'difference': {
            'start_rate': _difference(selected_metrics['start_rate'], unselected_metrics['start_rate']),
            'exit_rate': _difference(selected_metrics['exit_rate_among_started'],
                                     unselected_metrics['exit_rate_among_started']),
            'benefit_mean': _difference(selected_metrics['benefit_mean'], unselected_metrics['benefit_mean']),
        },
        'selected_ids': sorted(str(row['demand_id']) for row in selected),
        'unselected_ids': sorted(str(row['demand_id']) for row in unselected),
    }


INCOMPARABLE_EXPLANATIONS = {
    'no_selected_observation': '入选需求尚无可用观察结果（未到观察期或未补报），不能据此判断撮合规则。',
    'no_unselected_demand': '该行业没有登记未入选需求，缺少对照范围。',
    'no_unselected_observation': '该行业有未入选需求，但均无可用观察结果，无法比较启动、退出和收益。',
    'no_unselected_within_score_band': '该行业的未入选需求在入选前评分区间内没有可匹配对象，需求条件差异过大。',
    'no_selected_observation_within_band': '该行业的入选需求均未进入观察可比较区间，无法与未入选需求配对。',
}


def _sector_rows(sector: str,
                 selected_rows: Sequence[Mapping[str, Any]],
                 unselected_rows: Sequence[Mapping[str, Any]],
                 outcomes: Mapping[str, Mapping[str, Any]],
                 rules: Mapping[str, Any]) -> dict[str, Any]:
    selected = [outcomes[row['demand_id']] for row in selected_rows]
    unselected = [outcomes[row['demand_id']] for row in unselected_rows]
    selected_observed = [row for row in selected if row['observed']]
    unselected_observed = [row for row in unselected if row['observed']]

    reasons: list[str] = []
    pairs: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    matched_selected: set[str] = set()
    matched_unselected: set[str] = set()
    band = float(rules['score_band'])

    for row in selected:
        if row['status'] == 'pending_observation':
            unmatched.append({'demand_id': row['demand_id'], 'reasons': ['pending_observation'],
                              'explanation': '尚未到观察期，不计入失败，也不参与本轮比较。'})
        elif row['status'] == 'no_result':
            unmatched.append({'demand_id': row['demand_id'], 'reasons': ['no_result'],
                              'explanation': '观察期已到但没有可用结果，作为数据缺口说明，不按失败计算。'})

    for row in selected_observed:
        if not unselected_rows:
            unmatched.append({'demand_id': row['demand_id'], 'reasons': ['no_unselected_demand'],
                              'explanation': INCOMPARABLE_EXPLANATIONS['no_unselected_demand']})
            continue
        if not unselected_observed:
            unmatched.append({'demand_id': row['demand_id'], 'reasons': ['no_unselected_observation'],
                              'explanation': INCOMPARABLE_EXPLANATIONS['no_unselected_observation']})
            continue
        candidates = [other for other in unselected_observed
                      if abs(float(other['score']) - float(row['score'])) <= band]
        if not candidates:
            unmatched.append({'demand_id': row['demand_id'], 'reasons': ['no_unselected_within_score_band'],
                              'explanation': INCOMPARABLE_EXPLANATIONS['no_unselected_within_score_band']})
            continue
        matched_selected.add(row['demand_id'])
        matched_unselected.update(other['demand_id'] for other in candidates)
        pairs.append({
            'selected_id': row['demand_id'],
            'unselected_ids': sorted(other['demand_id'] for other in candidates),
            'score_gap_max': max(abs(float(other['score']) - float(row['score'])) for other in candidates),
        })

    if not selected_observed:
        reasons.append('no_selected_observation')
    if not unselected_rows:
        reasons.append('no_unselected_demand')
    elif not unselected_observed:
        reasons.append('no_unselected_observation')
    elif not pairs:
        reasons.append('no_selected_observation_within_band')

    comparable = bool(pairs)
    all_selected = _condition_metrics(selected_rows)
    all_unselected = _condition_metrics(unselected_rows)
    result = {
        'sector': sector,
        'comparable': comparable,
        'reasons': [] if comparable else reasons,
        'explanations': [INCOMPARABLE_EXPLANATIONS[reason] for reason in reasons
                         if reason in INCOMPARABLE_EXPLANATIONS],
        'selected_total': len(selected_rows),
        'unselected_total': len(unselected_rows),
        'selected_observed': len(selected_observed),
        'unselected_observed': len(unselected_observed),
        'selected_pending_observation': sorted(row['demand_id'] for row in selected
                                               if row['status'] == 'pending_observation'),
        'selected_no_result': sorted(row['demand_id'] for row in selected if row['status'] == 'no_result'),
        'matched_selected_ids': sorted(matched_selected),
        'matched_unselected_ids': sorted(matched_unselected),
        'matched_pairs': pairs,
        'unmatched_selected': unmatched,
        'selected_conditions': all_selected,
        'unselected_conditions': all_unselected,
    }
    if comparable:
        result['comparison'] = compare_outcomes(
            [row for row in selected_observed if row['demand_id'] in matched_selected],
            [row for row in unselected_observed if row['demand_id'] in matched_unselected],
        )
    return result


def build_comparison_scope(selections: Sequence[Mapping[str, Any]],
                           outcomes: Mapping[str, Mapping[str, Any]],
                           rules: Mapping[str, Any]) -> dict[str, Any]:
    sectors: dict[str, list[Mapping[str, Any]]] = {}
    for row in selections:
        sectors.setdefault(normalized_text(row['sector']), []).append(row)

    all_sectors: list[dict[str, Any]] = []
    comparable_sectors: list[dict[str, Any]] = []
    incomparable_sectors: list[dict[str, Any]] = []
    for sector in sorted(sectors):
        rows = sectors[sector]
        selected_rows = [row for row in rows if row['selected']]
        unselected_rows = [row for row in rows if not row['selected']]
        if not selected_rows:
            continue
        analysis = _sector_rows(sector, selected_rows, unselected_rows, outcomes, rules)
        all_sectors.append(analysis)
        (comparable_sectors if analysis['comparable'] else incomparable_sectors).append(analysis)

    selected_ids: set[str] = set()
    unselected_ids: set[str] = set()
    for analysis in comparable_sectors:
        selected_ids.update(analysis['matched_selected_ids'])
        unselected_ids.update(analysis['matched_unselected_ids'])

    unmatched = [item | {'sector': analysis['sector']}
                 for analysis in all_sectors for item in analysis['unmatched_selected']]
    selected_outcomes = [outcomes[demand_id] for demand_id in sorted(selected_ids)]
    unselected_outcomes = [outcomes[demand_id] for demand_id in sorted(unselected_ids)]
    comparison = compare_outcomes(selected_outcomes, unselected_outcomes) if (selected_ids or unselected_ids) else None
    if comparison is not None:
        selected_conditions = _condition_metrics([row for row in selections if row['demand_id'] in selected_ids])
        unselected_conditions = _condition_metrics([row for row in selections if row['demand_id'] in unselected_ids])
        comparison['selected_conditions'] = selected_conditions
        comparison['unselected_conditions'] = unselected_conditions
        comparison['difference']['pre_selection_score'] = _difference(
            selected_conditions['pre_selection_score_mean'],
            unselected_conditions['pre_selection_score_mean'])
    return {
        'score_band': float(rules['score_band']),
        'require_same_sector': True,
        'sectors': all_sectors,
        'comparable_sectors': comparable_sectors,
        'incomparable_sectors': incomparable_sectors,
        'matched_selected_ids': sorted(selected_ids),
        'matched_unselected_ids': sorted(unselected_ids),
        'unmatched_selected': unmatched,
        'comparison': comparison,
    }


# ---------------------------------------------------------------------------
# recommendation


def recommend(scope: Mapping[str, Any], rules: Mapping[str, Any],
              counts: Mapping[str, Any]) -> dict[str, Any]:
    comparison = scope.get('comparison') or {}
    selected = comparison.get('selected') or {}
    unselected = comparison.get('unselected') or {}
    diff = comparison.get('difference') or {}
    reasons: list[str] = []
    explanations: list[str] = []

    selected_observed = int(selected.get('observed_count') or 0)
    unselected_observed = int(unselected.get('observed_count') or 0)
    benefit_diff = diff.get('benefit_mean')
    exit_diff = diff.get('exit_rate')
    start_diff = diff.get('start_rate')
    pre_score_diff = diff.get('pre_selection_score')

    must_hold = False
    if selected_observed < int(rules['min_selected_observed']):
        reasons.append('insufficient_selected_observations')
        explanations.append('可比范围内已观察的入选需求不足。')
        must_hold = True
    if unselected_observed < int(rules['min_unselected_observed']):
        reasons.append('insufficient_unselected_observations')
        explanations.append('可比范围内已观察的未入选需求不足。')
        must_hold = True

    if must_hold:
        recommendation = 'insufficient_evidence'
    elif benefit_diff is None:
        reasons.append('benefit_not_comparable')
        explanations.append('可比范围内缺少可比较的收益报告，不能把落地率当作规则有效。')
        if exit_diff is not None and exit_diff < -float(rules['exit_margin']):
            recommendation = 'conditional_renew'
            reasons.append('process_evidence_only')
            explanations.append('仅有退出率等过程证据，收益无法比较，只能形成有条件待审意见。')
        else:
            recommendation = 'insufficient_evidence'
            reasons.append('no_comparable_outcome_evidence')
            explanations.append('启动、退出和收益的可比证据不足，不能形成续期建议。')
    else:
        benefit_margin = float(rules['benefit_margin'])
        exit_margin = float(rules['exit_margin'])
        start_margin = float(rules['start_margin'])
        benefit_better = benefit_diff > benefit_margin
        benefit_not_worse = benefit_diff >= -benefit_margin
        exit_worse = exit_diff is not None and exit_diff > exit_margin
        exit_better = exit_diff is not None and exit_diff < -exit_margin
        start_worse = start_diff is not None and start_diff < -start_margin
        if benefit_better and not exit_worse and not start_worse:
            recommendation = 'renew'
            reasons.extend(['benefit_advantage', 'no_exit_burden'])
            explanations.append('入选需求的平均收益优于可比未入选需求，退出率没有更差。')
        elif benefit_better:
            recommendation = 'conditional_renew'
            reasons.append('benefit_advantage_with_process_caveat')
            explanations.append('入选需求收益有优势，但启动或退出表现存在需要附带条件的差异。')
        elif benefit_not_worse and exit_better:
            recommendation = 'conditional_renew'
            reasons.extend(['exit_advantage_only', 'no_benefit_advantage'])
            explanations.append('收益差异不显著，但入选需求退出更少，只能作为有条件续期依据。')
        else:
            recommendation = 'do_not_renew'
            reasons.append('no_benefit_advantage')
            explanations.append('与可比未入选需求相比，入选需求没有显示出收益优势。')

    conditions_differ = (
        pre_score_diff is not None
        and abs(float(pre_score_diff)) > float(rules['pre_selection_gap_warning']))
    if conditions_differ:
        reasons.append('pre_selection_conditions_differ')
        explanations.append('入选与未入选需求的入选前评分差异较大，不能仅凭入选项目表现判断规则有效。')
        if recommendation == 'renew':
            recommendation = 'conditional_renew'

    if counts.get('pending_observation_ids'):
        reasons.append('pending_observations_excluded')
        explanations.append('部分入选需求尚未到观察期，未计入失败或本轮比较。')
    if counts.get('no_result_ids'):
        reasons.append('no_result_disclosed')
        explanations.append('部分需求观察期已到但没有结果，作为数据缺口披露，不按失败计算。')
    if counts.get('incomparable_sector_count'):
        reasons.append('incomparable_sectors_excluded')
        explanations.append('存在无法比较的行业，已单独说明，未纳入总体比较。')
    if int(((comparison.get('selected') or {}).get('maturity') or {}).get('early') or 0):
        reasons.append('early_observations_included')
        explanations.append('部分结果早于完整观察期，已标注成熟度，续期意见需复审。')
        if recommendation == 'renew':
            # An unconditional renewal needs results from a completed
            # observation window; early results can only support a
            # conditional opinion.
            recommendation = 'conditional_renew'
            reasons.append('renewal_conditional_on_mature_results')

    return {
        'recommendation': recommendation,
        'reasons': list(dict.fromkeys(reasons)),
        'explanations': list(dict.fromkeys(explanations)),
    }


# ---------------------------------------------------------------------------
# review assembly


def assemble_review(selections: Sequence[Mapping[str, Any]],
                    batches: Sequence[Mapping[str, Any]],
                    observations: Sequence[Mapping[str, Any]],
                    reservations: Mapping[str, Mapping[str, Any]],
                    *,
                    as_of: Any,
                    rules: Mapping[str, Any] | None = None,
                    evidence_cutoff: Any = None) -> dict[str, Any]:
    merged_rules = merge_rules(rules)
    as_of_text = normalize_time(as_of, 'as_of')
    cutoff = normalize_time(evidence_cutoff, 'evidence_cutoff') if evidence_cutoff else None

    included_selections = [
        dict(row) for row in selections
        if cutoff is None or normalize_time(row['recorded_at'], 'recorded_at') <= cutoff
    ]
    included_batches = [
        dict(row) for row in batches
        if cutoff is None or normalize_time(row['recorded_at'], 'recorded_at') <= cutoff
    ]
    included_observations = [
        dict(row) for row in observations
        if any(batch['batch_id'] == row['batch_id'] for batch in included_batches)
    ]
    if not included_selections:
        raise ContractError('pilot round has no recorded selections')

    observations_by_batch: dict[str, list[Mapping[str, Any]]] = {}
    for row in included_observations:
        observations_by_batch.setdefault(row['batch_id'], []).append(row)
    for batch in included_batches:
        expected = batch_fingerprint(batch, observations_by_batch.get(batch['batch_id'], []))
        if expected != batch['fingerprint']:
            raise ContractError('observation batch fingerprint mismatch')

    effective = effective_observations(included_observations)
    effective_by_demand = {row['demand_id']: row for row in effective}
    outcomes = {}
    for selection in included_selections:
        outcomes[selection['demand_id']] = classify_outcome(
            selection, effective_by_demand.get(selection['demand_id']),
            reservations.get(selection['demand_id']), as_of_text)

    scope = build_comparison_scope(included_selections, outcomes, merged_rules)
    selected_outcomes = [row for row in outcomes.values() if row['selected']]
    unselected_outcomes = [row for row in outcomes.values() if not row['selected']]
    pending_ids = sorted(row['demand_id'] for row in outcomes.values()
                         if row['status'] == 'pending_observation')
    no_result_ids = sorted(row['demand_id'] for row in outcomes.values()
                           if row['status'] == 'no_result')
    counts = {
        'selected_total': len(selected_outcomes),
        'unselected_total': len(unselected_outcomes),
        'selected_observed': sum(row['observed'] for row in selected_outcomes),
        'unselected_observed': sum(row['observed'] for row in unselected_outcomes),
        'selected_pending_observation': sum(row['status'] == 'pending_observation' for row in selected_outcomes),
        'unselected_pending_observation': sum(row['status'] == 'pending_observation' for row in unselected_outcomes),
        'selected_no_result': sum(row['status'] == 'no_result' for row in selected_outcomes),
        'unselected_no_result': sum(row['status'] == 'no_result' for row in unselected_outcomes),
        'pending_observation_ids': pending_ids,
        'no_result_ids': no_result_ids,
        'comparable_sector_count': len(scope['comparable_sectors']),
        'incomparable_sector_count': len(scope['incomparable_sectors']),
        'matched_selected_count': len(scope['matched_selected_ids']),
        'matched_unselected_count': len(scope['matched_unselected_ids']),
    }
    recommendation = recommend(scope, merged_rules, counts)

    selection_batches = []
    by_batch: dict[str, list[Mapping[str, Any]]] = {}
    for row in included_selections:
        by_batch.setdefault(row['batch_id'], []).append(row)
    for batch_id in sorted(by_batch):
        rows = by_batch[batch_id]
        selection_batches.append({
            'batch_id': batch_id,
            'fingerprint': selection_batch_fingerprint(batch_id, rows),
            'demand_ids': sorted(row['demand_id'] for row in rows),
        })

    observation_batch_rows = []
    for batch in sorted(included_batches, key=lambda item: (normalize_time(item['recorded_at']), item['batch_id'])):
        rows = observations_by_batch.get(batch['batch_id'], [])
        observation_batch_rows.append({
            'batch_id': batch['batch_id'],
            'period': normalized_text(batch['period']),
            'kind': batch['kind'],
            'note': normalized_text(batch.get('note')),
            'recorded_at': normalize_time(batch['recorded_at']),
            'fingerprint': batch['fingerprint'],
            'observation_ids': sorted(row['observation_id'] for row in rows),
        })

    reservation_snapshot = {}
    for selection in sorted(included_selections, key=lambda item: item['demand_id']):
        reservation = reservations.get(selection['demand_id']) or {}
        reservation_snapshot[selection['demand_id']] = {
            'status': reservation.get('status'),
            'accepted_at': _iso_or_none(reservation.get('accepted_at')),
            'closed_at': _iso_or_none(reservation.get('closed_at')),
        }

    basis = {
        'algorithm': ALGORITHM_VERSION,
        'round_id': included_selections[0]['round_id'],
        'as_of': as_of_text,
        'evidence_cutoff': cutoff,
        'rules': merged_rules,
        'selection_batches': selection_batches,
        'selection_fingerprints': {
            row['demand_id']: selection_fingerprint(row)
            for row in sorted(included_selections, key=lambda item: item['demand_id'])
        },
        'observation_batches': observation_batch_rows,
        'effective_observations': [
            {'observation_id': row['observation_id'], 'demand_id': row['demand_id'],
             'period': row['period'], 'fingerprint': row['fingerprint'],
             'sources': row['sources']}
            for row in effective
        ],
        'reservation_snapshot': reservation_snapshot,
    }
    comparison_fingerprint = stable_hash({'rules': merged_rules, 'scope': scope,
                                          'recommendation': recommendation})
    return {
        'round_id': basis['round_id'],
        'algorithm': ALGORITHM_VERSION,
        'as_of': as_of_text,
        'evidence_cutoff': cutoff,
        'rules': merged_rules,
        'counts': counts,
        'outcomes': [outcomes[key] for key in sorted(outcomes)],
        'sectors': scope['sectors'],
        'comparable_sectors': scope['comparable_sectors'],
        'incomparable_sectors': scope['incomparable_sectors'],
        'unmatched_selected': scope['unmatched_selected'],
        'comparison': scope['comparison'],
        'recommendation': recommendation,
        'basis': basis,
        'basis_fingerprint': stable_hash(basis),
        'comparison_fingerprint': comparison_fingerprint,
    }


# ---------------------------------------------------------------------------
# persistence: selection snapshots and observation batches


def _row_dicts(connection: Any, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]


def load_selections(connection: Any, round_id: str) -> list[dict[str, Any]]:
    rows = _row_dicts(connection, 'SELECT * FROM pilot_selections WHERE round_id=? ORDER BY demand_id', (round_id,))
    for row in rows:
        row['intake'] = json.loads(row['intake_json'])
    return rows


def load_batches(connection: Any, round_id: str) -> list[dict[str, Any]]:
    return _row_dicts(connection,
                      'SELECT * FROM pilot_observation_batches WHERE round_id=? ORDER BY recorded_at, batch_id',
                      (round_id,))


def load_observations(connection: Any, round_id: str) -> list[dict[str, Any]]:
    return _row_dicts(connection,
                      'SELECT * FROM pilot_observations WHERE round_id=? ORDER BY sequence', (round_id,))


def load_reservations(connection: Any, demand_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    identifiers = sorted(set(demand_ids))
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(identifiers), 500):
        chunk = identifiers[start:start + 500]
        placeholders = ','.join('?' for _ in chunk)
        rows = _row_dicts(connection,
                          f'SELECT demand_id,status,accepted_at,closed_at FROM pilot_reservations '
                          f'WHERE demand_id IN ({placeholders})', chunk)
        result.update({row['demand_id']: row for row in rows})
    return result


def record_selection_batch(connection: Any, round_id: str, batch_id: str,
                           selections: Sequence[Mapping[str, Any]],
                           *, recorded_at: Any = None) -> dict[str, Any]:
    prepare_retrospective(connection)
    round_key = normalized_text(round_id)
    batch_key = normalized_text(batch_id)
    if not round_key or not batch_key:
        raise ContractError('round and selection batch identifiers are required')
    if not selections:
        raise ContractError('selection batch must not be empty')
    recorded = normalize_time(recorded_at or utc_now(), 'recorded_at')

    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selections:
        demand_id = normalized_text(item.get('demand_id'))
        if not demand_id:
            raise ContractError('selection demand identifier is required')
        if demand_id in seen:
            raise ContractError(f'duplicate demand in selection batch: {demand_id}')
        seen.add(demand_id)
        intake = item.get('intake')
        if not isinstance(intake, Mapping) or not intake:
            raise ContractError('selection requires the pre-selection demand snapshot')
        sector = normalized_text(item.get('sector') or intake.get('sector'))
        if not sector:
            raise ContractError('selection requires a sector')
        selected = item.get('selected')
        if not isinstance(selected, bool):
            raise ContractError('selection flag must be boolean')
        observation_days = item.get('observation_days', DEFAULT_OBSERVATION_DAYS)
        if type(observation_days) is not int or observation_days <= 0:
            raise ContractError('observation days must be a positive integer')
        score = item.get('score')
        score = score_intake(demand_id, intake) if score is None else finite_number(score)
        if score < 0 or score > 1:
            raise ContractError('pre-selection score must be between 0 and 1')
        selected_at = normalize_time(item.get('selected_at') or recorded, 'selected_at')
        observation_start = _iso_or_none(item.get('observation_start'))
        if observation_start and parse_time(observation_start) < parse_time(selected_at):
            raise ContractError('observation cannot start before selection')
        prepared.append({
            'demand_id': demand_id,
            'round_id': round_key,
            'batch_id': batch_key,
            'sector': sector,
            'selected': int(selected),
            'score': score,
            'intake_json': canonical_json(dict(intake)),
            'intake_fingerprint': stable_hash(dict(intake)),
            'observation_days': observation_days,
            'selected_at': selected_at,
            'observation_start': observation_start,
            'recorded_at': recorded,
        })

    stored = []
    for row in prepared:
        existing = connection.execute(
            'SELECT * FROM pilot_selections WHERE round_id=? AND demand_id=?',
            (row['round_id'], row['demand_id'])).fetchone()
        if existing is not None:
            current = dict(existing)
            if selection_semantic(current) != selection_semantic(row):
                raise ContractError(
                    'pre-selection snapshot is immutable; open a new round instead of editing it')
            stored.append(current)
            continue
        connection.execute(
            'INSERT INTO pilot_selections(demand_id,round_id,batch_id,sector,selected,score,intake_json,'
            'intake_fingerprint,observation_days,selected_at,observation_start,recorded_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (row['demand_id'], row['round_id'], row['batch_id'], row['sector'], row['selected'],
             row['score'], row['intake_json'], row['intake_fingerprint'], row['observation_days'],
             row['selected_at'], row['observation_start'], row['recorded_at']))
        stored.append(row)

    return {
        'round_id': round_key,
        'batch_id': batch_key,
        'recorded_at': recorded,
        'selections': [row['demand_id'] for row in stored],
        'selected_count': sum(bool(row['selected']) for row in stored),
        'unselected_count': sum(not bool(row['selected']) for row in stored),
        'fingerprint': selection_batch_fingerprint(batch_key, stored),
    }


def record_observation_batch(connection: Any, round_id: str, batch_id: str, period: str,
                             kind: str, observations: Sequence[Mapping[str, Any]],
                             *, note: str = '', recorded_at: Any = None,
                             allow_unknown_demand: bool = False) -> dict[str, Any]:
    prepare_retrospective(connection)
    round_key = normalized_text(round_id)
    batch_key = normalized_text(batch_id)
    period_key = normalized_text(period)
    kind_key = normalized_text(kind)
    if not round_key or not batch_key or not period_key:
        raise ContractError('round, batch and period identifiers are required')
    if kind_key not in OBSERVATION_KINDS:
        raise ContractError(f'unsupported observation kind: {kind}')
    if not observations:
        raise ContractError('observation batch must not be empty')
    recorded = normalize_time(recorded_at or utc_now(), 'recorded_at')

    known = {row['demand_id']: row for row in load_selections(connection, round_key)}
    existing_batch = connection.execute('SELECT * FROM pilot_observation_batches WHERE batch_id=?',
                                        (batch_key,)).fetchone()
    if existing_batch is not None:
        current = dict(existing_batch)
        if (current['round_id'] != round_key or normalized_text(current['period']) != period_key
                or current['kind'] != kind_key or normalized_text(current['note']) != normalized_text(note)):
            raise ContractError('observation batch replay conflicts with the recorded batch')

    last_sequence = connection.execute(
        'SELECT COALESCE(MAX(sequence),0) FROM pilot_observations').fetchone()[0]
    new_rows: list[dict[str, Any]] = []
    for item in observations:
        demand_id = normalized_text(item.get('demand_id'))
        if not demand_id:
            raise ContractError('observation demand identifier is required')
        if demand_id not in known and not allow_unknown_demand:
            raise ContractError(f'observation refers to a demand outside the round: {demand_id}')
        supersedes = item.get('supersedes') or None
        if kind_key == 'retraction' and not supersedes:
            raise ContractError('retraction requires the identifier being retracted')
        if supersedes:
            target = connection.execute('SELECT * FROM pilot_observations WHERE observation_id=?',
                                        (supersedes,)).fetchone()
            if target is None:
                raise ContractError('retraction or correction target is unknown')
            target = dict(target)
            if target['round_id'] != round_key or target['demand_id'] != demand_id:
                raise ContractError('retraction or correction target belongs to another round or demand')
            if parse_time(target['recorded_at']) > parse_time(recorded):
                raise ContractError('cannot supersede an observation recorded later')
        candidate = {
            'demand_id': demand_id,
            'period': period_key,
            'kind': kind_key,
            'supersedes': supersedes,
            'started': _optional_bool(item.get('started')),
            'exited': _optional_bool(item.get('exited')),
            'exit_reason': normalized_text(item.get('exit_reason')) or None,
            'benefit': _optional_number(item.get('benefit')),
            'note': normalized_text(item.get('note')),
        }
        observation_id = observation_id_for(candidate | {'round_id': round_key, 'batch_id': batch_key})
        existing = connection.execute('SELECT * FROM pilot_observations WHERE observation_id=?',
                                      (observation_id,)).fetchone()
        if existing is not None:
            if observation_semantic(dict(existing)) != candidate:
                raise ContractError('conflicting observation replay')
            continue
        row = candidate | {
            'observation_id': observation_id,
            'sequence': int(last_sequence) + len(new_rows) + 1,
            'batch_id': batch_key,
            'round_id': round_key,
            'recorded_at': recorded,
        }
        row['fingerprint'] = observation_fingerprint(row)
        new_rows.append(row)

    if existing_batch is not None and new_rows:
        raise ContractError('observation batch is immutable; report later results in a new batch')

    if existing_batch is not None:
        stored_observations = [dict(row) for row in connection.execute(
            'SELECT * FROM pilot_observations WHERE batch_id=? ORDER BY sequence', (batch_key,))]
        return {'round_id': round_key, 'batch_id': batch_key, 'period': period_key, 'kind': kind_key,
                'recorded_at': normalize_time(existing_batch['recorded_at']),
                'observations': [row['observation_id'] for row in stored_observations],
                'fingerprint': existing_batch['fingerprint'], 'replayed': True}

    batch_row = {
        'batch_id': batch_key,
        'round_id': round_key,
        'period': period_key,
        'kind': kind_key,
        'note': normalized_text(note),
        'recorded_at': recorded,
    }
    all_batch_rows = new_rows
    fingerprint = batch_fingerprint(batch_row, all_batch_rows)
    connection.execute(
        'INSERT INTO pilot_observation_batches(batch_id,round_id,period,kind,note,recorded_at,'
        'observation_fingerprints_json,fingerprint) VALUES(?,?,?,?,?,?,?,?)',
        (batch_key, round_key, period_key, kind_key, normalized_text(note), recorded,
         canonical_json(sorted((row['observation_id'], row['fingerprint']) for row in all_batch_rows)),
         fingerprint))
    for row in new_rows:
        connection.execute(
            'INSERT INTO pilot_observations(observation_id,sequence,batch_id,round_id,demand_id,period,'
            'kind,supersedes,started,exited,exit_reason,benefit,note,recorded_at,fingerprint) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (row['observation_id'], row['sequence'], row['batch_id'], row['round_id'], row['demand_id'],
             row['period'], row['kind'], row['supersedes'], _bool_int(row['started']),
             _bool_int(row['exited']), row['exit_reason'], row['benefit'], row['note'],
             row['recorded_at'], row['fingerprint']))
    return {'round_id': round_key, 'batch_id': batch_key, 'period': period_key, 'kind': kind_key,
            'recorded_at': recorded,
            'observations': sorted(row['observation_id'] for row in all_batch_rows),
            'fingerprint': fingerprint, 'replayed': existing_batch is not None}


def rounds_for_demand(connection: Any, demand_id: str) -> list[str]:
    rows = connection.execute('SELECT DISTINCT round_id FROM pilot_selections WHERE demand_id=?',
                              (normalized_text(demand_id),)).fetchall()
    return sorted(row[0] for row in rows)


# ---------------------------------------------------------------------------
# review, opinions and decisions


def build_effect_review(connection: Any, round_id: str, *, as_of: Any = None,
                        rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    prepare_retrospective(connection)
    round_key = normalized_text(round_id)
    selections = load_selections(connection, round_key)
    if not selections:
        raise ContractError('pilot round has no recorded selections')
    batches = load_batches(connection, round_key)
    observations = load_observations(connection, round_key)
    reservations = load_reservations(connection, [row['demand_id'] for row in selections])

    evidence_cutoff = None
    recorded_times = [normalize_time(row['recorded_at']) for row in selections]
    recorded_times += [normalize_time(row['recorded_at']) for row in observations]
    for reservation in reservations.values():
        for value in (reservation.get('accepted_at'), reservation.get('closed_at')):
            if value:
                recorded_times.append(normalize_time(value))
    if recorded_times:
        evidence_cutoff = max(recorded_times)
    # Default to the latest recorded event rather than the wall clock: the same
    # evidence must always produce the same review (and the same opinion).
    evaluated_as_of = normalize_time(as_of) if as_of is not None else (evidence_cutoff or normalize_time(utc_now()))

    included_selections = [row for row in selections
                           if normalize_time(row['recorded_at']) <= evaluated_as_of]
    included_observations = [row for row in observations
                             if normalize_time(row['recorded_at']) <= evaluated_as_of]
    included_batch_ids = {row['batch_id'] for row in included_observations}
    included_batches = [row for row in batches if row['batch_id'] in included_batch_ids]
    included_cutoff = None
    included_times = [normalize_time(row['recorded_at']) for row in included_selections]
    included_times += [normalize_time(row['recorded_at']) for row in included_observations]
    if included_times:
        included_cutoff = max(included_times)
    included_reservations = {
        demand_id: value for demand_id, value in reservations.items()
        if any(row['demand_id'] == demand_id for row in included_selections)
    }
    return assemble_review(included_selections, included_batches, included_observations,
                           included_reservations, as_of=evaluated_as_of,
                           rules=rules, evidence_cutoff=included_cutoff)


def _opinion_row(connection: Any, opinion_id: str) -> dict[str, Any] | None:
    row = connection.execute('SELECT * FROM renewal_opinions WHERE opinion_id=?', (opinion_id,)).fetchone()
    return dict(row) if row else None


def opinion_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'opinion_id': row['opinion_id'],
        'round_id': row['round_id'],
        'status': row['status'],
        'recommendation': row['recommendation'],
        'basis_fingerprint': row['basis_fingerprint'],
        'comparison_fingerprint': row['comparison_fingerprint'],
        'summary': json.loads(row['summary_json']),
        'created_at': row['created_at'],
        'superseded_at': row['superseded_at'],
        'decided_at': row['decided_at'],
        'decision_id': row['decision_id'],
    }


def _summary_from_review(review: Mapping[str, Any]) -> dict[str, Any]:
    comparison = review.get('comparison') or {}
    diff = comparison.get('difference') or {}
    return {
        'counts': review['counts'],
        'recommendation': review['recommendation'],
        'difference': diff,
        'comparable_sectors': [row['sector'] for row in review['comparable_sectors']],
        'incomparable_sectors': [
            {'sector': row['sector'], 'reasons': row['reasons'],
             'explanations': row['explanations'],
             'selected_pending_observation': row['selected_pending_observation'],
             'selected_no_result': row['selected_no_result']}
            for row in review['incomparable_sectors']
        ],
        'observation_batches': [row['batch_id'] for row in review['basis']['observation_batches']],
    }


def refresh_renewal_opinion(connection: Any, round_id: str, *, as_of: Any = None,
                            rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    prepare_retrospective(connection)
    round_key = normalized_text(round_id)
    review = build_effect_review(connection, round_key, as_of=as_of, rules=rules)
    now = normalize_time(utc_now())

    pending = [dict(row) for row in connection.execute(
        'SELECT * FROM renewal_opinions WHERE round_id=? AND status=? ORDER BY created_at, opinion_id',
        (round_key, 'pending'))]
    for row in pending:
        if row['basis_fingerprint'] == review['basis_fingerprint']:
            return {'opinion': opinion_payload(row), 'review': review,
                    'superseded': [], 'created': False}

    superseded: list[str] = []
    for row in pending:
        connection.execute(
            'UPDATE renewal_opinions SET status=?,superseded_at=? WHERE opinion_id=? AND status=?',
            ('superseded', now, row['opinion_id'], 'pending'))
        superseded.append(row['opinion_id'])

    summary = _summary_from_review(review)
    opinion_id = stable_hash({
        'round_id': round_key,
        'basis_fingerprint': review['basis_fingerprint'],
        'comparison_fingerprint': review['comparison_fingerprint'],
        'created_at': now,
    })
    connection.execute(
        'INSERT INTO renewal_opinions(opinion_id,round_id,status,recommendation,basis_json,'
        'basis_fingerprint,comparison_fingerprint,summary_json,created_at,superseded_at,decided_at,decision_id) '
        'VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)',
        (opinion_id, round_key, 'pending', review['recommendation']['recommendation'],
         canonical_json(review['basis']), review['basis_fingerprint'],
         review['comparison_fingerprint'], canonical_json(summary), now))
    row = _opinion_row(connection, opinion_id)
    return {'opinion': opinion_payload(row), 'review': review,
            'superseded': superseded, 'created': True}


def pending_opinions(connection: Any, round_id: str) -> list[dict[str, Any]]:
    prepare_retrospective(connection)
    rows = connection.execute(
        'SELECT * FROM renewal_opinions WHERE round_id=? AND status=? ORDER BY created_at, opinion_id',
        (normalized_text(round_id), 'pending')).fetchall()
    return [opinion_payload(dict(row)) for row in rows]


def review_rounds(connection: Any) -> list[dict[str, Any]]:
    prepare_retrospective(connection)
    rows = connection.execute(
        'SELECT round_id, COUNT(*) AS selections, SUM(selected) AS selected_count, '
        'MAX(recorded_at) AS last_recorded_at FROM pilot_selections GROUP BY round_id ORDER BY round_id').fetchall()
    result = []
    for row in rows:
        round_id = row['round_id']
        pending = connection.execute(
            'SELECT COUNT(*) FROM renewal_opinions WHERE round_id=? AND status=?',
            (round_id, 'pending')).fetchone()[0]
        decided = connection.execute(
            'SELECT COUNT(*) FROM renewal_decisions WHERE round_id=?', (round_id,)).fetchone()[0]
        batches = connection.execute(
            'SELECT COUNT(*) FROM pilot_observation_batches WHERE round_id=?', (round_id,)).fetchone()[0]
        result.append({'round_id': round_id, 'selections': row['selections'],
                       'selected_count': row['selected_count'],
                       'observation_batches': batches,
                       'last_selection_recorded_at': row['last_recorded_at'],
                       'pending_opinions': pending, 'renewal_decisions': decided})
    return result


def decide_renewal(connection: Any, round_id: str, opinion_id: str, outcome: str,
                   approver: str, note: str = '') -> dict[str, Any]:
    prepare_retrospective(connection)
    round_key = normalized_text(round_id)
    opinion_key = normalized_text(opinion_id)
    outcome_key = normalized_text(outcome)
    approver_key = normalized_text(approver)
    if outcome_key not in RENEWAL_OUTCOMES:
        raise ContractError(f'unsupported renewal outcome: {outcome}')
    if not approver_key:
        raise ContractError('renewal decision requires an approver')
    row = _opinion_row(connection, opinion_key)
    if row is None or row['round_id'] != round_key:
        raise ContractError('renewal opinion is unknown for this round')
    if row['status'] == 'superseded':
        raise ContractError('renewal opinion has been superseded; review the pending opinion')
    if row['status'] == 'decided':
        existing = connection.execute('SELECT * FROM renewal_decisions WHERE decision_id=?',
                                      (row['decision_id'],)).fetchone()
        if existing is not None:
            current = dict(existing)
            if (current['outcome'] == outcome_key and current['approver'] == approver_key
                    and normalized_text(current['note']) == normalized_text(note)):
                return decision_payload(current)
        raise ContractError('renewal opinion is already decided')

    basis = json.loads(row['basis_json'])
    decided_at = normalize_time(utc_now())
    decision_id = stable_hash({
        'round_id': round_key,
        'opinion_id': opinion_key,
        'outcome': outcome_key,
        'approver': approver_key,
        'note': normalized_text(note),
        'decided_at': decided_at,
    })
    batch_ids = [batch['batch_id'] for batch in basis['observation_batches']]
    connection.execute(
        'INSERT INTO renewal_decisions(decision_id,round_id,opinion_id,outcome,recommendation,approver,'
        'note,basis_json,basis_fingerprint,comparison_fingerprint,observation_batch_ids_json,decided_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (decision_id, round_key, opinion_key, outcome_key, row['recommendation'], approver_key,
         normalized_text(note), row['basis_json'], row['basis_fingerprint'],
         row['comparison_fingerprint'], canonical_json(batch_ids), decided_at))
    connection.execute(
        'UPDATE renewal_opinions SET status=?,decided_at=?,decision_id=? WHERE opinion_id=? AND status=?',
        ('decided', decided_at, decision_id, opinion_key, 'pending'))
    stored = connection.execute('SELECT * FROM renewal_decisions WHERE decision_id=?',
                                (decision_id,)).fetchone()
    return decision_payload(dict(stored))


def decision_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'decision_id': row['decision_id'],
        'round_id': row['round_id'],
        'opinion_id': row['opinion_id'],
        'outcome': row['outcome'],
        'recommendation': row['recommendation'],
        'approver': row['approver'],
        'note': row['note'],
        'basis_fingerprint': row['basis_fingerprint'],
        'comparison_fingerprint': row['comparison_fingerprint'],
        'observation_batch_ids': json.loads(row['observation_batch_ids_json']),
        'decided_at': row['decided_at'],
    }


def renewal_decisions(connection: Any, round_id: str) -> list[dict[str, Any]]:
    prepare_retrospective(connection)
    rows = connection.execute(
        'SELECT * FROM renewal_decisions WHERE round_id=? ORDER BY decided_at, decision_id',
        (normalized_text(round_id),)).fetchall()
    return [decision_payload(dict(row)) for row in rows]


def reproduce_decision_basis(connection: Any, decision: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the review from the batches captured in a decision.

    Nothing is recomputed from today's monthly report: the decision keeps its
    own ``as_of``, evidence cutoff and frozen reservation snapshot.  Missing or
    altered rows are reported as errors instead of silently changing the
    decision.
    """

    row = connection.execute('SELECT * FROM renewal_decisions WHERE decision_id=?',
                             (decision['decision_id'],)).fetchone()
    if row is None:
        raise ContractError('renewal decision is unknown')
    decision_row = dict(row)
    basis = json.loads(decision_row['basis_json'])
    errors: list[str] = []
    selections = {item['demand_id']: item for item in load_selections(connection, basis['round_id'])}
    for demand_id, fingerprint in basis['selection_fingerprints'].items():
        stored = selections.get(demand_id)
        if stored is None:
            errors.append(f'selection {demand_id} is missing')
        elif selection_fingerprint(stored) != fingerprint:
            errors.append(f'selection {demand_id} was altered')
    observations = {item['observation_id']: item for item in load_observations(connection, basis['round_id'])}
    for item in basis['effective_observations']:
        for source in item['sources']:
            stored = observations.get(source['observation_id'])
            if stored is None:
                errors.append(f"observation {source['observation_id']} is missing")
            elif observation_fingerprint(stored) != source['fingerprint']:
                errors.append(f"observation {source['observation_id']} was altered")
    batches = {item['batch_id']: item for item in load_batches(connection, basis['round_id'])}
    for item in basis['observation_batches']:
        stored = batches.get(item['batch_id'])
        if stored is None:
            errors.append(f"observation batch {item['batch_id']} is missing")
        elif stored['fingerprint'] != item['fingerprint']:
            errors.append(f"observation batch {item['batch_id']} was altered")

    reproduced = None
    if not errors:
        try:
            reproduced = assemble_review(
                [item for item in load_selections(connection, basis['round_id'])
                 if item['demand_id'] in basis['selection_fingerprints']],
                [item for item in load_batches(connection, basis['round_id'])
                 if item['batch_id'] in {entry['batch_id'] for entry in basis['observation_batches']}],
                [item for item in load_observations(connection, basis['round_id'])
                 if item['batch_id'] in {entry['batch_id'] for entry in basis['observation_batches']}],
                basis['reservation_snapshot'], as_of=basis['as_of'], rules=basis['rules'],
                evidence_cutoff=basis['evidence_cutoff'])
        except ContractError as exc:
            errors.append(str(exc))

    try:
        current = build_effect_review(connection, basis['round_id'])
    except ContractError as exc:
        errors.append(f'current review unavailable: {exc}')
        current = None
    recorded_basis_fingerprint = decision_row['basis_fingerprint']
    rebuilt_basis_fingerprint = stable_hash(basis)
    return {
        'decision_id': decision_row['decision_id'],
        'round_id': decision_row['round_id'],
        'outcome': decision_row['outcome'],
        'approver': decision_row['approver'],
        'decided_at': decision_row['decided_at'],
        'observation_batch_ids': [entry['batch_id'] for entry in basis['observation_batches']],
        'recorded_basis_fingerprint': recorded_basis_fingerprint,
        'rebuilt_basis_fingerprint': rebuilt_basis_fingerprint,
        'reproduced_basis_fingerprint': reproduced['basis_fingerprint'] if reproduced else None,
        'recorded_comparison_fingerprint': decision_row['comparison_fingerprint'],
        'reproduced_comparison_fingerprint': reproduced['comparison_fingerprint'] if reproduced else None,
        'reproducible': reproduced is not None,
        'basis_match': reproduced is not None and rebuilt_basis_fingerprint == recorded_basis_fingerprint,
        'errors': errors,
        'current_basis_fingerprint': current['basis_fingerprint'] if current else None,
        'current_recommendation': current['recommendation']['recommendation'] if current else None,
        'superseded_by_newer_evidence': (
            None if current is None else current['basis_fingerprint'] != recorded_basis_fingerprint),
    }
