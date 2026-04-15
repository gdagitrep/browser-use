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
	async def _fake_runner(request, api_key_headers=None):
		_ = request
		_ = api_key_headers
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
	async def _fake_runner(request, api_key_headers=None):
		_ = request
		_ = api_key_headers
		return _FakeHistory()

	monkeypatch.setattr(local_api, '_run_generic_task', _fake_runner)

	with TestClient(local_api.app) as client:
		resp = client.post('/v1/jobs/agent', json={'task': 'legacy endpoint'})
		resp.raise_for_status()
		job_id = resp.json()['job_id']

		legacy_resp = client.get(f'/v1/jobs/{job_id}')
		legacy_resp.raise_for_status()
		assert legacy_resp.json()['job_id'] == job_id


def test_run_task_passes_api_key_header_without_storing(monkeypatch):
	seen: dict[str, str | None] = {}

	async def _fake_runner(request, api_key_headers=None):
		seen['key'] = api_key_headers.for_provider(request.llm_provider) if api_key_headers else None
		return _FakeHistory()

	monkeypatch.setattr(local_api, '_run_generic_task', _fake_runner)

	with TestClient(local_api.app) as client:
		resp = client.post(
			'/api/v1/run-task',
			json={'task': 'open example.com', 'llm_provider': 'openai', 'llm_model': 'gpt-5-mini'},
			headers={'X-OpenAI-API-Key': 'sk-test-openai'},
		)
		resp.raise_for_status()
		task_id = resp.json()['id']

		assert _wait_for_finished(client, task_id) == 'finished'
		assert seen['key'] == 'sk-test-openai'

		legacy_resp = client.get(f'/v1/jobs/{task_id}')
		legacy_resp.raise_for_status()
		request_payload = legacy_resp.json()['request']
		assert 'openai_api_key' not in request_payload
		assert 'api_key' not in request_payload


def test_build_llm_uses_provider_specific_header_over_generic():
	request = local_api.RunTaskRequest(task='open example.com', llm_provider='openai', llm_model='gpt-5-mini')
	headers = local_api.LLMAPIKeyHeaders(api_key='sk-generic', openai_api_key='sk-openai')

	llm = local_api._build_llm(request, api_key_headers=headers)

	assert llm.api_key == 'sk-openai'


def test_build_llm_uses_generic_header():
	request = local_api.RunTaskRequest(task='open example.com', llm_provider='openai', llm_model='gpt-5-mini')
	headers = local_api.LLMAPIKeyHeaders(api_key='sk-generic')

	llm = local_api._build_llm(request, api_key_headers=headers)

	assert llm.api_key == 'sk-generic'


async def test_run_generic_task_creates_user_data_dir(monkeypatch, tmp_path):
	profile_dir = tmp_path / 'profile'
	downloads_dir = tmp_path / 'downloads'

	class FakeAgent:
		browser_session = None

		def __init__(self, **kwargs):
			_ = kwargs

		async def run(self, **kwargs):
			_ = kwargs
			return _FakeHistory()

	monkeypatch.setattr(local_api, '_build_llm', lambda *args, **kwargs: object())
	monkeypatch.setattr(local_api, 'Agent', FakeAgent)

	request = local_api.RunTaskRequest(
		task='open example.com',
		llm_provider='openai',
		llm_model='gpt-5-mini',
		downloads_path=str(downloads_dir),
		user_data_dir=str(profile_dir),
	)

	await local_api._run_generic_task(request)

	assert profile_dir.is_dir()
	assert downloads_dir.is_dir()


def test_session_id_resolves_stable_profile_dir():
	path1 = local_api._resolve_user_data_dir(user_data_dir=None, session_id='team-a', namespace='local-api')
	path2 = local_api._resolve_user_data_dir(user_data_dir=None, session_id='team-a', namespace='local-api')

	assert path1 == path2
	assert path1 is not None
	assert 'browser-use-user-data-dir-local-api-team-a' in path1
