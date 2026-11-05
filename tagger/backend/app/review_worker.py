"""Execute only explicitly started review batches; never enable ordinary tagging."""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import inspect
from app import review
from app.db import get_engine, session_scope
from app.openrouter_provider import annotate_openrouter
from app.provider_settings import load_settings

log = logging.getLogger(__name__)


def run_one():
    with session_scope() as s:
        task = review.claim(s)
    if not task:
        return False
    usage = {}
    try:
        provider = load_settings()
        if not provider['api_key']:
            raise ValueError('OpenRouter key is no longer configured.')
        def event(value):
            usage.update((value.get('data') or {}).get('usage') or {})
        proposal = annotate_openrouter(task['path'], task['prompt'], task['schema'], task['model'],
            api_key=provider['api_key'], timeout=120, on_event=event)
        with session_scope() as s:
            review.finish(s, task['id'], task['token'], proposal=proposal, usage=usage)
    except Exception as exc:
        log.exception('Review request failed; explicit retry required')
        with session_scope() as s:
            review.finish(s, task['id'], task['token'], usage=usage,
                          error=str(exc) if isinstance(exc, ValueError) else 'Review request failed. Check provider settings and start a new batch to retry.')
    return True


async def serve(stop):
    initialized = False
    tasks = set()
    executor = ThreadPoolExecutor(max_workers=review.MAX_CONCURRENT_REVIEWS, thread_name_prefix="maply-review")
    loop = asyncio.get_running_loop()
    try:
        while not stop.is_set():
            try:
                if not initialized:
                    if not await asyncio.to_thread(lambda: inspect(get_engine()).has_table('review_batches')):
                        await asyncio.sleep(1)
                        continue
                    def initialize():
                        with session_scope() as s:
                            review.pause_after_restart(s)
                    await asyncio.to_thread(initialize)
                    initialized = True
                finished = {t for t in tasks if t.done()}
                tasks -= finished
                for task in finished:
                    task.result()
                for _ in range(review.MAX_CONCURRENT_REVIEWS - len(tasks)):
                    tasks.add(loop.run_in_executor(executor, run_one))
            except Exception:
                log.exception('Review worker unavailable; no automatic paid retry')
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        executor.shutdown(wait=True)
        if initialized:
            with session_scope() as s:
                review.pause_after_restart(s)
