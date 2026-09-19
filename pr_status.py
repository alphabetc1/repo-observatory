"""Collect read-only GitHub ownership, review, mergeability and current-head CI."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from cacheboard import REPO, atomic_json, stamp
from settings import CONFIG

OWNER, REPOSITORY_NAME = REPO.split('/')

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("cache-observatory-status")
PERSON = 'nodes { login } pageInfo { hasNextPage endCursor }'
COMMON = f'number state updatedAt assignees(first:100) {{ {PERSON} }}'
FIELDS = '''isDraft headRefOid baseRefName mergeable reviewDecision
reviewRequests(first:100) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } ... on Bot { login } } } pageInfo { hasNextPage endCursor } }
latestReviews(first:100) { nodes { author { login } state submittedAt url } pageInfo { hasNextPage endCursor } }'''


class StatusClient:
    def __init__(self, use_gh=False):
        self.use_gh = use_gh
        credential_dir = os.environ.get('CREDENTIALS_DIRECTORY')
        self.token = (Path(credential_dir) / 'github-token').read_text().strip() if credential_dir else os.environ.get('GITHUB_TOKEN', '')
        if not use_gh and not self.token:
            raise RuntimeError('GitHub authentication is not configured for status refresh')

    def request(self, endpoint, query=None):
        for attempt in range(4):
            try:
                if self.use_gh:
                    args = ['gh', 'api', endpoint]
                    payload = None
                    if query:
                        args += ['--input', '-']
                        payload = json.dumps({'query': query})
                    result = subprocess.run(args, input=payload, text=True, capture_output=True, timeout=65)
                    if result.returncode:
                        raise RuntimeError('GitHub CLI query failed: ' + result.stderr[:250])
                    value = json.loads(result.stdout)
                else:
                    headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'Cache-Observatory', 'X-GitHub-Api-Version': '2022-11-28'}
                    data = json.dumps({'query': query}).encode() if query else None
                    req = urllib.request.Request('https://api.github.com/' + endpoint, headers=headers, data=data)
                    with urllib.request.urlopen(req, timeout=60) as response:
                        value = json.load(response)
                if isinstance(value, dict) and value.get('errors'):
                    raise RuntimeError('GitHub GraphQL response is incomplete: ' + str(value['errors'])[:250])
                return value
            except urllib.error.HTTPError as error:
                if error.code not in (500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError('GitHub query returned HTTP ' + str(error.code)) from error
            except (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired, RuntimeError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError('GitHub query did not complete')

    def metadata(self, numbers):
        fields = ' '.join(f'n{n}:issueOrPullRequest(number:{n}) {{ __typename ... on Issue {{ {COMMON} }} ... on PullRequest {{ {COMMON} {FIELDS} }} }}' for n in numbers)
        data = self.request('graphql', f'query {{ repository(owner:{json.dumps(OWNER)}, name:{json.dumps(REPOSITORY_NAME)}) {{' + fields + '} }')['data']['repository']
        result = []
        for n in numbers:
            item = data[f'n{n}']
            if not item:
                raise RuntimeError(f'GitHub did not return #{n}')
            for key in ('assignees', 'reviewRequests', 'latestReviews'):
                connection = item.get(key)
                while connection and connection['pageInfo']['hasNextPage']:
                    nodes = {'assignees': 'login', 'reviewRequests': 'requestedReviewer { __typename ... on User { login } ... on Team { slug } ... on Bot { login } }', 'latestReviews': 'author { login } state submittedAt url'}[key]
                    cursor = json.dumps(connection['pageInfo']['endCursor'])
                    fragment = f"{key}(first:100, after:{cursor}) {{ nodes {{ {nodes} }} pageInfo {{ hasNextPage endCursor }} }}"
                    query = f'query {{ repository(owner:{json.dumps(OWNER)}, name:{json.dumps(REPOSITORY_NAME)}) {{ issueOrPullRequest(number:{n}) {{ ... on {item["__typename"]} {{ {fragment} }} }} }} }}'
                    extra = self.request('graphql', query)['data']['repository']['issueOrPullRequest'][key]
                    if extra['pageInfo']['hasNextPage'] and extra['pageInfo']['endCursor'] == connection['pageInfo']['endCursor']:
                        raise RuntimeError('GitHub pagination did not advance')
                    connection['nodes'].extend(extra['nodes'])
                    connection['pageInfo'] = extra['pageInfo']
            result.append(item)
        return result

    def base_ci(self, item, previous):
        params = urllib.parse.urlencode({'head_sha': item['headRefOid'], 'event': CONFIG['workflow_event'], 'per_page': 1})
        workflow = urllib.parse.quote(CONFIG['workflow'], safe='')
        value = self.request(f'repos/{REPO}/actions/workflows/{workflow}/runs?{params}')
        runs = value['workflow_runs']
        ci = describe_ci(runs[0] if runs else None, item['headRefOid'], previous)
        if ci.get('run_id') and ci['state'] in ('failure', 'timed_out', 'action_required'):
            if previous.get('run_id') == ci['run_id'] and previous.get('attempt') == ci['attempt'] and previous.get('state') == ci['state'] and previous.get('jobs_checked'):
                ci['failed_jobs'] = previous.get('failed_jobs', [])
                ci['jobs_checked'] = True
            else:
                jobs = []
                page = 1
                while True:
                    data = self.request(f'repos/{REPO}/actions/runs/{ci["run_id"]}/attempts/{ci["attempt"]}/jobs?per_page=100&page={page}')
                    jobs.extend(data['jobs'])
                    if len(jobs) >= data['total_count']:
                        break
                    if not data['jobs']:
                        raise RuntimeError('Incomplete workflow jobs response')
                    page += 1
                ci['failed_jobs'] = [{'name': j['name'], 'state': j['conclusion'], 'url': j['html_url']} for j in jobs if j['conclusion'] in ('failure', 'timed_out', 'action_required')]
                ci['jobs_checked'] = True
        return ci


def describe_ci(run, head, previous=None):
    previous = previous or {}
    if not run:
        if previous.get('head_sha') and previous['head_sha'] != head and previous.get('url'):
            return {**previous, 'state': 'stale', 'previous_state': previous.get('previous_state', previous.get('state')), 'current_head_sha': head}
        return {'state': 'not_run', 'head_sha': head, 'failed_jobs': []}
    if run['head_sha'] != head or run['event'] != CONFIG['workflow_event']:
        return {'state': 'unknown', 'head_sha': head, 'failed_jobs': []}
    state = run.get('conclusion') if run['status'] == 'completed' else run['status']
    return {'state': state or 'unknown', 'head_sha': head, 'run_id': run['id'], 'attempt': run['run_attempt'], 'url': run['html_url'], 'updated_at': run['updated_at'], 'failed_jobs': []}


def describe_item(item):
    result = {'number': item['number'], 'state': item['state'].lower(), 'assignees': [v['login'] for v in item['assignees']['nodes']], 'checked_at': stamp(), 'source_updated_at': item['updatedAt'], 'error': None}
    if item['__typename'] == 'PullRequest':
        result.update({'draft': item['isDraft'], 'head_sha': item['headRefOid'], 'base_ref': item['baseRefName'], 'conflict': {'MERGEABLE': 'clear', 'CONFLICTING': 'conflict', 'UNKNOWN': 'unknown'}[item['mergeable']], 'review': item['reviewDecision'] or ('REVIEW_ACTIVITY' if item['latestReviews']['nodes'] else 'UNREVIEWED'), 'reviewers': [v['requestedReviewer'].get('login') or v['requestedReviewer'].get('slug') for v in item['reviewRequests']['nodes'] if v['requestedReviewer']], 'reviews': [{'author': (v['author'] or {}).get('login', 'deleted-user'), 'state': v['state'], 'at': v['submittedAt'], 'url': v['url']} for v in item['latestReviews']['nodes']]})
    return result


def read_status(data_dir):
    path = data_dir / 'workflow-status.json'
    value = json.loads(path.read_text()) if path.exists() else {'entries': {}, 'updated_at': None}
    if value.get('repository', REPO) != REPO:
        raise ValueError('Data directory belongs to another repository')
    value['repository'] = REPO
    return value


def publish(data_dir, updates):
    with (data_dir / 'workflow-status.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read_status(data_dir)
        for key, update in updates.items():
            if update.get('checked_at', '') >= value['entries'].get(key, {}).get('checked_at', ''):
                value['entries'][key] = update
        value['updated_at'] = stamp()
        atomic_json(data_dir / 'workflow-status.json', value)
    return value


def refresh(data_dir, numbers=None, use_gh=False):
    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot = json.loads((data_dir / 'snapshot.json').read_text())
    if snapshot.get('repository', REPO) != REPO:
        raise ValueError('Data directory belongs to another repository')
    allowed = {v['number'] for v in snapshot['entries']}
    numbers = sorted(allowed if numbers is None else set(numbers) & allowed)
    client = StatusClient(use_gh)
    previous = read_status(data_dir)['entries']
    errors = 0

    def collect(item):
        key = str(item['number'])
        result = describe_item(item)
        if item['__typename'] == 'PullRequest' and item['state'] == 'OPEN':
            try:
                result['base_ci'] = client.base_ci(item, previous.get(key, {}).get('base_ci', {}))
            except Exception as error:
                result['base_ci'] = previous.get(key, {}).get('base_ci', {'state': 'unknown'})
                if result['base_ci'].get('head_sha') not in (None, result['head_sha']):
                    result['base_ci'] = {**result['base_ci'], 'state': 'stale', 'previous_state': result['base_ci'].get('previous_state', result['base_ci'].get('state'))}
                result['error'] = str(error)[:300]
        return key, result

    for offset in range(0, len(numbers), 20):
        batch = numbers[offset:offset + 20]
        try:
            items = client.metadata(batch)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                updates = dict(pool.map(collect, items))
            unresolved = [int(key) for key, value in updates.items() if value.get('conflict') == 'unknown' and value.get('state') == 'open']
            if unresolved:
                try:
                    for item in client.metadata(unresolved):
                        value = updates[str(item['number'])]
                        if item.get('headRefOid') == value.get('head_sha'):
                            value['conflict'] = {'MERGEABLE': 'clear', 'CONFLICTING': 'conflict', 'UNKNOWN': 'unknown'}[item['mergeable']]
                except Exception:
                    # GitHub may still be computing mergeability; unknown is explicit.
                    LOG.warning('Mergeability retry unavailable; retaining unknown state')
        except Exception as error:
            updates = {str(n): {**previous.get(str(n), {'number': n}), 'error': str(error)[:300], 'last_attempt': stamp()} for n in batch}
        errors += sum(bool(v.get('error')) for v in updates.values())
        publish(data_dir, updates)
        LOG.info('Refreshed %s/%s entries; errors=%s', min(offset + 20, len(numbers)), len(numbers), errors)
    return {'updated': len(numbers), 'errors': errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--use-gh', action='store_true')
    parser.add_argument('--number', type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    result = refresh(args.data_dir, [args.number] if args.number else None, args.use_gh)
    print(json.dumps(result))
    return int(bool(result['errors']))


if __name__ == '__main__':
    raise SystemExit(main())
