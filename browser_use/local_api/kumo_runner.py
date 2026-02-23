import asyncio
import re
from pathlib import Path

from browser_use import ActionResult, Agent, BrowserProfile, BrowserSession, ChatOpenAI, Tools

tools = Tools()


async def _click_first_by_css(page, selector: str) -> bool:
	"""Click the first matching element for a CSS selector."""
	els = await page.get_elements_by_css_selector(selector)
	if not els:
		return False
	await els[0].click()
	return True


async def _get_text_by_css(page, selector: str) -> str:
	return await page.evaluate(
		f"""() => {{
			const el = document.querySelector({selector!r});
			return el ? (el.textContent || el.innerText || '') : '';
		}}"""
	)


async def _wait_for_selector(page, selector: str, timeout_ms: int = 15_000) -> bool:
	start = asyncio.get_event_loop().time()
	while (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
		exists = await page.evaluate(f'() => Boolean(document.querySelector({selector!r}))')
		if exists:
			return True
		await asyncio.sleep(0.25)
	return False


def _parse_processed_from_count_text(count_text: str) -> tuple[int | None, int | None]:
	t = (count_text or '').strip().lower()
	m = re.match(r'^\s*(\d+)\s*\(\s*(\d+)\s*processed\s*\)\s*$', t)
	if m:
		return int(m.group(1)), int(m.group(2))
	m2 = re.match(r'^\s*(\d+)\s*\(\s*all\s*processed\s*[^)]*\)\s*$', t)
	if m2:
		total = int(m2.group(1))
		return total, total
	m3 = re.match(r'^\s*(\d+)\s*$', t)
	if m3:
		return int(m3.group(1)), None
	return None, None


async def _wait_for_ai_processing_ready(page, timeout_s: float = 240.0) -> tuple[bool, str]:
	start = asyncio.get_event_loop().time()
	last = ''
	while (asyncio.get_event_loop().time() - start) < timeout_s:
		status = (await _get_text_by_css(page, '#kumo-scrape-bar-status') or '').strip()
		count = (await _get_text_by_css(page, '#kumo-scrape-bar-count') or '').strip()
		progress = (await _get_text_by_css(page, '#kumo-scrape-bar-progress') or '').strip()

		status_l = status.lower()
		count_l = count.lower()
		last = f'status={status!r}, count={count!r}, progress={progress!r}'

		if 'download started' in status_l:
			return True, last
		if 'ready to download' in status_l:
			return True, last
		if 'all processed' in count_l:
			return True, last

		total, processed = _parse_processed_from_count_text(count_l)
		if total and processed is not None and processed >= total:
			return True, last

		if 'error' in status_l:
			return False, last

		await asyncio.sleep(2.0)

	return False, f'timed out after {timeout_s}s; last seen {last}'


async def _wait_for_drive_upload_result(page, timeout_s: float = 180.0) -> tuple[bool, str]:
	start = asyncio.get_event_loop().time()
	last = ''
	while (asyncio.get_event_loop().time() - start) < timeout_s:
		status = (await _get_text_by_css(page, '#kumo-scrape-bar-status') or '').strip()
		status_l = status.lower()
		last = f'status={status!r}'

		if 'upload failed' in status_l:
			return False, last
		if 'uploaded to drive' in status_l or 'uploaded to google drive' in status_l:
			return True, last

		await asyncio.sleep(2.0)

	return False, f'timed out after {timeout_s}s; last seen {last}'


@tools.action(
	description='Click the "Start" button in the Kumo Scraper plugin bar to begin extraction. The plugin bar should be visible at the top of the saved search page.'
)
async def click_plugin_start(browser_session: BrowserSession, page_extraction_llm) -> ActionResult:
	page = await browser_session.must_get_current_page()
	try:
		await _wait_for_selector(page, '#kumo-scrape-bar', timeout_ms=20_000)
		clicked = await _click_first_by_css(page, '#kumo-scrape-bar-start')
		if clicked:
			await asyncio.sleep(1.0)
			return ActionResult(extracted_content="Successfully clicked the 'Start' button in the plugin bar.")

		start_btn = await page.must_get_element_by_prompt(
			'Start button in the Kumo Scraper plugin bar at the top of the page', llm=page_extraction_llm
		)
		await start_btn.click()
		await asyncio.sleep(1.0)
		return ActionResult(extracted_content="Successfully clicked the 'Start' button in the plugin bar (via prompt).")
	except Exception as e:
		return ActionResult(extracted_content=f"Failed to click 'Start' button: {e}", error=str(e))


@tools.action(
	description='Click the "Clear" button in the Kumo Scraper plugin bar to clear any previously saved deals before starting a new extraction.'
)
async def click_plugin_clear(browser_session: BrowserSession, page_extraction_llm) -> ActionResult:
	page = await browser_session.must_get_current_page()
	try:
		await _wait_for_selector(page, '#kumo-scrape-bar', timeout_ms=20_000)
		clicked = await _click_first_by_css(page, '#kumo-scrape-bar-clear')
		if clicked:
			await asyncio.sleep(0.75)
			return ActionResult(extracted_content="Successfully clicked the 'Clear' button in the plugin bar.")

		clear_btn = await page.must_get_element_by_prompt('Clear button in the Kumo Scraper plugin bar', llm=page_extraction_llm)
		await clear_btn.click()
		await asyncio.sleep(0.75)
		return ActionResult(extracted_content="Successfully clicked the 'Clear' button in the plugin bar (via prompt).")
	except Exception as e:
		return ActionResult(extracted_content=f"Failed to click 'Clear' button: {e}", error=str(e))


@tools.action(
	description='Wait for the plugin extraction to complete. Monitor the status text and progress counter in the plugin bar. The extraction is done when the status shows "Done." or when progress matches total. Wait up to 90 seconds.'
)
async def wait_for_extraction_complete(browser_session: BrowserSession, page_extraction_llm) -> ActionResult:
	page = await browser_session.must_get_current_page()
	max_wait_seconds = 90
	check_interval = 2.0
	elapsed = 0.0

	try:
		await _wait_for_selector(page, '#kumo-scrape-bar-status', timeout_ms=20_000)
		while elapsed < max_wait_seconds:
			_ = page_extraction_llm
			status_text = (await _get_text_by_css(page, '#kumo-scrape-bar-status') or '').strip().lower()
			if 'done' in status_text or 'complete' in status_text:
				return ActionResult(extracted_content=f'Extraction completed! Status: {status_text}')
			if 'error' in status_text:
				return ActionResult(
					extracted_content=f'Extraction encountered an error. Status: {status_text}', error=status_text
				)

			progress_text = (await _get_text_by_css(page, '#kumo-scrape-bar-progress') or '').strip()
			if progress_text and '/' in progress_text:
				parts = progress_text.split('/')
				if len(parts) == 2:
					try:
						current = int(parts[0].strip())
						total = int(parts[1].strip())
						if current >= total and total > 0:
							await asyncio.sleep(2.0)
							status_text2 = (await _get_text_by_css(page, '#kumo-scrape-bar-status') or '').strip()
							if 'done' in status_text2.lower():
								return ActionResult(
									extracted_content=f'Extraction completed! Progress: {progress_text}, Status: {status_text2}'
								)
					except ValueError:
						pass

			await asyncio.sleep(check_interval)
			elapsed += check_interval

		return ActionResult(
			extracted_content=f'Waited {max_wait_seconds} seconds. Extraction may still be running or may have completed. Check the plugin status manually.',
			error=f'Timeout after {max_wait_seconds} seconds',
		)
	except Exception as e:
		return ActionResult(extracted_content=f'Error while waiting for extraction: {e}', error=str(e))


@tools.action(
	description='Click the "Download JSON" button in the Kumo Scraper plugin bar to download the extracted data as a JSON file.'
)
async def click_plugin_download_json(browser_session: BrowserSession, page_extraction_llm) -> ActionResult:
	page = await browser_session.must_get_current_page()
	try:
		await _wait_for_selector(page, '#kumo-scrape-bar', timeout_ms=20_000)
		ready, detail = await _wait_for_ai_processing_ready(page, timeout_s=240.0)
		if not ready:
			return ActionResult(
				extracted_content=f'AI processing not finished yet; refusing to download. ({detail})',
				error='AI processing not finished',
			)

		clicked = await _click_first_by_css(page, '#kumo-scrape-bar-dl-json')
		if clicked:
			await asyncio.sleep(1.0)
			return ActionResult(
				extracted_content="Successfully clicked the 'Download JSON' button. The download should start shortly."
			)

		dl_btn = await page.must_get_element_by_prompt(
			'Download JSON button in the Kumo Scraper plugin bar', llm=page_extraction_llm
		)
		await dl_btn.click()
		await asyncio.sleep(1.0)
		return ActionResult(
			extracted_content="Successfully clicked the 'Download JSON' button (via prompt). The download should start shortly."
		)
	except Exception as e:
		return ActionResult(extracted_content=f"Failed to click 'Download JSON' button: {e}", error=str(e))


@tools.action(
	description='Click the "Upload to Google Drive" button in the Kumo Scraper plugin bar. This should only be done after AI processing is complete (all processed). Wait for the upload to finish and report success/failure.'
)
async def click_plugin_upload_to_drive(browser_session: BrowserSession, page_extraction_llm) -> ActionResult:
	page = await browser_session.must_get_current_page()
	try:
		await _wait_for_selector(page, '#kumo-scrape-bar', timeout_ms=20_000)
		ready, detail = await _wait_for_ai_processing_ready(page, timeout_s=240.0)
		if not ready:
			return ActionResult(
				extracted_content=f'AI processing not finished yet; refusing to upload. ({detail})',
				error='AI processing not finished',
			)

		clicked = await _click_first_by_css(page, '#kumo-scrape-bar-upload-drive')
		if not clicked:
			btn = await page.must_get_element_by_prompt(
				'Upload to Google Drive button in the Kumo Scraper plugin bar', llm=page_extraction_llm
			)
			await btn.click()

		ok, status_detail = await _wait_for_drive_upload_result(page, timeout_s=180.0)
		if not ok:
			return ActionResult(
				extracted_content=f'Upload did not complete successfully. ({status_detail})',
				error='Upload failed or timed out',
			)

		final_status = (await _get_text_by_css(page, '#kumo-scrape-bar-status') or '').strip()
		return ActionResult(extracted_content=f'Upload completed. Status: {final_status}')
	except Exception as e:
		return ActionResult(extracted_content=f'Failed to upload to Google Drive: {e}', error=str(e))


def build_agent_task(saved_search_url: str, email: str, password: str) -> str:
	return f"""Navigate to {saved_search_url}.
If you see a login page, sign in with:
- Email: {email}
- Password: {password}

IMPORTANT: After logging in, you may land on a different page (like a normal search page).
You MUST navigate back to the saved search URL: {saved_search_url}
Make sure you are on the correct saved search page before proceeding.

Once you are confirmed to be on the saved search page ({saved_search_url}), you should see a plugin bar at the top of the page with buttons like \"Start\", \"Download JSON\", etc.

Steps to complete:
1. Wait for the plugin bar to appear at the top of the page (it should be visible with \"Kumo Scraper\" text and buttons)
2. Click the \"Clear\" button in the plugin bar using the click_plugin_clear tool to clear any previously saved results.
3. Click the \"Start\" button in the plugin bar using the click_plugin_start tool. This will begin the extraction process.
4. Wait for the extraction to complete using the wait_for_extraction_complete tool. This will monitor the plugin's status and wait up to 90 seconds for it to finish.
5. IMPORTANT: The plugin processes scraped deals with AI asynchronously after scraping. Only upload after AI processing is finished.
   - The UI will show \"ready to download\" or the Saved count will show \"(all processed ✓)\".
6. Click the \"Upload to Google Drive\" button using the click_plugin_upload_to_drive tool. This tool will also wait for AI processing to finish before clicking, and then wait for upload completion.

After you have successfully completed the Google Drive upload, mark the task as complete."""


async def run_kumo_extraction(
	saved_search_url: str,
	email: str,
	password: str,
	*,
	model: str = 'gpt-5-mini',
	downloads_path: str,
	user_data_dir: str,
	executable_path: str,
	keep_alive: bool = False,
	max_steps: int = 100,
):
	Path(downloads_path).mkdir(parents=True, exist_ok=True)

	browser_profile = BrowserProfile(
		user_data_dir=user_data_dir,
		executable_path=executable_path,
		keep_alive=keep_alive,
		accept_downloads=True,
		downloads_path=downloads_path,
	)
	llm = ChatOpenAI(model=model)
	task = build_agent_task(saved_search_url=saved_search_url, email=email, password=password)

	agent = Agent(task=task, llm=llm, browser_profile=browser_profile, tools=tools)
	return await agent.run(max_steps=max_steps)
