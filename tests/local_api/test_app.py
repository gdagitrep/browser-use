import time
from importlib import import_module

from fastapi.testclient import TestClient

local_api = import_module('browser_use.local_api.app')


class _FakeHistory:
	def is_done(self):
		return True

	def is_successful(self):
		return True

	def final_result(self):
		return 'ok'

	def urls(self):
		return ['https://example.com']

	def action_names(self):
		return ['navigate']

	def errors(self):
		return [None]

	def number_of_steps(self):
		return 1

	def total_duration_seconds(self):
		return 0.1


def _wait_for_finished(client: TestClient, task_id: str, timeout: float = 2.0):
	deadline = time.time() + timeout
	while time.time() < deadline:
		status_resp = client.get(f'/api/v1/task/{task_id}/status')
		status_resp.raise_for_status()
		status = status_resp.json()['status']
		if status in {'finished', 'failed', 'stopped'}:
			return status
		time.sleep(0.05)
	raise AssertionError(f'task {task_id} did not finish in time')


def test_run_task_cloud_like(monkeypatch):
	async def _fake_runner(request):
		_ = request
		return _FakeHistory()

	monkeypatch.setattr(local_api, '_run_generic_task', _fake_runner)

	with TestClient(local_api.app) as client:
		resp = client.post('/api/v1/run-task', json={'task': 'open example.com'})
		resp.raise_for_status()
		task_id = resp.json()['id']

		assert _wait_for_finished(client, task_id) == 'finished'

		detail_resp = client.get(f'/api/v1/task/{task_id}')
		detail_resp.raise_for_status()
		detail = detail_resp.json()
		assert detail['status'] == 'finished'
		assert detail['output'] == 'ok'


def test_run_kumo_task_cloud_like(monkeypatch):
	async def _fake_kumo(**kwargs):
		_ = kwargs
		return _FakeHistory()

	monkeypatch.setattr(local_api, 'run_kumo_extraction', _fake_kumo)

	with TestClient(local_api.app) as client:
		resp = client.post(
			'/api/v1/run-kumo-task',
			json={'saved_search_url': 'https://example.com/saved', 'email': 'user@example.com', 'password': 'secret'},
		)
		resp.raise_for_status()
		task_id = resp.json()['id']

		assert _wait_for_finished(client, task_id) == 'finished'


def test_legacy_endpoint_still_works(monkeypatch):
	async def _fake_runner(request):
		_ = request
		return _FakeHistory()

	monkeypatch.setattr(local_api, '_run_generic_task', _fake_runner)

	with TestClient(local_api.app) as client:
		resp = client.post('/v1/jobs/agent', json={'task': 'legacy endpoint'})
		resp.raise_for_status()
		job_id = resp.json()['job_id']

		legacy_resp = client.get(f'/v1/jobs/{job_id}')
		legacy_resp.raise_for_status()
		assert legacy_resp.json()['job_id'] == job_id


def test_session_id_resolves_stable_profile_dir():
	path1 = local_api._resolve_user_data_dir(user_data_dir=None, session_id='team-a', namespace='local-api')
	path2 = local_api._resolve_user_data_dir(user_data_dir=None, session_id='team-a', namespace='local-api')

	assert path1 == path2
	assert path1 is not None
	assert 'browser-use-user-data-dir-local-api-team-a' in path1
