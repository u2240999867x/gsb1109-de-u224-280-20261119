import asyncio
import concurrent.futures
from glob import glob
import json
import queue
import os
import time
from inspect import isawaitable
from typing import Any, NamedTuple

from tornado import gen
import weakref

from .core import Stream, convert_interval, RefCounter, sync


def sink_to_file(filename, upstream, mode='w', prefix='', suffix='\n', flush=False):
    file = open(filename, mode=mode)

    def write(text):
        file.write(prefix + text + suffix)
        if flush:
            file.flush()

    upstream.sink(write)
    return file


class Source(Stream):
    """Start node for a set of Streams

    Source nodes emit data into other nodes. They typically get this data
    by polling external sources, and are necessarily run by an event loop.

    Parameters
    ----------
    start: bool
        Whether to call the run method immediately. If False, nothing
        will happen until ``source.start()`` is called.
    """
    _graphviz_shape = 'doubleoctagon'

    def __init__(self, start=False, **kwargs):
        self.stopped = True
        super().__init__(ensure_io_loop=True, **kwargs)
        self.started = False
        if start:
            self.start()

    def stop(self):
        """set self.stopped, which will cause polling to stop after next run"""
        if not self.stopped:
            self.stopped = True

    def start(self):
        """start polling

        If already running, this has no effect. If the source was started and then
        stopped again, this will restart the ``self.run`` coroutine.
        """
        if self.stopped:
            self.stopped = False
            self.started = True
            self.loop.add_callback(self.run)

    async def run(self):
        """This coroutine will be invoked by start() and emit all data

        You might either overrive ``_run()`` when all logic can be contained
        there, or override this method directly.

        Note the use of ``.stopped`` to halt the coroutine, whether or not

        """
        while not self.stopped:
            await self._run()

    async def _run(self):
        """This is the functionality to run on each cycle

        Typically this may be used for polling some external IO source
        or time-based data emission. You might choose to include an
        ``await asyncio.sleep()`` for the latter.
        """
        raise NotImplementedError


@Stream.register_api(staticmethod)
class from_periodic(Source):
    """Generate data from a function on given period

    cf ``streamz.dataframe.PeriodicDataFrame``

    Parameters
    ----------
    callback: callable
        Function to call on each iteration. Takes no arguments.
    poll_interval: float
        Time to sleep between calls (s)
    """

    def __init__(self, callback, poll_interval=0.1, **kwargs):
        self._cb = callback
        self._poll = poll_interval
        super().__init__(**kwargs)

    async def _run(self):
        await asyncio.gather(*self._emit(self._cb()))
        await asyncio.sleep(self._poll)


def PeriodicCallback(callback, callback_time, asynchronous=False, **kwargs):  # pragma: no cover
    """For backward compatibility - please use Stream.from_periodic"""
    if kwargs:
        callback = lambda: callback(**kwargs)
    return Stream.from_periodic(callback, callback_time, asynchronous=asynchronous)


@Stream.register_api(staticmethod)
class from_textfile(Source):
    """ Stream data from a text file

    Parameters
    ----------
    f: file or string
        Source of the data. If string, will be opened.
    poll_interval: Number
        Interval to poll file for new data in seconds
    delimiter: str
        Character(s) to use to split the data into parts
    start: bool
        Whether to start running immediately; otherwise call stream.start()
        explicitly.
    from_end: bool
        Whether to begin streaming from the end of the file (i.e., only emit
        lines appended after the stream starts).

    Examples
    --------
    >>> source = Stream.from_textfile('myfile.json')  # doctest: +SKIP
    >>> source.map(json.loads).pluck('value').sum().sink(print)  # doctest: +SKIP
    >>> source.start()  # doctest: +SKIP

    Returns
    -------
    Stream
    """
    def __init__(self, f, poll_interval=0.100, delimiter='\n',
                 from_end=False, **kwargs):
        if isinstance(f, str):
            f = open(f)
        self.buffer = ''
        self.file = f
        self.from_end = from_end
        if self.from_end:
            # this only happens when we are ready to read
            self.file.seek(0, 2)
        self.delimiter = delimiter

        self.poll_interval = poll_interval
        super().__init__(**kwargs)

    async def _run(self):
        line = self.file.read()
        if line:
            self.buffer = self.buffer + line
            if self.delimiter in self.buffer:
                parts = self.buffer.split(self.delimiter)
                self.buffer = parts.pop(-1)
                for part in parts:
                    await asyncio.gather(*self._emit(part + self.delimiter))
        else:
            await asyncio.sleep(self.poll_interval)


@Stream.register_api(staticmethod)
class filenames(Source):
    """ Stream over filenames in a directory

    Parameters
    ----------
    path: string
        Directory path or globstring over which to search for files
    poll_interval: Number
        Seconds between checking path
    start: bool (False)
        Whether to start running immediately; otherwise call stream.start()
        explicitly.

    Examples
    --------
    >>> source = Stream.filenames('path/to/dir')  # doctest: +SKIP
    >>> source = Stream.filenames('path/to/*.csv', poll_interval=0.500)  # doctest: +SKIP
    """
    def __init__(self, path, poll_interval=0.100, **kwargs):
        if '*' not in path:
            if os.path.isdir(path):
                if not path.endswith(os.path.sep):
                    path = path + '/'
                path = path + '*'
        self.path = path
        self.seen = set()
        self.poll_interval = poll_interval
        super().__init__(**kwargs)

    async def _run(self):
        filenames = set(glob(self.path))
        new = filenames - self.seen
        for fn in sorted(new):
            self.seen.add(fn)
            await asyncio.gather(*self._emit(fn))
        await asyncio.sleep(self.poll_interval)  # TODO: remove poll if delayed


@Stream.register_api(staticmethod)
class from_tcp(Source):
    """
    Creates events by reading from a socket using tornado TCPServer

    The stream of incoming bytes is split on a given delimiter, and the parts
    become the emitted events.

    Parameters
    ----------
    port : int
        The port to open and listen on. It only gets opened when the source
        is started, and closed upon ``stop()``
    delimiter : bytes
        The incoming data will be split on this value. The resulting events
        will still have the delimiter at the end.
    start : bool
        Whether to immediately initiate the source. You probably want to
        set up downstream nodes first.
    server_kwargs : dict or None
        If given, additional arguments to pass to TCPServer

    Examples
    --------

    >>> source = Source.from_tcp(4567)  # doctest: +SKIP
    """
    def __init__(self, port, delimiter=b'\n', server_kwargs=None, **kwargs):
        self.server_kwargs = server_kwargs or {}
        self.port = port
        self.server = None
        self.delimiter = delimiter
        super().__init__(**kwargs)

    def run(self):
        from tornado.tcpserver import TCPServer
        from tornado.iostream import StreamClosedError

        class EmitServer(TCPServer):
            source = self

            async def handle_stream(self, stream, address):
                while not self.source.stopped:
                    try:
                        data = await stream.read_until(self.source.delimiter)
                        result = self.source._emit(data)
                        if isawaitable(result):
                            await result
                    except StreamClosedError:
                        break

        self.server = EmitServer(**self.server_kwargs)
        self.server.listen(self.port)

    def stop(self):
        if not self.stopped:
            self.server.stop()
            self.server = None
            self.stopped = True


@Stream.register_api(staticmethod)
class from_http_server(Source):
    """Listen for HTTP POSTs on given port

    Each connection will emit one event, containing the body data of
    the request

    Parameters
    ----------
    port : int
        The port to listen on
    path : str
        Specific path to listen on. Can be regex, but content is not used.
    start : bool
        Whether to immediately startup the server. Usually you want to connect
        downstream nodes first, and then call ``.start()``.
    server_kwargs : dict or None
        If given, set of further parameters to pass on to HTTPServer

    Examples
    --------

    >>> source = Source.from_http_server(4567)  # doctest: +SKIP

    """

    def __init__(self, port, path='/.*', server_kwargs=None, **kwargs):
        self.port = port
        self.path = path
        self.server_kwargs = server_kwargs or {}
        self.server = None
        super().__init__(**kwargs)

    def run(self):
        from tornado.web import Application, RequestHandler
        from tornado.httpserver import HTTPServer

        class Handler(RequestHandler):
            source = self

            async def post(self):
                await asyncio.gather(*self.source._emit(self.request.body))
                self.write('OK')

        application = Application([
            (self.path, Handler),
        ])
        server = HTTPServer(application, **self.server_kwargs)
        server.listen(self.port)
        self.server = server

    def stop(self):
        """Shutdown HTTP server"""
        if not self.stopped:
            self.server.stop()
            self.server = None
            self.stopped = True


class ProcessRecord(NamedTuple):
    """One JSON-lines data record emitted by ``from_process(json_lines=True)``

    Attributes
    ----------
    run_id: int
        Identifies which process start ("run") produced this record. It is
        incremented every time the source is (re)started, so records from
        different device handovers can always be told apart, even if the
        command is identical and the device restarts its own sequence
        numbers from 1.
    data: dict
        The parsed JSON object, decoded from one newline-terminated line
        of the process' STDOUT.
    """
    run_id: int
    data: Any


class ProcessRun:
    """Lifecycle state and diagnostics for one start of a ``from_process``
    source in JSON-lines mode

    A new instance is created on every ``start()`` and appended to
    ``source.runs``; the most recent one is ``source.current_run``. All
    attributes belong exclusively to this run: a late-finishing previous
    run never touches the state of a later one.

    Attributes
    ----------
    run_id: int
        1-based identifier of this run, matching ``ProcessRecord.run_id``.
    cmd: list of str
        The exact command line used for this start.
    pid: int or None
        PID of the child process, once spawned.
    process: asyncio.subprocess.Process or None
        The child process object of this run.
    exit_code: int or None
        The process exit code, once it has exited and been reaped.
        Negative values indicate termination by a signal.
    reason: str or None
        Why this run ended: ``'exited'`` if the process finished on its
        own, ``'stopped'`` if it was killed by a manual ``stop()``.
    stop_requested: bool
        Whether ``stop()`` was requested while this was the current run.
    records_emitted: int
        Number of complete JSON-object records emitted downstream.
    partial_line: bytes
        Trailing STDOUT bytes not terminated by a newline when the stream
        ended. Kept as diagnostics only: never emitted as data and never
        concatenated with the output of the next run.
    discarded_lines: list of bytes
        Complete lines that were not valid JSON objects. Diagnostics only.
    exited: bool
        True once the child process has exited and been reaped. Note that
        complete records may still be in flight downstream at this point;
        compare ``sinks_done``/``done``.
    output_done: bool
        True once STDOUT reached end-of-stream and was fully consumed.
    sinks_done: bool
        True once the asynchronous downstream sinks of every complete
        record of this run have returned.
    done: bool
        True once this run is fully complete: the process was reaped, all
        output was collected and all of this run's record sinks returned.
    completion: concurrent.futures.Future
        Resolved exactly once, with this ``ProcessRun``, when ``done``
        becomes true. Never borrows state from a later run.
    stderr: bytes
        The raw STDERR of this run, captured independently of the data
        stream (property).
    stderr_tail: bytes
        The (truncated) tail of STDERR, at most ``stderr_tail_bytes``
        long (property).
    stderr_truncated: bool
        True if STDERR is longer than the kept tail (property).
    """

    def __init__(self, run_id, cmd, stderr_tail_bytes=4096):
        self.run_id = run_id
        self.cmd = list(cmd)
        self.pid = None
        self.process = None
        self.exit_code = None
        self.reason = None
        self.stop_requested = False
        self.records_emitted = 0
        self.partial_line = b''
        self.discarded_lines = []
        self.exited = False
        self.output_done = False
        self.sinks_done = False
        self.done = False
        self.completion = concurrent.futures.Future()
        self._stderr = bytearray()
        self._stderr_tail_bytes = stderr_tail_bytes
        self._signalled = False
        self._pump_task = None
        self._stderr_task = None
        self._supervisor_task = None

    @property
    def returncode(self):
        """Alias for ``exit_code``"""
        return self.exit_code

    @property
    def stderr(self):
        return bytes(self._stderr)

    @property
    def stderr_tail(self):
        tail = self._stderr_tail_bytes
        if tail and len(self._stderr) > tail:
            return bytes(self._stderr[-tail:])
        return bytes(self._stderr)

    @property
    def stderr_truncated(self):
        return bool(self._stderr_tail_bytes) and \
            len(self._stderr) > self._stderr_tail_bytes

    def __repr__(self):
        return ('<ProcessRun run_id={} exit_code={} reason={} done={}>'
                .format(self.run_id, self.exit_code, self.reason, self.done))


@Stream.register_api(staticmethod)
class from_process(Source):
    """Messages from a running external process

    This doesn't work on Windows

    Parameters
    ----------
    cmd : list of str or str
        Command to run: program name, followed by arguments
    open_kwargs : dict
        To pass on the the process open function, see ``subprocess.Popen``.
    with_stderr : bool
        Whether to include the process STDERR in the stream. Only applies
        when ``json_lines`` is False; in JSON-lines mode STDERR is always
        captured separately per run (see ``ProcessRun.stderr``) and never
        mixed into the data.
    with_end : bool
        Whether to emit a final trailing frame that is not terminated by
        a newline. Only applies when ``json_lines`` is False; in JSON-lines
        mode an unterminated tail is kept as diagnostics only
        (see ``ProcessRun.partial_line``).
    json_lines : bool
        If True, enable JSON-lines mode: only complete, newline-terminated
        JSON objects from STDOUT are emitted downstream, as
        ``ProcessRecord(run_id, data)`` namedtuples. Each ``start()``
        begins a new run (see ``ProcessRun``) with its own process,
        readers, diagnostics and completion tracking, so devices can be
        swapped (stop, change ``cmd``, start again) without tearing down
        downstream nodes and without mixing up records or diagnostics
        between runs.
    stop_timeout : float
        Seconds to wait for a graceful termination after ``stop()`` before
        forcefully killing a process that ignores the terminate signal.
        Only used in JSON-lines mode.
    stderr_tail_bytes : int
        Maximum size of the truncated STDERR tail kept per run
        (``ProcessRun.stderr_tail``). Only used in JSON-lines mode.
    start : bool
        Whether to immediately startup the process. Usually you want to connect
        downstream nodes first, and then call ``.start()``.

    Attributes
    ----------
    runs : list of ProcessRun
        One entry per ``start()``, in order (JSON-lines mode only).
    current_run : ProcessRun or None
        The most recently started run (JSON-lines mode only).

    Example
    -------
    >>> source = Source.from_process(['ping', 'localhost'])  # doctest: +SKIP
    """

    def __init__(self, cmd, open_kwargs=None, with_stderr=False, with_end=True,
                 json_lines=False, stop_timeout=1.0, stderr_tail_bytes=4096,
                 **kwargs):
        self.cmd = cmd
        self.open_kwargs = open_kwargs or {}
        self.with_stderr = with_stderr
        self.with_end = with_end
        self.json_lines = json_lines
        self.stop_timeout = stop_timeout
        self.stderr_tail_bytes = stderr_tail_bytes
        self.process = None
        self.runs = []
        self.current_run = None
        super().__init__(**kwargs)

    def _argv(self):
        import shlex
        if isinstance(self.cmd, (list, tuple)):
            return list(self.cmd)
        return shlex.split(self.cmd)

    def start(self):
        if not self.json_lines:
            return super().start()
        if self.stopped:
            self.stopped = False
            self.started = True
            run = ProcessRun(run_id=len(self.runs) + 1, cmd=self._argv(),
                             stderr_tail_bytes=self.stderr_tail_bytes)
            self.runs.append(run)
            self.current_run = run
            self.loop.add_callback(self._launch_run, run)

    def stop(self):
        if not self.json_lines:
            return super().stop()
        if not self.stopped:
            self.stopped = True
        run = self.current_run
        if run is not None and not run.stop_requested and not run.done:
            run.stop_requested = True
            self.loop.add_callback(self._begin_stop, run)

    async def run(self):
        import subprocess
        if self.json_lines:
            # JSON-lines runs are launched and supervised independently of
            # this coroutine; see start()/_launch_run().
            return
        stderr = subprocess.STDOUT if self.with_stderr else None
        process = await asyncio.create_subprocess_exec(
            *self._argv(), stdout=subprocess.PIPE,
            stderr=stderr, **self.open_kwargs)
        while not self.stopped:
            try:
                out = await process.stdout.readuntil(b'\n')
            except asyncio.IncompleteReadError as err:
                if self.with_end and err.partial:
                    out = err.partial
                else:
                    break
            if process.returncode is not None:
                self.stopped = True
            await asyncio.gather(*self._emit(out))
        if process.returncode is not None:
            process.terminate()
            await process.wait()

    async def _launch_run(self, run):
        """Spawn the child process and its dedicated supervisor/reader tasks

        Each run gets exactly one stdout pump, one stderr drain and one
        supervisor, so repeated handovers leave neither child processes nor
        old readers behind.
        """
        import subprocess
        try:
            process = await asyncio.create_subprocess_exec(
                *run.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **self.open_kwargs)
        except Exception as exc:
            run.exited = True
            run.output_done = True
            run.sinks_done = True
            run.done = True
            if not run.completion.done():
                run.completion.set_exception(exc)
            return
        run.process = process
        run.pid = process.pid
        self.process = process
        run._pump_task = asyncio.ensure_future(self._pump_stdout(run))
        run._stderr_task = asyncio.ensure_future(self._drain_stderr(run))
        run._supervisor_task = asyncio.ensure_future(self._supervise(run))
        if run.stop_requested:
            self._begin_stop(run)

    async def _pump_stdout(self, run):
        """Emit complete JSON-object lines of one run, in order

        Backpressure is per run: awaiting a slow downstream sink here never
        blocks other runs, and this run's records keep their original order.
        """
        stdout = run.process.stdout
        while True:
            line = await stdout.readline()
            if not line:
                break
            if line.endswith(b'\n'):
                await self._emit_json_line(run, line)
            else:
                # Unterminated trailing data at end of stream: diagnostics
                # only, never emitted and never carried into the next run.
                run.partial_line += line
        run.output_done = True
        run.sinks_done = True

    async def _emit_json_line(self, run, line):
        try:
            obj = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            obj = None
        if not isinstance(obj, dict):
            run.discarded_lines.append(line)
            return
        run.records_emitted += 1
        record = ProcessRecord(run.run_id, obj)
        await asyncio.gather(*self._emit(
            record, metadata=[{'run_id': run.run_id, 'process_run': run}]))

    async def _drain_stderr(self, run):
        """Continuously drain STDERR of one run into its diagnostics buffer

        This is independent of downstream backpressure on the data stream,
        so a chatty process can never be blocked on a full stderr pipe
        while a sink is paused.
        """
        stderr = run.process.stderr
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                break
            run._stderr.extend(chunk)

    async def _supervise(self, run):
        """Reap the child process of one run, then finalize that run

        Reaping waits only for process exit - not for another stdout line
        and not for paused downstream sinks of older records.
        """
        process = run.process
        run.exit_code = await process.wait()
        run.exited = True
        if run.reason is None:
            if run._signalled and run.exit_code is not None \
                    and run.exit_code < 0:
                run.reason = 'stopped'
            else:
                run.reason = 'exited'
        # The process is reaped; now wait for this run's own readers to
        # finish delivering buffered output (they end at end-of-stream).
        await asyncio.gather(run._pump_task, run._stderr_task,
                             return_exceptions=True)
        self._finalize_run(run)

    def _begin_stop(self, run):
        """Ask one run's process to terminate, escalating to a kill"""
        process = run.process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            run._signalled = True
        except ProcessLookupError:
            return
        self.loop.call_later(self.stop_timeout, self._kill_run, run)

    def _kill_run(self, run):
        process = run.process
        if process is None or process.returncode is not None:
            return
        try:
            process.kill()
            run._signalled = True
        except ProcessLookupError:
            pass

    def _finalize_run(self, run):
        """Mark one run complete, exactly once, from its own state only"""
        if run.done:
            return
        if not (run.exited and run.output_done and run.sinks_done):
            return
        run.done = True
        if not run.completion.done():
            run.completion.set_result(run)


@Stream.register_api(staticmethod)
class from_kafka(Source):
    """ Accepts messages from Kafka

    Uses the confluent-kafka library,
    https://docs.confluent.io/current/clients/confluent-kafka-python/


    Parameters
    ----------
    topics: list of str
        Labels of Kafka topics to consume from
    consumer_params: dict
        Settings to set up the stream, see
        https://docs.confluent.io/current/clients/confluent-kafka-python/#configuration
        https://github.com/edenhill/librdkafka/blob/master/CONFIGURATION.md
        Examples:
        bootstrap.servers, Connection string(s) (host:port) by which to reach
        Kafka;
        group.id, Identity of the consumer. If multiple sources share the same
        group, each message will be passed to only one of them.
    poll_interval: number
        Seconds that elapse between polling Kafka for new messages
    start: bool (False)
        Whether to start polling upon instantiation

    Examples
    --------

    >>> source = Stream.from_kafka(['mytopic'],
    ...           {'bootstrap.servers': 'localhost:9092',
    ...            'group.id': 'streamz'})  # doctest: +SKIP

    """
    def __init__(self, topics, consumer_params, poll_interval=0.1, **kwargs):
        self.cpars = consumer_params
        self.consumer = None
        self.topics = topics
        self.poll_interval = poll_interval
        super().__init__(**kwargs)

    def do_poll(self):
        if self.consumer is not None:
            msg = self.consumer.poll(0)
            if msg and msg.value() and msg.error() is None:
                return msg.value()

    @gen.coroutine
    def poll_kafka(self):
        while True:
            val = self.do_poll()
            if val:
                yield self._emit(val)
            else:
                yield gen.sleep(self.poll_interval)
            if self.stopped:
                break
        self._close_consumer()

    def start(self):
        import confluent_kafka as ck
        if self.stopped:
            self.stopped = False
            self.consumer = ck.Consumer(self.cpars)
            self.consumer.subscribe(self.topics)
            weakref.finalize(
                self, lambda consumer=self.consumer: _close_consumer(consumer)
            )
            tp = ck.TopicPartition(self.topics[0], 0, 0)

            # blocks for consumer thread to come up and invoke poll to
            # establish connection with broker to fetch oauth token for kafka
            self.consumer.poll(timeout=1)
            self.consumer.get_watermark_offsets(tp)
            self.loop.add_callback(self.poll_kafka)

    def _close_consumer(self):
        if self.consumer is not None:
            consumer = self.consumer
            self.consumer = None
            consumer.unsubscribe()
            consumer.close()
        self.stopped = True


def _close_consumer(consumer):
    try:
        consumer.close()
    except RuntimeError:
        pass


class FromKafkaBatched(Source):
    """Base class for both local and cluster-based batched kafka processing"""
    def __init__(self, topic, consumer_params, poll_interval='1s',
                 npartitions=None, refresh_partitions=False,
                 max_batch_size=10000, keys=False,
                 engine=None, **kwargs):
        self.consumer_params = consumer_params
        # Override the auto-commit config to enforce custom streamz
        # checkpointing
        self.consumer_params['enable.auto.commit'] = 'false'
        if 'auto.offset.reset' not in self.consumer_params.keys():
            consumer_params['auto.offset.reset'] = 'latest'
        self.topic = topic
        self.npartitions = npartitions
        self.refresh_partitions = refresh_partitions
        if self.npartitions is not None and self.npartitions <= 0:
            raise ValueError("Number of Kafka topic partitions must be > 0.")
        self.poll_interval = convert_interval(poll_interval)
        self.max_batch_size = max_batch_size
        self.keys = keys
        self.engine = engine
        self.started = False

        super().__init__(**kwargs)

    @gen.coroutine
    def poll_kafka(self):
        import confluent_kafka as ck

        def commit(_part):
            topic, part_no, _, _, offset = _part[1:]
            _tp = ck.TopicPartition(topic, part_no, offset + 1)
            self.consumer.commit(offsets=[_tp], asynchronous=True)

        @gen.coroutine
        def checkpoint_emit(_part):
            ref = RefCounter(cb=lambda: commit(_part), loop=self.loop)
            yield self._emit(_part, metadata=[{'ref': ref}])

        if self.npartitions is None:
            kafka_cluster_metadata = self.consumer.list_topics(self.topic)
            if self.engine == "cudf":  # pragma: no cover
                self.npartitions = len(kafka_cluster_metadata[self.topic.encode('utf-8')])
            else:
                self.npartitions = len(kafka_cluster_metadata.topics[self.topic].partitions)
        self.positions = [0] * self.npartitions

        tps = []
        for partition in range(self.npartitions):
            tps.append(ck.TopicPartition(self.topic, partition))

        while True:
            try:
                committed = self.consumer.committed(tps, timeout=1)
            except ck.KafkaException:
                pass
            else:
                for tp in committed:
                    self.positions[tp.partition] = tp.offset
                break

        while not self.stopped:
            out = []

            if self.refresh_partitions:
                kafka_cluster_metadata = self.consumer.list_topics(self.topic)
                if self.engine == "cudf":  # pragma: no cover
                    new_partitions = len(kafka_cluster_metadata[self.topic.encode('utf-8')])
                else:
                    new_partitions = len(kafka_cluster_metadata.topics[self.topic].partitions)
                if new_partitions > self.npartitions:
                    self.positions.extend([-1001] * (new_partitions - self.npartitions))
                    self.npartitions = new_partitions

            for partition in range(self.npartitions):
                tp = ck.TopicPartition(self.topic, partition, 0)
                try:
                    low, high = self.consumer.get_watermark_offsets(
                        tp, timeout=0.1)
                except (RuntimeError, ck.KafkaException):
                    continue
                self.started = True
                if 'auto.offset.reset' in self.consumer_params.keys():
                    if self.consumer_params['auto.offset.reset'] == 'latest' and \
                            self.positions[partition] == -1001:
                        self.positions[partition] = high
                current_position = self.positions[partition]
                lowest = max(current_position, low)
                if high > lowest + self.max_batch_size:
                    high = lowest + self.max_batch_size
                if high > lowest:
                    out.append((self.consumer_params, self.topic, partition,
                                self.keys, lowest, high - 1))
                    self.positions[partition] = high
            self.consumer_params['auto.offset.reset'] = 'earliest'

            for part in out:
                yield self.loop.add_callback(checkpoint_emit, part)

            else:
                yield gen.sleep(self.poll_interval)

    def start(self):
        import confluent_kafka as ck
        if self.engine == "cudf":  # pragma: no cover
            from custreamz import kafka

        if self.stopped:
            if self.engine == "cudf":  # pragma: no cover
                self.consumer = kafka.Consumer(self.consumer_params)
            else:
                self.consumer = ck.Consumer(self.consumer_params)
            weakref.finalize(self, lambda consumer=self.consumer: _close_consumer(consumer))
            self.stopped = False
            tp = ck.TopicPartition(self.topic, 0, 0)

            # blocks for consumer thread to come up and invoke poll to establish
            # connection with broker to fetch oauth token for kafka
            self.consumer.poll(timeout=1)
            self.consumer.get_watermark_offsets(tp)
            self.loop.add_callback(self.poll_kafka)


@Stream.register_api(staticmethod)
def from_kafka_batched(topic, consumer_params, poll_interval='1s',
                       npartitions=None, refresh_partitions=False,
                       start=False, dask=False,
                       max_batch_size=10000, keys=False,
                       engine=None, **kwargs):
    """ Get messages and keys (optional) from Kafka in batches

    Uses the confluent-kafka library,
    https://docs.confluent.io/current/clients/confluent-kafka-python/

    This source will emit lists of messages for each partition of a single given
    topic per time interval, if there is new data. If using dask, one future
    will be produced per partition per time-step, if there is data.

    Checkpointing is achieved through the use of reference counting. A reference
    counter is emitted downstream for each batch of data. A callback is
    triggered when the reference count reaches zero and the offsets are
    committed back to Kafka. Upon the start of this function, the previously
    committed offsets will be fetched from Kafka and begin reading form there.
    This will guarantee at-least-once semantics.

    Parameters
    ----------
    topic: str
        Kafka topic to consume from
    consumer_params: dict
        | Settings to set up the stream, see
        | https://docs.confluent.io/current/clients/confluent-kafka-python/#configuration
        | https://github.com/edenhill/librdkafka/blob/master/CONFIGURATION.md
        | Examples:
        | bootstrap.servers: Connection string(s) (host:port) by which to reach Kafka
        | group.id: Identity of the consumer. If multiple sources share the same
        | group, each message will be passed to only one of them.
    poll_interval: number
        Seconds that elapse between polling Kafka for new messages
    npartitions: int (None)
        | Number of partitions in the topic.
        | If None, streamz will poll Kafka to get the number of partitions.
     refresh_partitions: bool (False)
        | Useful if the user expects to increase the number of topic partitions on the
        | fly, maybe to handle spikes in load. Streamz polls Kafka in every batch to
        | determine the current number of partitions. If partitions have been added,
        | streamz will automatically start reading data from the new partitions as well.
        | If set to False, streamz will not accommodate adding partitions on the fly.
        | It is recommended to restart the stream after decreasing the number of partitions.
    start: bool (False)
        Whether to start polling upon instantiation
    max_batch_size: int
        The maximum number of messages per partition to be consumed per batch
    keys: bool (False)
        | Whether to extract keys along with the messages.
        | If True, this will yield each message as a dict:
        | {'key':msg.key(), 'value':msg.value()}
    engine: str (None)
        | If engine is set to "cudf", streamz reads data (messages must be JSON)
        | from Kafka in an accelerated manner directly into cuDF (GPU) dataframes.
        | This is done using the RAPIDS custreamz library.

        | Please refer to RAPIDS cudf API here:
        | https://docs.rapids.ai/api/cudf/stable/

        | Folks interested in trying out custreamz would benefit from this
        | accelerated Kafka reader. If one does not want to use GPUs, they
        | can use streamz as is, with the default engine=None.

        | To use this option, one must install custreamz (use the
        | appropriate CUDA version recipe & Python version)
        | using a command like the one below, which will install all
        | GPU dependencies and streamz itself:

        | conda install -c rapidsai-nightly -c nvidia -c conda-forge \
        | -c defaults custreamz=0.15 python=3.7 cudatoolkit=10.2

        | More information at: https://rapids.ai/start.html

    Important Kafka Configurations
    By default, a stream will start reading from the latest offsets
    available. Please set 'auto.offset.reset': 'earliest' in the
    consumer configs, if the stream needs to start processing from
    the earliest offsets.

    Examples
    ----------

    >>> source = Stream.from_kafka_batched('mytopic',
    ...           {'bootstrap.servers': 'localhost:9092',
    ...            'group.id': 'streamz'})  # doctest: +SKIP

    """
    if dask:
        from distributed.client import default_client
        kwargs['loop'] = default_client().loop
    source = FromKafkaBatched(topic, consumer_params,
                              poll_interval=poll_interval,
                              npartitions=npartitions,
                              refresh_partitions=refresh_partitions,
                              max_batch_size=max_batch_size,
                              keys=keys,
                              engine=engine,
                              **kwargs)
    if dask:
        source = source.scatter()

    if start:
        source.start()

    if engine == "cudf": # pragma: no cover
        return source.starmap(get_message_batch_cudf)
    else:
        return source.starmap(get_message_batch)


def get_message_batch(kafka_params, topic, partition, keys, low, high, timeout=None):
    """Fetch a batch of kafka messages (keys & values) in given topic/partition

    This will block until messages are available, or timeout is reached.
    """
    import confluent_kafka as ck
    t0 = time.time()
    consumer = ck.Consumer(kafka_params)
    tp = ck.TopicPartition(topic, partition, low)
    consumer.assign([tp])
    out = []
    try:
        while True:
            msg = consumer.poll(0)
            if msg and msg.value() and msg.error() is None:
                if high >= msg.offset():
                    if keys:
                        out.append({'key':msg.key(), 'value':msg.value()})
                    else:
                        out.append(msg.value())
                if high <= msg.offset():
                    break
            else:
                time.sleep(0.1)
                if timeout is not None and time.time() - t0 > timeout:
                    break
    finally:
        consumer.close()
    return out


def get_message_batch_cudf(kafka_params, topic, partition, keys, low, high, timeout=None): # pragma: no cover
    """
    Fetch a batch of kafka messages (currently, messages must be in JSON format)
    in given topic/partition as a cudf dataframe
    """
    from custreamz import kafka
    consumer = kafka.Consumer(kafka_params)
    gdf = None
    try:
        gdf = consumer.read_gdf(topic=topic, partition=partition, lines=True, start=low, end=high + 1)
    finally:
        consumer.close()
    return gdf


@Stream.register_api(staticmethod)
class from_iterable(Source):
    """ Emits items from an iterable.

    Parameters
    ----------
    iterable: iterable
        An iterable to emit messages from.

    Examples
    --------

    >>> source = Stream.from_iterable(range(3))
    >>> L = source.sink_to_list()
    >>> source.start()
    >>> L
    [0, 1, 2]
    """

    def __init__(self, iterable, **kwargs):
        self._iterable = iterable
        super().__init__(**kwargs)

    async def run(self):
        for x in self._iterable:
            if self.stopped:
                break
            await asyncio.gather(*self._emit(x))
            if self.stopped:
                break
        self.stopped = True


@Stream.register_api()
class from_websocket(Source):
    """Read binary data from a websocket

    This source will accept connections on a given port and handle messages
    coming in.

    The websockets library must be installed.

    :param host: str
        Typically "localhost"
    :param port: int
        Which port to listen on (must be available)
    :param serve_kwargs: dict
        Passed to ``websockets.serve``
    :param kwargs:
        Passed to superclass
    """

    def __init__(self, host, port, serve_kwargs=None, **kwargs):
        self.host = host
        self.port = port
        self.s_kw = serve_kwargs
        self.server = None
        super().__init__(**kwargs)

    @gen.coroutine
    def _read(self, ws, path):
        while not self.stopped:
            data = yield ws.recv()
            yield self._emit(data)

    async def run(self):
        import websockets
        self.server = await websockets.serve(
            self._read, self.host, self.port, **(self.s_kw or {})
        )

    def stop(self):
        self.server.close()
        sync(self.loop, self.server.wait_closed)


@Stream.register_api()
class from_q(Source):
    """Source events from a threading.Queue, running another event framework

    The queue is polled, i.e., there is a latency/overhead tradeoff, since
    we cannot use ``await`` directly with a multithreaded queue.

    Allows mixing of another event loop, for example pyqt, on another thread.
    Note that, by default, a streamz.Source such as this one will start
    an event loop in a new thread, unless otherwise specified.
    """

    def __init__(self, q, sleep_time=0.01, **kwargs):
        """
        :param q: threading.Queue
            Any items pushed into here will become streamz events
        :param sleep_time: int
            Sets how long we wait before checking the input queue when
            empty (in s)
        :param kwargs:
            passed to streamz.Source
        """
        self.q = q
        self.sleep = sleep_time
        super().__init__(**kwargs)

    async def _run(self):
        """Poll threading queue for events
        This uses check-and-wait, but overhead is low. Could maybe have
        a sleep-free version with an threading.Event.
        """
        try:
            out = self.q.get_nowait()
            await self.emit(out, asynchronous=True)
        except queue.Empty:
            await asyncio.sleep(self.sleep)


@Stream.register_api()
class from_mqtt(from_q):
    """Read from MQTT source

    See https://en.wikipedia.org/wiki/MQTT for a description of the protocol
    and its uses.

    See also ``sinks.to_mqtt``.

    Requires ``paho.mqtt``

    The outputs are ``paho.mqtt.client.MQTTMessage`` instances, which each have
    attributes timestamp, payload, topic, ...

    NB: paho.mqtt.python runs on its own thread in this implementation. We may
    wish to instead call client.loop() directly

    :param host: str
    :param port: int
    :param topic: str
        (May in the future support a list of topics)
    :param keepalive: int
        See mqtt docs - to keep the channel alive
    :param client_kwargs:
        Passed to the client's ``connect()`` method
    """
    def __init__(self, host, port, topic, keepalive=60 , client_kwargs=None,
                 user=None, pw=None, **kwargs):
        self.host = host
        self.port = port
        self.keepalive = keepalive
        self.topic = topic
        self.client_kwargs = client_kwargs
        self.user = user
        self.pw = pw
        super().__init__(q=queue.Queue(), **kwargs)

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(self.topic)

    def _on_message(self, client, userdata, msg):
        self.q.put(msg)

    async def run(self):
        import paho.mqtt.client as mqtt
        client = mqtt.Client()
        if self.user:
            client.username_pw_set(self.user, self.pw)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(self.host, self.port, self.keepalive, **(self.client_kwargs or {}))
        client.loop_start()
        await super().run()
        client.disconnect()
