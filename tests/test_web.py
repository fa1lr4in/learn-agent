import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from learn_agent.api import create_app
from test_api import KEY, TOKEN, OfflineProvider, call, reply


class WebWorkbenchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="learn-agent-web-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.project = self.root / '中文 & 项目'
        self.project.mkdir()
        self.source = self.project / 'main.py'
        self.source.write_text('print(5050)\n', encoding='utf-8')
        self.provider = OfflineProvider()
        self.app = create_app(self.project, self.root / 'logs', KEY, TOKEN, provider=self.provider)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        output = patch('sys.stdout', new=io.StringIO())
        output.start()
        self.addCleanup(output.stop)

    def test_page_bootstraps_only_local_token_and_escapes_workspace(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="api-token" content="{TOKEN}"', response.text)
        self.assertNotIn(KEY, response.text)
        self.assertIn('中文 &amp; 项目', response.text)
        self.assertIn('default-src \'self\'', response.headers['content-security-policy'])
        self.assertEqual(response.headers['x-frame-options'], 'DENY')
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_page_rejects_foreign_origin_and_host(self):
        self.assertEqual(self.client.get('/', headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(self.client.get('/', headers={'Host': 'other.example'}).status_code, 400)

    def test_only_web_assets_are_served(self):
        for name in ['app.js', 'client.js', 'view-model.js', 'theme.js', 'styles.css']:
            with self.subTest(asset=name):
                response = self.client.get('/assets/' + name)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(KEY, response.text)
                self.assertNotIn(TOKEN, response.text)
        for path in ['/assets/../api.py', '/assets/%2e%2e/api.py', '/assets/.api-server.json', '/assets/../logs/sessions.sqlite3']:
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_task_index_requires_authorization_and_does_not_create_tasks(self):
        self.assertEqual(self.client.get('/tasks').status_code, 401)
        self.client.headers['Authorization'] = f'Bearer {TOKEN}'
        self.assertEqual(self.client.get('/tasks').json(), [])
        self.assertEqual(self.provider.requests, [])

    def test_task_index_finds_waiting_and_completed_task(self):
        self.client.headers['Authorization'] = f'Bearer {TOKEN}'
        self.provider.replies = [reply(call('run_python', {'path': 'main.py'})), reply()]
        session = self.client.post('/sessions').json()['id']
        task = self.client.post(f'/sessions/{session}/tasks', json={'prompt': '运行入口。'}).json()
        with self.app.state.manager.condition:
            self.assertTrue(self.app.state.manager.condition.wait_for(lambda: self.app.state.manager.tasks[task['id']].pending_approval is not None, timeout=5))
        self.assertEqual(self.client.get('/tasks').json(), [{'id': task['id'], 'session_id': session, 'finished': False}])
        pending = self.client.get('/tasks/' + task['id']).json()['pending_approval']
        self.client.post(f'/tasks/{task["id"]}/approvals', json={'approval_id': pending['id'], 'approved': True})
        self.app.state.manager.tasks[task['id']].thread.join(5)
        self.assertTrue(self.client.get('/tasks').json()[0]['finished'])
        self.assertEqual(len(self.provider.requests), 2)

    def edit_task(self):
        self.client.headers['Authorization'] = f'Bearer {TOKEN}'
        self.provider.replies = [reply(call('edit_file', {'path': 'main.py', 'old_text': '5050', 'new_text': '5051'})), reply()]
        session = self.client.post('/sessions').json()['id']
        task = self.client.post(f'/sessions/{session}/tasks', json={'prompt': '修改入口。'}).json()
        with self.app.state.manager.condition:
            self.assertTrue(self.app.state.manager.condition.wait_for(lambda: self.app.state.manager.tasks[task['id']].pending_approval is not None, timeout=5))
        return self.client.get('/tasks/' + task['id']).json()

    def test_prepared_diff_is_available_before_approval_and_matches_applied_result(self):
        task = self.edit_task()
        pending = task['pending_approval']
        preview = pending['preview']
        self.assertIn('-print(5050)', preview)
        self.assertIn('+print(5051)', preview)
        self.assertEqual(self.source.read_text(), 'print(5050)\n')
        self.assertFalse(any(e['event'] == 'tool_result' for e in task['events']))
        waiting = next(e for e in task['events'] if e['event'] == 'tool_waiting')
        self.assertEqual(waiting['preview'], preview)
        self.client.post(f'/tasks/{task["id"]}/approvals', json={'approval_id': pending['id'], 'approved': True})
        self.app.state.manager.tasks[task['id']].thread.join(5)
        final = self.client.get('/tasks/' + task['id']).json()
        result = next(e['result'] for e in final['events'] if e['event'] == 'tool_result')
        self.assertEqual(result['diff'], preview)
        self.assertEqual(self.source.read_text(), 'print(5051)\n')
        self.assertTrue(final['session_saved'])

    def test_rejecting_edit_keeps_file_and_has_no_tool_execution(self):
        task = self.edit_task()
        self.client.post(f'/tasks/{task["id"]}/approvals', json={'approval_id': task['pending_approval']['id'], 'approved': False})
        self.app.state.manager.tasks[task['id']].thread.join(5)
        final = self.client.get('/tasks/' + task['id']).json()
        self.assertEqual((final['state'], final['reason']), ('cancelled', 'user_denied'))
        self.assertEqual(self.source.read_text(), 'print(5050)\n')
        self.assertFalse(any(e['event'] in ('tool_started', 'tool_result') for e in final['events']))

    def test_file_changed_while_approval_waits_is_not_overwritten(self):
        task = self.edit_task()
        self.source.write_text('print(6000)\n', encoding='utf-8')
        self.client.post(f'/tasks/{task["id"]}/approvals', json={'approval_id': task['pending_approval']['id'], 'approved': True})
        self.app.state.manager.tasks[task['id']].thread.join(5)
        final = self.client.get('/tasks/' + task['id']).json()
        result = next(e['result'] for e in final['events'] if e['event'] == 'tool_result')
        self.assertFalse(result['ok'])
        self.assertEqual(self.source.read_text(), 'print(6000)\n')

    def test_duplicate_submission_while_waiting_does_not_create_or_run_another_task(self):
        task = self.edit_task()
        for session in [task['session_id'], self.client.post('/sessions').json()['id']]:
            response = self.client.post(f'/sessions/{session}/tasks', json={'prompt': '重复提交。'})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.client.get('/tasks').json()), 1)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.source.read_text(), 'print(5050)\n')
        self.client.post(f'/tasks/{task["id"]}/cancel')
        self.app.state.manager.tasks[task['id']].thread.join(5)

    def test_old_approval_cannot_authorize_next_tool_and_observation_does_not_replay(self):
        task = self.edit_task()
        self.provider.replies = [reply(call('run_python', {'path': 'main.py'})), reply()]
        old = {'approval_id': task['pending_approval']['id'], 'approved': True}
        self.assertEqual(self.client.post(f'/tasks/{task["id"]}/approvals', json=old).status_code, 202)
        with self.app.state.manager.condition:
            self.assertTrue(self.app.state.manager.condition.wait_for(
                lambda: (pending := self.app.state.manager.tasks[task['id']].pending_approval) is not None and pending['id'] != old['approval_id'], timeout=5))
        self.assertEqual(self.client.post(f'/tasks/{task["id"]}/approvals', json=old).status_code, 409)
        current = self.client.get('/tasks/' + task['id']).json()
        self.assertEqual(current['pending_approval']['tool'], 'run_python')
        self.assertEqual(self.source.read_text(), 'print(5051)\n')
        self.assertEqual([e['tool'] for e in current['events'] if e['event'] == 'tool_result'], ['edit_file'])
        self.client.post(f'/tasks/{task["id"]}/approvals', json={'approval_id': current['pending_approval']['id'], 'approved': True})
        self.app.state.manager.tasks[task['id']].thread.join(5)
        final = self.client.get('/tasks/' + task['id']).json()
        self.assertEqual((final['state'], final['reason'], final['session_saved']), ('completed', 'stop', True))
        self.assertEqual(len(self.provider.requests), 3)
        saved = self.client.get('/sessions/' + task['session_id']).json()
        self.assertEqual([r['result']['ok'] for r in saved['tool_runs']], [True, True])
        self.assertEqual(saved['tool_runs'][1]['result']['stdout'].strip(), '5051')
        for _ in range(3):
            self.assertEqual(self.client.get('/tasks/' + task['id']).json(), final)
            self.assertEqual(self.client.get('/sessions/' + task['session_id']).json(), saved)
        self.assertEqual(len(self.provider.requests), 3)

    def test_new_backend_keeps_saved_history_but_not_in_memory_tasks_or_old_token(self):
        task = self.edit_task()
        self.client.post(f'/tasks/{task["id"]}/approvals', json={'approval_id': task['pending_approval']['id'], 'approved': False})
        self.app.state.manager.tasks[task['id']].thread.join(5)
        saved = self.client.get('/sessions/' + task['session_id']).json()
        new_provider = OfflineProvider()
        app = create_app(self.project, self.root / 'logs', KEY, 'new-process-token', provider=new_provider)
        with TestClient(app, base_url='http://127.0.0.1:8000') as client:
            self.assertEqual(client.get('/tasks', headers={'Authorization': f'Bearer {TOKEN}'}).status_code, 401)
            client.headers['Authorization'] = 'Bearer new-process-token'
            self.assertEqual(client.get('/tasks').json(), [])
            self.assertEqual(client.get('/tasks/' + task['id']).status_code, 404)
            self.assertEqual(client.get('/sessions/' + task['session_id']).json(), saved)
            self.assertEqual(saved['tool_runs'][0]['approved'], False)
            self.assertIsNone(saved['tool_runs'][0]['result'])
            self.assertEqual(saved['reason'], 'user_denied')
            self.assertEqual(new_provider.requests, [])
            self.assertEqual(self.source.read_text(), 'print(5050)\n')


if __name__ == '__main__':
    unittest.main()
