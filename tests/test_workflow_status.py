"""Exercise status projection with recorded GitHub responses and state transitions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pr_status import describe_ci, describe_item, publish, read_status

FIXTURES = Path(__file__).parent / 'fixtures'


class WorkflowStatusTests(unittest.TestCase):
    def setUp(self):
        self.run = json.loads((FIXTURES / 'base-ci-failure.json').read_text())
        self.metadata = json.loads((FIXTURES / 'workflow-metadata.json').read_text())

    def test_base_ci_uses_current_pr_head_and_event(self):
        head = self.run['head_sha']
        ci = describe_ci(self.run, head)
        self.assertEqual(ci['state'], 'failure')
        self.assertEqual(ci['url'], self.run['html_url'])
        self.assertEqual(describe_ci(self.run, 'different-head')['state'], 'unknown')
        other = {**self.run, 'event': 'workflow_dispatch'}
        self.assertEqual(describe_ci(other, head)['state'], 'unknown')

    def test_new_head_never_inherits_old_green(self):
        previous = {'head_sha': 'old-head', 'state': 'success', 'url': self.run['html_url']}
        ci = describe_ci(None, 'new-head', previous)
        self.assertEqual(ci['state'], 'stale')
        self.assertEqual(ci['previous_state'], 'success')
        self.assertEqual(ci['current_head_sha'], 'new-head')
        ci = describe_ci(None, 'new-head', ci)
        self.assertEqual(ci['previous_state'], 'success')

    def test_pending_skipped_cancelled_and_absence_are_distinct(self):
        self.assertEqual(describe_ci(None, self.run['head_sha'])['state'], 'not_run')
        for state in ('skipped', 'cancelled', 'neutral', 'success', 'timed_out'):
            run = {**self.run, 'conclusion': state, 'status': 'completed'}
            self.assertEqual(describe_ci(run, run['head_sha'])['state'], state)
        run = {**self.run, 'status': 'in_progress', 'conclusion': None}
        self.assertEqual(describe_ci(run, run['head_sha'])['state'], 'in_progress')

    def test_owners_and_reviewers_are_separate(self):
        pr = next(v for v in self.metadata if v['number'] == 38803)
        result = describe_item(pr)
        self.assertEqual(result['assignees'], [])
        self.assertIn('merrymercy', result['reviewers'])
        self.assertEqual(result['review'], 'REVIEW_REQUIRED')
        self.assertEqual(result['head_sha'], pr['headRefOid'])
        issue = describe_item(next(v for v in self.metadata if v['number'] == 38485))
        self.assertNotIn('base_ci', issue)
        self.assertNotIn('reviewers', issue)

    def test_unknown_conflict_does_not_mean_clear(self):
        item = copy.deepcopy(self.metadata[0])
        item['mergeable'] = 'UNKNOWN'
        self.assertEqual(describe_item(item)['conflict'], 'unknown')

    def test_review_activity_is_not_called_unreviewed(self):
        item = copy.deepcopy(self.metadata[0])
        item['reviewDecision'] = None
        item['latestReviews']['nodes'] = [{'author': {'login':'reviewer'}, 'state':'COMMENTED', 'submittedAt':item['updatedAt'], 'url':self.run['html_url']}]
        self.assertEqual(describe_item(item)['review'], 'REVIEW_ACTIVITY')

    def test_slow_refresh_cannot_overwrite_newer_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publish(root, {'38803': {'checked_at': '2026-09-10T06:40:00Z', 'state': 'merged'}})
            publish(root, {'38803': {'checked_at': '2026-09-10T06:30:00Z', 'state': 'open'}})
            self.assertEqual(read_status(root)['entries']['38803']['state'], 'merged')
            publish(root, {'38485': {'checked_at': '2026-09-10T06:45:00Z', 'state': 'open'}})
            self.assertEqual(len(read_status(root)['entries']), 2)


if __name__ == '__main__':
    unittest.main()
