"""StatusReader keeps draining a dispatcher's stderr through isolated read errors."""


def test_isolated_read_failures_never_stop_the_draining():
    import io

    from cairnwork.workers.agy_dispatch import StatusReader

    class EveryOther(io.BytesIO):
        calls = 0

        def readline(self, size: int | None = -1):
            self.calls += 1
            if self.calls % 2:
                raise OSError("boom")
            return super().readline(size)

    lines = b"noise\n" * 5 + b'NEXUS_AGY_DISPATCH {"status": "pool_exhausted"}\n'
    reader = StatusReader(EveryOther(lines))
    reader.run()  # five isolated failures, never three in a row
    assert reader.status() == {"status": "pool_exhausted"}
