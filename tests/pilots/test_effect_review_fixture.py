from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scene_exchange import commands
from scene_exchange.coordinator import Application

FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures' / 'effect_review.json'


class EffectReviewFixtureTests(unittest.TestCase):
    """End-to-end acceptance for the review fixture and CLI surface."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'review.sqlite3'
        self.application = Application(self.database)
        fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.selections = fixture['selections']
        self.batches = fixture['observation_batches']

    def seed(self):
        self.application.record_selection_batch('round-2026a', 'selection-2026a',
                                                self.selections,
                                                recorded_at='2026-01-05T09:00:00+08:00')
        for batch in self.batches:
            self.application.record_observation_batch(
                'round-2026a', batch['batch_id'], batch['period'], batch['kind'],
                batch['observations'], note=batch['note'], recorded_at=batch['recorded_at'])

    def test_fixture_reviews_only_the_comparable_sector(self):
        self.seed()
        review = self.application.effect_review('round-2026a')
        comparable = {row['sector'] for row in review['comparable_sectors']}
        incomparable = {row['sector']: row for row in review['incomparable_sectors']}
        self.assertEqual(comparable, {'manufacturing'})
        self.assertIn('healthcare', incomparable)
        self.assertIn('no_unselected_demand', incomparable['healthcare']['reasons'])
        self.assertEqual(review['comparison']['selected_ids'],
                         ['pilot-selected-001', 'pilot-selected-002'])
        self.assertEqual(review['comparison']['unselected_ids'],
                         ['pilot-unselected-001', 'pilot-unselected-002'])
        self.assertEqual(review['recommendation']['recommendation'], 'conditional_renew')

    def test_supplemental_batch_is_part_of_the_recorded_basis(self):
        self.seed()
        review = self.application.effect_review('round-2026a')
        batch_ids = [batch['batch_id'] for batch in review['basis']['observation_batches']]
        self.assertEqual(batch_ids, ['observation-2026-02',
                                     'observation-2026-03-supplemental'])
        self.assertEqual(len(review['basis']['effective_observations']), 5)

    def test_cli_workflow_records_and_reproduces_decision(self):
        selection_fixture = FIXTURE
        output = io.StringIO()
        with redirect_stdout(output):
            commands.main(['record-selections', '--database', str(self.database),
                           '--round', 'round-2026a', '--batch', 'selection-2026a',
                           '--fixture', str(selection_fixture),
                           '--recorded-at', '2026-01-05T09:00:00+08:00'])
        for batch in self.batches:
            with redirect_stdout(output):
                commands.main(['record-observations', '--database', str(self.database),
                               '--round', 'round-2026a', '--batch', batch['batch_id'],
                               '--period', batch['period'], '--kind', batch['kind'],
                               '--fixture', str(selection_fixture), '--note', batch['note'],
                               '--recorded-at', batch['recorded_at']])
        with redirect_stdout(output):
            commands.main(['review-pilots', '--database', str(self.database),
                           '--round', 'round-2026a', '--refresh'])
        refreshed = json.loads(output.getvalue().strip().splitlines()[-1])
        opinion_id = refreshed['opinion']['opinion_id']

        with redirect_stdout(output):
            commands.main(['decide-renewal', '--database', str(self.database),
                           '--round', 'round-2026a', '--opinion', opinion_id,
                           '--outcome', 'conditional_renew', '--approver', '主管部门'])
        decision = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(decision['outcome'], 'conditional_renew')
        self.assertEqual(decision['observation_batch_ids'],
                         ['observation-2026-02', 'observation-2026-03-supplemental'])

        with redirect_stdout(output):
            commands.main(['reproduce-decision', '--database', str(self.database),
                           '--decision', decision['decision_id']])
        check = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertTrue(check['reproducible'])
        self.assertTrue(check['basis_match'])
        self.assertFalse(check['superseded_by_newer_evidence'])

    def test_elapsed_window_without_result_is_disclosed(self):
        self.seed()
        self.application.record_selection_batch(
            'round-2026a', 'selection-2026a-late',
            [dict(self.selections[0],
                  demand_id='pilot-selected-004', selected_at='2026-01-06T09:00:00+08:00')],
            recorded_at='2026-01-06T09:00:00+08:00')
        late = self.application.effect_review('round-2026a', as_of='2027-01-01T00:00:00+00:00')
        self.assertEqual(late['counts']['selected_no_result'], 1)
        self.assertEqual(late['counts']['no_result_ids'], ['pilot-selected-004'])
        self.assertIn('no_result_disclosed', late['recommendation']['reasons'])
        # The demands that do have results remain observed; the missing result
        # is a disclosed data gap, not a failure.
        self.assertEqual(late['counts']['selected_observed'], 3)
        # All remaining selected results are mature, so a renewal opinion is
        # possible, but the missing result is disclosed rather than counted as
        # a failure.
        self.assertEqual(late['recommendation']['recommendation'], 'renew')

    def test_round_overview_lists_pending_opinions(self):
        self.seed()
        self.application.refresh_renewal_opinion('round-2026a')
        overview = self.application.review_rounds()
        self.assertEqual(len(overview), 1)
        self.assertEqual(overview[0]['round_id'], 'round-2026a')
        self.assertEqual(overview[0]['selections'], 5)
        self.assertEqual(overview[0]['selected_count'], 3)
        self.assertEqual(overview[0]['pending_opinions'], 1)


if __name__ == '__main__':
    unittest.main()
