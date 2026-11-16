from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scene_exchange.coordinator import Application
from scene_exchange.intake.demand_contract import ContractError, DomainRecord
from scene_exchange.pilots import retrospective
from scene_exchange.registry.exchange_db import Repository


INTAKE = {
    'sector': 'manufacturing', 'problem_severity': 8, 'data_readiness': 0.8,
    'deployment_window': 90, 'budget_band': 7, 'safety_class': 2,
    'localization_need': 0.6, 'integration_count': 4, 'supplier_capacity': 0.7,
    'evidence_strength': 0.8, 'conflict_count': 1, 'expected_benefit': 0.9,
}


def intake(sector: str = 'manufacturing', **updates):
    return dict(INTAKE, sector=sector, **updates)


def selection(demand_id: str, *, selected: bool, sector: str = 'manufacturing',
              **updates) -> dict:
    body = {'demand_id': demand_id, 'sector': sector, 'selected': selected,
            'intake': intake(sector)}
    body.update(updates)
    return body


class RetrospectiveCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'review.sqlite3'
        self.application = Application(self.database)

    def seed_round(self, selections, *, recorded_at='2026-01-01T00:00:00+00:00',
                   round_id='round-1', batch_id='selection-1'):
        return self.application.record_selection_batch(round_id, batch_id, selections,
                                                       recorded_at=recorded_at)

    def observe(self, observations, *, batch_id, period='2026-02',
                kind='initial', recorded_at='2026-02-01T00:00:00+00:00',
                round_id='round-1'):
        return self.application.record_observation_batch(
            round_id, batch_id, period, kind, observations, recorded_at=recorded_at)

    def basic_round(self):
        return self.seed_round([
            selection('selected-a', selected=True),
            selection('selected-b', selected=True),
            selection('unselected-a', selected=False),
            selection('unselected-b', selected=False),
        ])

    def mature_observations(self, **updates):
        rows = [
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'selected-b', 'started': True, 'benefit': 0.8},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.5},
            {'demand_id': 'unselected-b', 'started': False},
        ]
        return rows


class EffectReviewTests(RetrospectiveCase):
    def test_selection_snapshot_and_untouched_intake_are_preserved(self):
        selection('selected-a', selected=True)
        self.seed_round([selection('selected-a', selected=True)])
        with sqlite3.connect(self.database) as connection:
            snapshot = connection.execute(
                'SELECT intake_json,selected,score FROM pilot_selections').fetchone()
        self.assertEqual(snapshot[1], 1)
        self.assertEqual(json.loads(snapshot[0]), intake())
        self.assertGreater(snapshot[2], 0.0)

    def test_selection_snapshot_is_immutable(self):
        self.seed_round([selection('selected-a', selected=True)])
        changed = selection('selected-a', selected=True)
        changed['intake'] = intake(problem_severity=1)
        with self.assertRaisesRegex(ContractError, 'immutable'):
            self.seed_round([changed])

    def test_selection_requires_pre_selection_snapshot(self):
        with self.assertRaisesRegex(ContractError, 'pre-selection demand snapshot'):
            self.seed_round([{'demand_id': 'x', 'selected': True, 'sector': 's'}])

    def test_selected_demand_high_landing_rate_is_not_enough(self):
        # Selected demands land 100%, but the unselected demands with the same
        # pre-selection conditions have the same outcome: no rule advantage.
        self.basic_round()
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.5},
            {'demand_id': 'selected-b', 'started': True, 'benefit': 0.5},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.5},
            {'demand_id': 'unselected-b', 'started': True, 'benefit': 0.5},
        ], batch_id='obs-1')
        review = self.application.effect_review('round-1')
        self.assertEqual(review['comparison']['selected']['start_rate'], 1.0)
        self.assertEqual(review['comparison']['unselected']['start_rate'], 1.0)
        self.assertEqual(review['recommendation']['recommendation'], 'do_not_renew')
        self.assertIn('no_benefit_advantage', review['recommendation']['reasons'])

    def test_renewal_when_selected_benefit_clearly_better(self):
        self.basic_round()
        self.observe(self.mature_observations(), batch_id='obs-1')
        review = self.application.effect_review(
            'round-1', as_of='2026-06-01T00:00:00+00:00')
        self.assertAlmostEqual(review['comparison']['difference']['benefit_mean'], 0.35)
        self.assertEqual(review['recommendation']['recommendation'], 'renew')
        self.assertNotIn('early_observations_included', review['recommendation']['reasons'])

    def test_early_results_only_support_conditional_renewal(self):
        self.basic_round()
        self.observe(self.mature_observations(), batch_id='obs-1')
        review = self.application.effect_review('round-1')
        self.assertEqual(review['comparison']['selected']['maturity'], {'early': 2})
        self.assertEqual(review['recommendation']['recommendation'], 'conditional_renew')
        self.assertIn('renewal_conditional_on_mature_results',
                      review['recommendation']['reasons'])

    def test_pending_observation_is_not_a_failure(self):
        self.seed_round([
            selection('selected-a', selected=True),
            selection('selected-future', selected=True,
                      selected_at='2026-06-01T00:00:00+00:00'),
            selection('unselected-a', selected=False),
        ], recorded_at='2026-01-01T00:00:00+00:00')
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.4},
        ], batch_id='obs-1')
        review = self.application.effect_review(
            'round-1', as_of='2026-03-01T00:00:00+00:00')
        outcome = {row['demand_id']: row for row in review['outcomes']}['selected-future']
        self.assertEqual(outcome['status'], 'pending_observation')
        self.assertFalse(outcome['observed'])
        self.assertEqual(review['counts']['selected_pending_observation'], 1)
        self.assertEqual(review['counts']['selected_no_result'], 0)
        self.assertIn('pending_observations_excluded', review['recommendation']['reasons'])
        self.assertEqual(review['comparison']['selected']['observed_count'], 1)

    def test_no_result_after_window_is_disclosed_not_failed(self):
        self.seed_round([selection('selected-a', selected=True),
                         selection('unselected-a', selected=False)])
        review = self.application.effect_review(
            'round-1', as_of='2026-12-01T00:00:00+00:00')
        outcome = {row['demand_id']: row for row in review['outcomes']}['selected-a']
        self.assertEqual(outcome['status'], 'no_result')
        self.assertFalse(outcome['observed'])
        self.assertIn('no_result_disclosed', review['recommendation']['reasons'])

    def test_do_not_count_pending_as_failed_in_sector_comparison(self):
        self.basic_round()
        self.observe(self.mature_observations(), batch_id='obs-1')
        # A third selected demand from another sector has not reached its window.
        self.seed_round([selection('selected-z', selected=True, sector='health')],
                        batch_id='selection-2', recorded_at='2026-01-02T00:00:00+00:00')
        review = self.application.effect_review(
            'round-1', as_of='2026-02-15T00:00:00+00:00')
        health = [row for row in review['incomparable_sectors'] if row['sector'] == 'health'][0]
        self.assertIn('no_selected_observation', health['reasons'])
        self.assertEqual(health['selected_pending_observation'], ['selected-z'])
        self.assertNotIn('health', [row['sector'] for row in review['comparable_sectors']])

    def test_incomparable_sector_has_no_control_group_explained(self):
        self.seed_round([
            selection('selected-a', selected=True),
            selection('selected-x', selected=True, sector='health'),
            selection('unselected-a', selected=False),
        ])
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.4},
            {'demand_id': 'selected-x', 'started': True, 'benefit': 0.9},
        ], batch_id='obs-1')
        review = self.application.effect_review('round-1')
        health = [row for row in review['incomparable_sectors'] if row['sector'] == 'health'][0]
        self.assertIn('no_unselected_demand', health['reasons'])
        self.assertTrue(health['explanations'])
        self.assertEqual(review['counts']['incomparable_sector_count'], 1)

    def test_unselected_demand_outside_score_band_is_not_a_comparison(self):
        self.seed_round([
            selection('selected-a', selected=True, score=0.9),
            selection('unselected-a', selected=False, score=0.2),
        ])
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.2},
        ], batch_id='obs-1')
        review = self.application.effect_review('round-1')
        sector = review['sectors'][0]
        self.assertFalse(sector['comparable'])
        self.assertIn('no_selected_observation_within_band', sector['reasons'])
        self.assertEqual(review['recommendation']['recommendation'], 'insufficient_evidence')

    def test_comparison_records_pre_selection_conditions(self):
        self.basic_round()
        self.observe(self.mature_observations(), batch_id='obs-1')
        review = self.application.effect_review('round-1')
        comparison = review['comparison']
        self.assertIn('pre_selection_score_mean', comparison['selected_conditions'])
        self.assertIn('problem_severity', comparison['selected_conditions']['pre_selection_conditions'])
        self.assertIsNotNone(comparison['difference']['pre_selection_score'])
        self.assertEqual(review['basis']['rules']['require_same_sector'], True)

    def test_cross_sector_comparison_requires_authorization(self):
        self.basic_round()
        with self.assertRaisesRegex(ContractError, 'cross-sector'):
            retrospective.merge_rules({'require_same_sector': False})


class ObservationLifecycleTests(RetrospectiveCase):
    def setUp(self):
        super().setUp()
        self.basic_round()
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'selected-b', 'started': True, 'benefit': 0.8},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.5},
            {'demand_id': 'unselected-b', 'started': False},
        ], batch_id='obs-1')
        self.pending = self.application.pending_opinions('round-1')[0]
        self.assertEqual(self.pending['recommendation'], 'conditional_renew')

    def test_supplemental_result_supersedes_pending_opinion(self):
        before = self.pending
        result = self.observe([
            {'demand_id': 'unselected-b', 'started': True, 'benefit': 0.95},
        ], batch_id='obs-2', kind='supplemental', period='2026-03',
            recorded_at='2026-03-05T00:00:00+00:00')
        self.assertTrue(result['created'])
        self.assertEqual(result['superseded'], [before['opinion_id']])
        self.assertNotEqual(result['opinion']['opinion_id'], before['opinion_id'])
        self.assertNotEqual(result['opinion']['basis_fingerprint'], before['basis_fingerprint'])

    def test_retraction_restores_prior_observation(self):
        correction = self.observe([
            {'demand_id': 'selected-a', 'benefit': 0.1,
             'supersedes': self._observation_id('selected-a', 'obs-1')},
        ], batch_id='obs-2', kind='correction', period='2026-02',
            recorded_at='2026-03-01T00:00:00+00:00')
        corrected = self.application.effect_review('round-1')
        self.assertAlmostEqual(
            [row for row in corrected['outcomes'] if row['demand_id'] == 'selected-a'][0]['benefit'], 0.1)
        retracted = self.observe([
            {'demand_id': 'selected-a', 'supersedes': correction['batch']['observations'][0]},
        ], batch_id='obs-3', kind='retraction', period='2026-02',
            recorded_at='2026-04-01T00:00:00+00:00')
        final = self.application.effect_review('round-1')
        outcome = [row for row in final['outcomes'] if row['demand_id'] == 'selected-a'][0]
        self.assertAlmostEqual(outcome['benefit'], 0.9)

    def test_correction_in_later_month_supersedes_earlier_result(self):
        correction = self.observe([
            {'demand_id': 'unselected-b', 'started': True, 'benefit': 0.1,
             'supersedes': self._observation_id('unselected-b', 'obs-1')},
        ], batch_id='obs-2', kind='correction', period='2026-03',
            recorded_at='2026-03-01T00:00:00+00:00')
        review = self.application.effect_review('round-1')
        outcome = [row for row in review['outcomes'] if row['demand_id'] == 'unselected-b'][0]
        self.assertEqual(outcome['observation_period'], '2026-03')
        self.assertAlmostEqual(outcome['benefit'], 0.1)
        self.assertTrue(outcome['started'])
        self.observe([
            {'demand_id': 'unselected-b', 'supersedes': correction['batch']['observations'][0]},
        ], batch_id='obs-3', kind='retraction', period='2026-03',
            recorded_at='2026-04-01T00:00:00+00:00')
        restored = self.application.effect_review('round-1')
        outcome = [row for row in restored['outcomes'] if row['demand_id'] == 'unselected-b'][0]
        self.assertEqual(outcome['observation_period'], '2026-02')
        self.assertFalse(outcome['started'])

    def test_retraction_requires_target(self):
        with self.assertRaisesRegex(ContractError, 'retraction requires'):
            self.observe([{'demand_id': 'selected-a', 'benefit': 0.1}],
                         batch_id='obs-x', kind='retraction',
                         recorded_at='2026-03-01T00:00:00+00:00')

    def test_unknown_demand_observation_is_rejected(self):
        with self.assertRaisesRegex(ContractError, 'outside the round'):
            self.observe([{'demand_id': 'nope', 'benefit': 0.1}], batch_id='obs-x',
                         recorded_at='2026-03-01T00:00:00+00:00')

    def test_observation_batch_is_immutable(self):
        with self.assertRaisesRegex(ContractError, 'immutable'):
            self.observe([{'demand_id': 'selected-a', 'benefit': 0.2}],
                         batch_id='obs-1', recorded_at='2026-03-01T00:00:00+00:00')

    def test_pilot_exit_refreshes_pending_opinion(self):
        before = self.application.pending_opinions('round-1')[0]
        self.application.repository.save_record(DomainRecord('selected-a', {'sector': 'manufacturing'}))
        self.application.repository.save_record(DomainRecord('selected-b', {'sector': 'manufacturing'}))
        self.application.register_supplier('supplier-a', 2)
        self.application.accept_pilot('selected-a', 'supplier-a')
        self.application.finish_pilot('selected-a', 'withdrawn')
        after = self.application.pending_opinions('round-1')
        self.assertEqual(len(after), 1)
        self.assertNotEqual(after[0]['opinion_id'], before['opinion_id'])
        review = self.application.effect_review('round-1')
        outcome = [row for row in review['outcomes'] if row['demand_id'] == 'selected-a'][0]
        self.assertTrue(outcome['exited'])
        self.assertEqual(outcome['reservation_status'], 'withdrawn')

    def test_unrelated_pilot_exit_does_not_change_opinion(self):
        before = self.application.pending_opinions('round-1')[0]
        self.application.repository.save_record(DomainRecord('outside', {'sector': 'manufacturing'}))
        self.application.register_supplier('supplier-a', 1)
        self.application.accept_pilot('outside', 'supplier-a')
        self.application.finish_pilot('outside', 'withdrawn')
        after = self.application.pending_opinions('round-1')
        self.assertEqual(after[0]['opinion_id'], before['opinion_id'])

    def _observation_id(self, demand_id, batch_id):
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                'SELECT observation_id FROM pilot_observations WHERE demand_id=? AND batch_id=?',
                (demand_id, batch_id)).fetchone()
        return row[0]


class RenewalDecisionTests(RetrospectiveCase):
    def setUp(self):
        super().setUp()
        self.basic_round()
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'selected-b', 'started': True, 'benefit': 0.8},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.5},
            {'demand_id': 'unselected-b', 'started': False},
        ], batch_id='obs-1')
        self.opinion = self.application.pending_opinions('round-1')[0]

    def test_decision_records_verifiable_basis(self):
        decision = self.application.decide_renewal(
            'round-1', self.opinion['opinion_id'], 'renew', 'review-board', note='round one')
        self.assertEqual(decision['outcome'], 'renew')
        self.assertEqual(decision['observation_batch_ids'], ['obs-1'])
        self.assertEqual(decision['basis_fingerprint'], self.opinion['basis_fingerprint'])
        check = self.application.reproduce_renewal_decision(decision)
        self.assertTrue(check['reproducible'])
        self.assertTrue(check['basis_match'])
        self.assertEqual(check['errors'], [])

    def test_new_monthly_report_does_not_change_decided_decision(self):
        decision = self.application.decide_renewal(
            'round-1', self.opinion['opinion_id'], 'renew', 'review-board')
        self.observe([
            {'demand_id': 'selected-a', 'benefit': 0.05},
            {'demand_id': 'selected-b', 'benefit': 0.05},
            {'demand_id': 'unselected-a', 'benefit': 0.95},
        ], batch_id='obs-2', kind='correction', period='2026-03',
            recorded_at='2026-03-10T00:00:00+00:00')
        stored = self.application.renewal_decisions('round-1')
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['decision_id'], decision['decision_id'])
        self.assertEqual(stored[0]['basis_fingerprint'], decision['basis_fingerprint'])
        check = self.application.reproduce_renewal_decision(stored[0])
        self.assertTrue(check['reproducible'])
        self.assertTrue(check['basis_match'])
        self.assertTrue(check['superseded_by_newer_evidence'])
        self.assertEqual(check['current_recommendation'], 'do_not_renew')

    def test_decided_opinion_cannot_be_superseded(self):
        self.application.decide_renewal(
            'round-1', self.opinion['opinion_id'], 'conditional_renew', 'review-board')
        self.assertEqual(self.application.pending_opinions('round-1'), [])
        self.observe([{'demand_id': 'selected-a', 'benefit': 0.1}],
                     batch_id='obs-2', kind='correction', period='2026-03',
                     recorded_at='2026-03-10T00:00:00+00:00')
        # A new pending opinion exists for the next review, but the old one is
        # still recorded as decided and untouched.
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT status FROM renewal_opinions WHERE opinion_id=?',
                                     (self.opinion['opinion_id'],)).fetchone()
        self.assertEqual(row[0], 'decided')
        self.assertEqual(len(self.application.renewal_decisions('round-1')), 1)

    def test_superseded_opinion_cannot_be_decided(self):
        first = self.opinion
        self.observe([{'demand_id': 'selected-a', 'benefit': 0.1}],
                     batch_id='obs-2', kind='correction', period='2026-03',
                     recorded_at='2026-03-10T00:00:00+00:00')
        with self.assertRaisesRegex(ContractError, 'superseded'):
            self.application.decide_renewal('round-1', first['opinion_id'], 'renew', 'board')

    def test_decision_records_basis_even_when_inputs_are_tampered(self):
        decision = self.application.decide_renewal(
            'round-1', self.opinion['opinion_id'], 'renew', 'review-board')
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE pilot_observations SET benefit=0.01 WHERE demand_id='selected-a'")
        check = self.application.reproduce_renewal_decision(decision)
        self.assertFalse(check['reproducible'])
        self.assertTrue(check['errors'])
        self.assertTrue(any('altered' in error for error in check['errors']))

    def test_unknown_outcome_is_rejected(self):
        with self.assertRaisesRegex(ContractError, 'unsupported renewal outcome'):
            self.application.decide_renewal('round-1', self.opinion['opinion_id'], 'keep', 'board')

    def test_decision_is_idempotent(self):
        first = self.application.decide_renewal('round-1', self.opinion['opinion_id'], 'renew', 'board')
        second = self.application.decide_renewal('round-1', self.opinion['opinion_id'], 'renew', 'board')
        self.assertEqual(first['decision_id'], second['decision_id'])
        self.assertEqual(len(self.application.renewal_decisions('round-1')), 1)

    def test_conflicting_redelivery_is_rejected(self):
        self.application.decide_renewal('round-1', self.opinion['opinion_id'], 'renew', 'board')
        with self.assertRaisesRegex(ContractError, 'already decided'):
            self.application.decide_renewal('round-1', self.opinion['opinion_id'], 'do_not_renew', 'board')

    def test_decision_reproduces_even_after_correction_of_the_same_row(self):
        decision = self.application.decide_renewal(
            'round-1', self.opinion['opinion_id'], 'renew', 'board')
        # A later correction supersedes the original row but leaves it intact;
        # the decision's evidence is still reproducible from the recorded batch.
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT observation_id FROM pilot_observations WHERE demand_id='selected-a'").fetchone()
        self.observe([{'demand_id': 'selected-a', 'benefit': 0.02,
                       'supersedes': row['observation_id']}],
                     batch_id='obs-2', kind='correction', period='2026-02',
                     recorded_at='2026-03-01T00:00:00+00:00')
        check = self.application.reproduce_renewal_decision(decision)
        self.assertTrue(check['reproducible'])
        self.assertTrue(check['basis_match'])
        self.assertTrue(check['superseded_by_newer_evidence'])
        self.assertEqual(check['current_recommendation'], 'do_not_renew')


class ReviewPersistenceTests(RetrospectiveCase):
    def test_existing_database_without_review_tables_is_upgraded(self):
        legacy = self.root / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as connection:
            connection.execute('CREATE TABLE legacy_marker(x)')
        repository = Repository(legacy)
        with sqlite3.connect(legacy) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('pilot_selections', tables)
        self.assertIn('renewal_opinions', tables)
        self.assertIn('renewal_decisions', tables)
        self.assertEqual(repository.snapshot()['chain_errors'], [])

    def test_review_is_repeatable_without_new_opinion(self):
        self.basic_round()
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'selected-b', 'started': True, 'benefit': 0.8},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.5},
        ], batch_id='obs-1')
        first = self.application.refresh_renewal_opinion('round-1')
        second = self.application.refresh_renewal_opinion('round-1')
        self.assertFalse(second['created'])
        self.assertEqual(first['opinion']['opinion_id'], second['opinion']['opinion_id'])
        self.assertEqual(second['superseded'], [])

    def test_selection_batch_replay_is_idempotent(self):
        self.basic_round()
        replay = self.seed_round([
            selection('selected-a', selected=True),
            selection('selected-b', selected=True),
            selection('unselected-a', selected=False),
            selection('unselected-b', selected=False),
        ])
        with sqlite3.connect(self.database) as connection:
            count = connection.execute('SELECT COUNT(*) FROM pilot_selections').fetchone()[0]
        self.assertEqual(count, 4)
        self.assertTrue(replay['fingerprint'])

    def test_cli_round_trip(self):
        import io
        from contextlib import redirect_stdout
        from scene_exchange import commands

        self.basic_round()
        self.observe([
            {'demand_id': 'selected-a', 'started': True, 'benefit': 0.9},
            {'demand_id': 'selected-b', 'started': True, 'benefit': 0.8},
            {'demand_id': 'unselected-a', 'started': True, 'benefit': 0.5},
        ], batch_id='obs-1')
        output = io.StringIO()
        with redirect_stdout(output):
            code = commands.main(['review-pilots', '--database', str(self.database),
                                  '--round', 'round-1', '--refresh'])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['opinion']['recommendation'], 'conditional_renew')
        self.assertEqual(payload['review']['round_id'], 'round-1')


if __name__ == '__main__':
    unittest.main()
