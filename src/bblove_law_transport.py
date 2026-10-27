"""Timed full-corpus transport; shared pilot transport remains unchanged.

The child reports network/body-read duration, not URLs or exception messages.
Parent elapsed minus that duration measures spawn/import/IPC/cleanup overhead.
"""
import io
import multiprocessing
import time
import urllib.error

from bblove_tax_p2_collect import MAX_BYTES, PilotBlocked, RemoteNetworkError, _network_worker


class TimedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.started = time.monotonic()

    def send(self, message):
        self.connection.send((*message, time.monotonic() - self.started))

    def close(self):
        self.connection.close()


def timed_network_worker(connection, url, headers, timeout, read_limit):
    _network_worker(TimedConnection(connection), url, headers, timeout, read_limit)


def measured_transport(request, timeout, *, worker=timed_network_worker):
    """Same isolation/read cap/hard deadline as pilot, with safe numeric timings."""
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    read_limit = min(MAX_BYTES + 1, getattr(request, 'bblove_read_limit', MAX_BYTES + 1))
    if not 1 <= read_limit <= MAX_BYTES + 1:
        raise PilotBlocked('RESPONSE_READ_LIMIT_INVALID')
    process = context.Process(target=worker, args=(writer, request.full_url,
                              dict(request.header_items()), timeout, read_limit))
    start = time.monotonic()
    message = None
    try:
        process.start()
        writer.close()
        if not reader.poll(max(0, timeout - (time.monotonic() - start))):
            raise RemoteNetworkError('DeadlineExceeded', 'AbsoluteDeadline')
        try:
            message = reader.recv()
        except EOFError:
            raise RemoteNetworkError('WorkerError', 'NoResponse') from None
    finally:
        reader.close()
        writer.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
    elapsed = time.monotonic() - start
    timing = {'transport_seconds': elapsed, 'network_seconds': message[-1],
              'process_overhead_seconds': max(0, elapsed - message[-1])}
    if message[0] == 'HTTP':
        error = urllib.error.HTTPError('', message[1], 'HTTP_ERROR', {}, None)
        error.bblove_timing = timing
        raise error
    if message[0] == 'NETWORK':
        error = RemoteNetworkError(message[1], message[2])
        error.bblove_timing = timing
        raise error
    response = io.BytesIO(message[3])
    response.status = message[1]
    response.headers = {'Content-Type': message[2]}
    response.bblove_timing = timing
    return response
