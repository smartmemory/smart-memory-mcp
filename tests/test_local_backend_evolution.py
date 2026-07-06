from smartmemory_mcp.backends.local import LocalBackend


class _Memory:
    seen = None

    def run_evolver(self, evolver_name, **kwargs):
        type(self).seen = (evolver_name, kwargs)
        return {"ok": True}


def test_local_backend_run_evolver_passes_name_to_core_facade():
    backend = object.__new__(LocalBackend)
    backend._mem = _Memory()

    result = backend.run_evolver("opinion_synthesis", log="logger")

    assert result == {"ok": True}
    assert _Memory.seen == ("opinion_synthesis", {"log": "logger"})
