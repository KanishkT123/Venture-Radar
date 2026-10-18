import os
import threading
from .research import run_research


class Worker(threading.Thread):
    def __init__(self, store):
        super().__init__(name='radar-worker', daemon=True)
        self.store = store
        self.stop_event = threading.Event()

    def run(self):
        self.store.recover()
        while not self.stop_event.is_set():
            if os.getenv('OPENAI_API_KEY'):
                try:
                    self.store.schedule_if_due()
                except ValueError:
                    pass  # Daily cap; retry eligibility on the next tick.
            job = self.store.claim()
            if job:
                try:
                    self.store.finish(job['id'], result=run_research(self.store, job))
                except Exception as e:
                    # Avoid saving credentials or arbitrary HTTP response bodies.
                    message = str(e)[:800] if isinstance(e, ValueError) else f'{type(e).__name__}: workflow failed; retry after checking configuration.'
                    self.store.finish(job['id'], error=message)
            else:
                self.stop_event.wait(2)
