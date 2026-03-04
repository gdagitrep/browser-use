import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from browser_use import Agent, BrowserProfile, ChatAnthropic, ChatBrowserUse, ChatGoogle, ChatOpenAI
from browser_use.local_api.kumo_runner import run_kumo_extraction


class JobStatus(StrEnum):
	queued = 'queued'
	running = 'running'
	succeeded = 'succeeded'
	failed = 'failed'
	cancelled = 'cancelled'


class CloudTaskStatus(StrEnum):
	running = 'running'
	finished = 'finished'
	failed = 'failed'
	stopped = 'stopped'


class RunTaskRequest(BaseModel):
	task: str = Field(min_length=1)
	llm_model: str = Field(default='bu-latest')
	llm_provider: Literal['browser_use', 'openai', 'google', 'anthropic'] = 'browser_use'
	session_id: str | None = None
	max_agent_steps: int = Field(default=100, ge=1, le=500)
	use_cloud: bool = False
	headless: bool | None = None
	keep_alive: bool = False
	accept_downloads: bool = True
	downloads_path: str | None = Field(default=str(Path.cwd() / 'downloads'))
	user_data_dir: str | None = None
	executable_path: str | None = None
	allowed_domains: list[str] | None = None
	prohibited_domains: list[str] | None = None
	use_vision: bool | Literal['auto'] = 'auto'
	# Kept for cloud API compatibility; currently ignored in local mode.
	enable_public_share: bool = False


class RunKumoTaskRequest(BaseModel):
	saved_search_url: str = Field(min_length=1)
	email: str = Field(min_length=1)
	password: str = Field(min_length=1)
	session_id: str | None = None
	llm_model: str = Field(default='gpt-5-mini')
	max_agent_steps: int = Field(default=100, ge=1, le=500)
	downloads_path: str = Field(default=str(Path.cwd() / 'downloads'))
	user_data_dir: str | None = None
	executable_path: str = Field(default='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
	keep_alive: bool = False


class TaskCreatedResponse(BaseModel):
	id: str
	status: CloudTaskStatus


class TaskStatusResponse(BaseModel):
	id: str
	status: CloudTaskStatus


class TaskDetailsResponse(BaseModel):
	id: str
	status: CloudTaskStatus
	created_at: datetime
	started_at: datetime | None = None
	completed_at: datetime | None = None
	output: str | None = None
	error: str | None = None
	steps: list[dict[str, Any]] = Field(default_factory=list)
	downloaded_files: list[str] = Field(default_factory=list)
	live_url: str | None = None
	public_share_url: str | None = None


class TaskListItem(BaseModel):
	id: str
	status: CloudTaskStatus
	created_at: datetime


class LegacyJobAccepted(BaseModel):
	job_id: str
	status: JobStatus


class LegacyJobState(BaseModel):
	job_id: str
	job_type: str
	status: JobStatus
	created_at: datetime
	started_at: datetime | None = None
	completed_at: datetime | None = None
	request: dict[str, Any]
	result: dict[str, Any] | None = None
	error: str | None = None


class JobStore:
	def __init__(self):
		self._lock = asyncio.Lock()
		self._jobs: dict[str, LegacyJobState] = {}
		self._tasks: dict[str, asyncio.Task] = {}

	async def create(self, *, job_type: str, request: dict[str, Any], runner) -> LegacyJobState:
		job_id = str(uuid4())
		job = LegacyJobState(
			job_id=job_id,
			job_type=job_type,
			status=JobStatus.queued,
			created_at=datetime.now(UTC),
			request=request,
		)
		async with self._lock:
			self._jobs[job_id] = job

		async def _run_wrapper():
			await self._set_running(job_id)
			try:
				result = await runner()
				await self._set_result(job_id, result)
			except asyncio.CancelledError:
				await self._set_cancelled(job_id)
				raise
			except Exception as e:
				await self._set_failed(job_id, str(e))

		task = asyncio.create_task(_run_wrapper(), name=f'local-api-job-{job_id}')
		async with self._lock:
			self._tasks[job_id] = task
		return job

	async def _set_running(self, job_id: str):
		async with self._lock:
			job = self._jobs[job_id]
			job.status = JobStatus.running
			job.started_at = datetime.now(UTC)

	async def _set_result(self, job_id: str, history):
		session_downloads: list[str] = []
		if isinstance(history, dict) and 'history' in history:
			session_downloads_raw = history.get('downloaded_files')
			if isinstance(session_downloads_raw, list):
				session_downloads = [str(p) for p in session_downloads_raw]
			history = history['history']

		errors_raw = _safe_call(history, 'errors')
		errors = errors_raw if isinstance(errors_raw, list) else []
		attachments = _collect_attachments(history)
		downloaded_files = list(dict.fromkeys([*session_downloads, *attachments]))
		payload = {
			'is_done': _safe_call(history, 'is_done'),
			'is_successful': _safe_call(history, 'is_successful'),
			'final_result': _safe_call(history, 'final_result'),
			'urls': _safe_call(history, 'urls') or [],
			'action_names': _safe_call(history, 'action_names') or [],
			'errors': [str(e) if e is not None else None for e in errors],
			'number_of_steps': _safe_call(history, 'number_of_steps'),
			'total_duration_seconds': _safe_call(history, 'total_duration_seconds'),
			'downloaded_files': downloaded_files,
		}
		async with self._lock:
			job = self._jobs[job_id]
			job.status = JobStatus.succeeded
			job.completed_at = datetime.now(UTC)
			job.result = payload

	async def _set_failed(self, job_id: str, error: str):
		async with self._lock:
			job = self._jobs[job_id]
			job.status = JobStatus.failed
			job.completed_at = datetime.now(UTC)
			job.error = error

	async def _set_cancelled(self, job_id: str):
		async with self._lock:
			job = self._jobs[job_id]
			job.status = JobStatus.cancelled
			job.completed_at = datetime.now(UTC)

	async def get(self, job_id: str) -> LegacyJobState:
		async with self._lock:
			job = self._jobs.get(job_id)
			if not job:
				raise KeyError(job_id)
			return job

	async def list(self) -> list[LegacyJobState]:
		async with self._lock:
			return list(self._jobs.values())

	async def cancel(self, job_id: str) -> LegacyJobState:
		async with self._lock:
			job = self._jobs.get(job_id)
			task = self._tasks.get(job_id)
		if not job:
			raise KeyError(job_id)
		if job.status in {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}:
			return job
		if task:
			task.cancel()
		return job


def _safe_call(history, method_name: str):
	method = getattr(history, method_name, None)
	if method is None:
		return None
	if callable(method):
		return method()
	return method


def _collect_attachments(history) -> list[str]:
	results = _safe_call(history, 'action_results')
	if not isinstance(results, list):
		return []

	files: list[str] = []
	for action_result in results:
		attachments = getattr(action_result, 'attachments', None)
		if isinstance(attachments, list):
			for path in attachments:
				files.append(str(path))
	return files


def _sanitize_session_id(session_id: str) -> str:
	allowed = []
	for ch in session_id:
		if ch.isalnum() or ch in {'-', '_'}:
			allowed.append(ch)
	return ''.join(allowed) or 'default'


def _resolve_user_data_dir(
	*,
	user_data_dir: str | None,
	session_id: str | None,
	namespace: str,
	default_name: str | None = None,
) -> str | None:
	"""
	Resolve a stable profile dir.

	If `session_id` is provided, we force a persistent browser-use profile path whose
	name includes `browser-use-user-data-dir-` so browser-use reuses it across runs.
	"""
	if user_data_dir:
		return user_data_dir

	if session_id:
		safe_session = _sanitize_session_id(session_id)
		return str(Path.home() / '.config' / 'browseruse' / f'browser-use-user-data-dir-{namespace}-{safe_session}')

	if default_name:
		return str(Path.home() / '.config' / 'browseruse' / default_name)

	return None


def _build_llm(request: RunTaskRequest):
	if request.llm_provider == 'browser_use':
		return ChatBrowserUse(model=request.llm_model)
	if request.llm_provider == 'openai':
		return ChatOpenAI(model=request.llm_model)
	if request.llm_provider == 'google':
		return ChatGoogle(model=request.llm_model)
	if request.llm_provider == 'anthropic':
		return ChatAnthropic(model=request.llm_model)
	raise ValueError(f'Unsupported llm_provider: {request.llm_provider}')


async def _run_generic_task(request: RunTaskRequest):
	if request.downloads_path:
		Path(request.downloads_path).mkdir(parents=True, exist_ok=True)
	resolved_user_data_dir = _resolve_user_data_dir(
		user_data_dir=request.user_data_dir,
		session_id=request.session_id,
		namespace='local-api',
	)
	if resolved_user_data_dir:
		Path(resolved_user_data_dir).expanduser().mkdir(parents=True, exist_ok=True)
	browser_profile = BrowserProfile(
		keep_alive=request.keep_alive,
		use_cloud=request.use_cloud,
		headless=request.headless,
		accept_downloads=request.accept_downloads,
		downloads_path=request.downloads_path,
		user_data_dir=resolved_user_data_dir,
		executable_path=request.executable_path,
		allowed_domains=request.allowed_domains,
		prohibited_domains=request.prohibited_domains,
	)
	llm = _build_llm(request)
	agent = Agent(task=request.task, llm=llm, browser_profile=browser_profile, use_vision=request.use_vision)
	history = await agent.run(max_steps=request.max_agent_steps)
	downloaded_files = agent.browser_session.downloaded_files if agent.browser_session else []
	return {'history': history, 'downloaded_files': downloaded_files}


def _to_cloud_status(status: JobStatus) -> CloudTaskStatus:
	if status in {JobStatus.queued, JobStatus.running}:
		return CloudTaskStatus.running
	if status == JobStatus.succeeded:
		return CloudTaskStatus.finished
	if status == JobStatus.failed:
		return CloudTaskStatus.failed
	return CloudTaskStatus.stopped


def _to_task_details(job: LegacyJobState) -> TaskDetailsResponse:
	result = job.result or {}
	return TaskDetailsResponse(
		id=job.job_id,
		status=_to_cloud_status(job.status),
		created_at=job.created_at,
		started_at=job.started_at,
		completed_at=job.completed_at,
		output=result.get('final_result'),
		error=job.error,
		steps=[],
		downloaded_files=result.get('downloaded_files') or [],
	)


app = FastAPI(title='Browser-Use Local API', version='0.2.0')
store = JobStore()


@app.get('/health')
async def health() -> dict[str, str]:
	return {'status': 'ok'}


# Cloud-like task API
@app.post('/api/v1/run-task')
async def run_task(request: RunTaskRequest) -> TaskCreatedResponse:
	job = await store.create(job_type='task', request=request.model_dump(), runner=lambda: _run_generic_task(request))
	return TaskCreatedResponse(id=job.job_id, status=_to_cloud_status(job.status))


@app.get('/api/v1/task/{task_id}')
async def get_task(task_id: str) -> TaskDetailsResponse:
	try:
		job = await store.get(task_id)
		return _to_task_details(job)
	except KeyError as e:
		raise HTTPException(status_code=404, detail=f'Task not found: {task_id}') from e


@app.get('/api/v1/task/{task_id}/status')
async def get_task_status(task_id: str) -> TaskStatusResponse:
	try:
		job = await store.get(task_id)
		return TaskStatusResponse(id=task_id, status=_to_cloud_status(job.status))
	except KeyError as e:
		raise HTTPException(status_code=404, detail=f'Task not found: {task_id}') from e


@app.get('/api/v1/tasks')
async def list_tasks() -> list[TaskListItem]:
	jobs = await store.list()
	return [TaskListItem(id=j.job_id, status=_to_cloud_status(j.status), created_at=j.created_at) for j in jobs]


@app.post('/api/v1/task/{task_id}/stop')
async def stop_task(task_id: str) -> TaskStatusResponse:
	try:
		job = await store.cancel(task_id)
		return TaskStatusResponse(id=task_id, status=_to_cloud_status(job.status))
	except KeyError as e:
		raise HTTPException(status_code=404, detail=f'Task not found: {task_id}') from e


@app.get('/api/v1/task/{task_id}/wait')
async def wait_task(task_id: str, timeout_seconds: float = 300.0, poll_interval: float = 1.0) -> TaskDetailsResponse:
	deadline = asyncio.get_running_loop().time() + timeout_seconds
	while True:
		try:
			job = await store.get(task_id)
		except KeyError as e:
			raise HTTPException(status_code=404, detail=f'Task not found: {task_id}') from e

		if job.status in {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}:
			return _to_task_details(job)

		if asyncio.get_running_loop().time() >= deadline:
			raise HTTPException(status_code=408, detail=f'Timeout waiting for task {task_id}')

		await asyncio.sleep(max(0.05, poll_interval))


# Optional specialized Kumo endpoint
@app.post('/api/v1/run-kumo-task')
async def run_kumo_task(request: RunKumoTaskRequest) -> TaskCreatedResponse:
	resolved_user_data_dir = _resolve_user_data_dir(
		user_data_dir=request.user_data_dir,
		session_id=request.session_id,
		namespace='kumo',
		default_name='browser-use-user-data-dir-kumo',
	)
	job = await store.create(
		job_type='kumo',
		request=request.model_dump(),
		runner=lambda: run_kumo_extraction(
			saved_search_url=request.saved_search_url,
			email=request.email,
			password=request.password,
			model=request.llm_model,
			downloads_path=request.downloads_path,
			user_data_dir=resolved_user_data_dir or '~/.config/browseruse/browser-use-user-data-dir-kumo',
			executable_path=request.executable_path,
			keep_alive=request.keep_alive,
			max_steps=request.max_agent_steps,
		),
	)
	return TaskCreatedResponse(id=job.job_id, status=_to_cloud_status(job.status))


# Legacy endpoints kept for backward compatibility
@app.post('/v1/jobs/agent')
async def create_agent_job_legacy(request: RunTaskRequest) -> LegacyJobAccepted:
	job = await store.create(job_type='agent', request=request.model_dump(), runner=lambda: _run_generic_task(request))
	return LegacyJobAccepted(job_id=job.job_id, status=job.status)


@app.post('/v1/jobs/kumo')
async def create_kumo_job_legacy(request: RunKumoTaskRequest) -> LegacyJobAccepted:
	resolved_user_data_dir = _resolve_user_data_dir(
		user_data_dir=request.user_data_dir,
		session_id=request.session_id,
		namespace='kumo',
		default_name='browser-use-user-data-dir-kumo',
	)
	job = await store.create(
		job_type='kumo',
		request=request.model_dump(),
		runner=lambda: run_kumo_extraction(
			saved_search_url=request.saved_search_url,
			email=request.email,
			password=request.password,
			model=request.llm_model,
			downloads_path=request.downloads_path,
			user_data_dir=resolved_user_data_dir or '~/.config/browseruse/browser-use-user-data-dir-kumo',
			executable_path=request.executable_path,
			keep_alive=request.keep_alive,
			max_steps=request.max_agent_steps,
		),
	)
	return LegacyJobAccepted(job_id=job.job_id, status=job.status)


@app.get('/v1/jobs/{job_id}')
async def get_job_legacy(job_id: str) -> LegacyJobState:
	try:
		return await store.get(job_id)
	except KeyError as e:
		raise HTTPException(status_code=404, detail=f'Job not found: {job_id}') from e


@app.get('/v1/jobs')
async def list_jobs_legacy() -> list[LegacyJobState]:
	return await store.list()


@app.post('/v1/jobs/{job_id}/cancel')
async def cancel_job_legacy(job_id: str) -> LegacyJobState:
	try:
		return await store.cancel(job_id)
	except KeyError as e:
		raise HTTPException(status_code=404, detail=f'Job not found: {job_id}') from e


def main():
	import uvicorn

	uvicorn.run('browser_use.local_api.app:app', host='0.0.0.0', port=8000, reload=False)


if __name__ == '__main__':
	main()
