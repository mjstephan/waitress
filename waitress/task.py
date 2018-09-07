##############################################################################
#
# Copyright (c) 2001, 2002 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################

import socket
import sys
import threading
import time

from waitress.buffers import ReadOnlyFileBasedBuffer

from waitress.compat import (
    tobytes,
    Queue,
    Empty,
    reraise,
)

from waitress.utilities import (
    build_http_date,
    logger,
    queue_logger,
)

rename_headers = {  # or keep them without the HTTP_ prefix added
    'CONTENT_LENGTH': 'CONTENT_LENGTH',
    'CONTENT_TYPE': 'CONTENT_TYPE',
}

hop_by_hop = frozenset((
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade'
))

class JustTesting(Exception):
    pass

class ThreadedTaskDispatcher(object):
    """A Task Dispatcher that creates a thread for each task.
    """
    stop_count = 0 # Number of threads that will stop soon.
    logger = logger
    queue_logger = queue_logger

    def __init__(self):
        self.threads = {} # { thread number -> 1 }
        self.queue = Queue()
        self.thread_mgmt_lock = threading.Lock()

    def start_new_thread(self, target, args):
        t = threading.Thread(target=target, name='waitress', args=args)
        t.daemon = True
        t.start()

    def handler_thread(self, thread_no):
        threads = self.threads
        try:
            while threads.get(thread_no):
                task = self.queue.get()
                if task is None:
                    # Special value: kill this thread.
                    break
                try:
                    task.service()
                except Exception as e:
                    self.logger.exception(
                        'Exception when servicing %r' % task)
                    if isinstance(e, JustTesting):
                        break
        finally:
            with self.thread_mgmt_lock:
                self.stop_count -= 1
                threads.pop(thread_no, None)

    def set_thread_count(self, count):
        with self.thread_mgmt_lock:
            threads = self.threads
            thread_no = 0
            running = len(threads) - self.stop_count
            while running < count:
                # Start threads.
                while thread_no in threads:
                    thread_no = thread_no + 1
                threads[thread_no] = 1
                running += 1
                self.start_new_thread(self.handler_thread, (thread_no,))
                thread_no = thread_no + 1
            if running > count:
                # Stop threads.
                to_stop = running - count
                self.stop_count += to_stop
                for n in range(to_stop):
                    self.queue.put(None)
                    running -= 1

    def add_task(self, task):
        queue_depth = self.queue.qsize()
        if queue_depth > 0:
            self.queue_logger.warning(
                "Task queue depth is %d" %
                queue_depth)
        try:
            task.defer()
            self.queue.put(task)
        except:
            task.cancel()
            raise

    def shutdown(self, cancel_pending=True, timeout=5):
        self.set_thread_count(0)
        # Ensure the threads shut down.
        threads = self.threads
        expiration = time.time() + timeout
        while threads:
            if time.time() >= expiration:
                self.logger.warning(
                    "%d thread(s) still running" %
                    len(threads))
                break
            time.sleep(0.1)
        if cancel_pending:
            # Cancel remaining tasks.
            try:
                queue = self.queue
                while not queue.empty():
                    task = queue.get()
                    if task is not None:
                        task.cancel()
            except Empty: # pragma: no cover
                pass
            return True
        return False

class Task(object):
    close_on_finish = False
    status = '200 OK'
    wrote_header = False
    start_time = 0
    content_length = None
    content_bytes_written = 0
    logged_write_excess = False
    logged_write_no_body = False
    logged_multi_proxy_headers = False
    complete = False
    chunked_response = False
    logger = logger

    def __init__(self, channel, request):
        self.channel = channel
        self.request = request
        self.response_headers = []
        version = request.version
        if version not in ('1.0', '1.1'):
            # fall back to a version we support.
            version = '1.0'
        self.version = version

    def service(self):
        try:
            try:
                self.start()
                self.execute()
                self.finish()
            except socket.error:
                self.close_on_finish = True
                if self.channel.adj.log_socket_errors:
                    raise
        finally:
            pass

    @property
    def has_body(self):
        return not (self.status.startswith('1') or
                    self.status.startswith('204') or
                    self.status.startswith('304')
                    )

    def cancel(self):
        self.close_on_finish = True

    def defer(self):
        pass

    def build_response_header(self):
        version = self.version
        # Figure out whether the connection should be closed.
        connection = self.request.headers.get('CONNECTION', '').lower()
        response_headers = []
        content_length_header = None
        date_header = None
        server_header = None
        connection_close_header = None

        for (headername, headerval) in self.response_headers:
            headername = '-'.join(
                [x.capitalize() for x in headername.split('-')]
            )

            if headername == 'Content-Length':
                if self.has_body:
                    content_length_header = headerval
                else:
                    continue  # pragma: no cover

            if headername == 'Date':
                date_header = headerval

            if headername == 'Server':
                server_header = headerval

            if headername == 'Connection':
                connection_close_header = headerval.lower()
            # replace with properly capitalized version
            response_headers.append((headername, headerval))

        if (
            content_length_header is None and
            self.content_length is not None and
            self.has_body
        ):
            content_length_header = str(self.content_length)
            response_headers.append(
                ('Content-Length', content_length_header)
            )

        def close_on_finish():
            if connection_close_header is None:
                response_headers.append(('Connection', 'close'))
            self.close_on_finish = True

        if version == '1.0':
            if connection == 'keep-alive':
                if not content_length_header:
                    close_on_finish()
                else:
                    response_headers.append(('Connection', 'Keep-Alive'))
            else:
                close_on_finish()

        elif version == '1.1':
            if connection == 'close':
                close_on_finish()

            if not content_length_header:
                # RFC 7230: MUST NOT send Transfer-Encoding or Content-Length
                # for any response with a status code of 1xx, 204 or 304.

                if self.has_body:
                    response_headers.append(('Transfer-Encoding', 'chunked'))
                    self.chunked_response = True

                if not self.close_on_finish:
                    close_on_finish()

            # under HTTP 1.1 keep-alive is default, no need to set the header
        else:
            raise AssertionError('neither HTTP/1.0 or HTTP/1.1')

        # Set the Server and Date field, if not yet specified. This is needed
        # if the server is used as a proxy.
        ident = self.channel.server.adj.ident

        if not server_header:
            if ident:
                response_headers.append(('Server', ident))
        else:
            response_headers.append(('Via', ident or 'waitress'))

        if not date_header:
            response_headers.append(('Date', build_http_date(self.start_time)))

        self.response_headers = response_headers

        first_line = 'HTTP/%s %s' % (self.version, self.status)
        # NB: sorting headers needs to preserve same-named-header order
        # as per RFC 2616 section 4.2; thus the key=lambda x: x[0] here;
        # rely on stable sort to keep relative position of same-named headers
        next_lines = ['%s: %s' % hv for hv in sorted(
            self.response_headers, key=lambda x: x[0])]
        lines = [first_line] + next_lines
        res = '%s\r\n\r\n' % '\r\n'.join(lines)

        return tobytes(res)

    def remove_content_length_header(self):
        response_headers = []

        for header_name, header_value in self.response_headers:
            if header_name.lower() == 'content-length':
                continue  # pragma: nocover
            response_headers.append((header_name, header_value))

        self.response_headers = response_headers

    def start(self):
        self.start_time = time.time()

    def finish(self):
        if not self.wrote_header:
            self.write(b'')
        if self.chunked_response:
            # not self.write, it will chunk it!
            self.channel.write_soon(b'0\r\n\r\n')

    def write(self, data):
        if not self.complete:
            raise RuntimeError('start_response was not called before body '
                               'written')
        channel = self.channel
        if not self.wrote_header:
            rh = self.build_response_header()
            channel.write_soon(rh)
            self.wrote_header = True

        if data and self.has_body:
            towrite = data
            cl = self.content_length
            if self.chunked_response:
                # use chunked encoding response
                towrite = tobytes(hex(len(data))[2:].upper()) + b'\r\n'
                towrite += data + b'\r\n'
            elif cl is not None:
                towrite = data[:cl - self.content_bytes_written]
                self.content_bytes_written += len(towrite)
                if towrite != data and not self.logged_write_excess:
                    self.logger.warning(
                        'application-written content exceeded the number of '
                        'bytes specified by Content-Length header (%s)' % cl)
                    self.logged_write_excess = True
            if towrite:
                channel.write_soon(towrite)
        else:
            # Cheat, and tell the application we have written all of the bytes,
            # even though the response shouldn't have a body and we are
            # ignoring it entirely.
            self.content_bytes_written += len(data)

            if not self.logged_write_no_body:
                self.logger.warning(
                    'application-written content was ignored due to HTTP '
                    'response that may not contain a message-body: (%s)' % self.status)
                self.logged_write_no_body = True


class ErrorTask(Task):
    """ An error task produces an error response
    """
    complete = True

    def execute(self):
        e = self.request.error
        body = '%s\r\n\r\n%s' % (e.reason, e.body)
        tag = '\r\n\r\n(generated by waitress)'
        body = body + tag
        self.status = '%s %s' % (e.code, e.reason)
        cl = len(body)
        self.content_length = cl
        self.response_headers.append(('Content-Length', str(cl)))
        self.response_headers.append(('Content-Type', 'text/plain'))
        if self.version == '1.1':
            connection = self.request.headers.get('CONNECTION', '').lower()
            if connection == 'close':
                self.response_headers.append(('Connection', 'close'))
            # under HTTP 1.1 keep-alive is default, no need to set the header
        else:
            # HTTP 1.0
            self.response_headers.append(('Connection', 'close'))
        self.close_on_finish = True
        self.write(tobytes(body))

class WSGITask(Task):
    """A WSGI task produces a response from a WSGI application.
    """
    environ = None

    def execute(self):
        env = self.get_environment()

        def start_response(status, headers, exc_info=None):
            if self.complete and not exc_info:
                raise AssertionError("start_response called a second time "
                                     "without providing exc_info.")
            if exc_info:
                try:
                    if self.wrote_header:
                        # higher levels will catch and handle raised exception:
                        # 1. "service" method in task.py
                        # 2. "service" method in channel.py
                        # 3. "handler_thread" method in task.py
                        reraise(exc_info[0], exc_info[1], exc_info[2])
                    else:
                        # As per WSGI spec existing headers must be cleared
                        self.response_headers = []
                finally:
                    exc_info = None

            self.complete = True

            if not status.__class__ is str:
                raise AssertionError('status %s is not a string' % status)
            if '\n' in status or '\r' in status:
                raise ValueError("carriage return/line "
                                 "feed character present in status")

            self.status = status

            # Prepare the headers for output
            for k, v in headers:
                if not k.__class__ is str:
                    raise AssertionError(
                        'Header name %r is not a string in %r' % (k, (k, v))
                    )
                if not v.__class__ is str:
                    raise AssertionError(
                        'Header value %r is not a string in %r' % (v, (k, v))
                    )

                if '\n' in v or '\r' in v:
                    raise ValueError("carriage return/line "
                                     "feed character present in header value")
                if '\n' in k or '\r' in k:
                    raise ValueError("carriage return/line "
                                     "feed character present in header name")

                kl = k.lower()
                if kl == 'content-length':
                    self.content_length = int(v)
                elif kl in hop_by_hop:
                    raise AssertionError(
                        '%s is a "hop-by-hop" header; it cannot be used by '
                        'a WSGI application (see PEP 3333)' % k)

            self.response_headers.extend(headers)

            # Return a method used to write the response data.
            return self.write

        # Call the application to handle the request and write a response
        app_iter = self.channel.server.application(env, start_response)

        if app_iter.__class__ is ReadOnlyFileBasedBuffer:
            # NB: do not put this inside the below try: finally: which closes
            # the app_iter; we need to defer closing the underlying file.  It's
            # intention that we don't want to call ``close`` here if the
            # app_iter is a ROFBB; the buffer (and therefore the file) will
            # eventually be closed within channel.py's _flush_some or
            # handle_close instead.
            cl = self.content_length
            size = app_iter.prepare(cl)
            if size:
                if cl != size:
                    if cl is not None:
                        self.remove_content_length_header()
                    self.content_length = size
                self.write(b'') # generate headers
                self.channel.write_soon(app_iter)
                return

        try:
            first_chunk_len = None
            for chunk in app_iter:
                if first_chunk_len is None:
                    first_chunk_len = len(chunk)
                    # Set a Content-Length header if one is not supplied.
                    # start_response may not have been called until first
                    # iteration as per PEP, so we must reinterrogate
                    # self.content_length here
                    if self.content_length is None:
                        app_iter_len = None
                        if hasattr(app_iter, '__len__'):
                            app_iter_len = len(app_iter)
                        if app_iter_len == 1:
                            self.content_length = first_chunk_len
                # transmit headers only after first iteration of the iterable
                # that returns a non-empty bytestring (PEP 3333)
                if chunk:
                    self.write(chunk)

            cl = self.content_length
            if cl is not None:
                if self.content_bytes_written != cl:
                    # close the connection so the client isn't sitting around
                    # waiting for more data when there are too few bytes
                    # to service content-length
                    self.close_on_finish = True
                    if self.request.command != 'HEAD':
                        self.logger.warning(
                            'application returned too few bytes (%s) '
                            'for specified Content-Length (%s) via app_iter' % (
                                self.content_bytes_written, cl),
                        )
        finally:
            if hasattr(app_iter, 'close'):
                app_iter.close()

    def parse_proxy_headers(self, environ, headers):
        forwarded_for = None
        client_addr = None

        forwarded_for = None

        if 'X_FORWARDED_FOR' in headers:
            forwarded_for = []

            for forward_hop in headers['X_FORWARDED_FOR'].split(','):
                forward_hop = forward_hop.strip()

                # Make sure that all IPv6 addresses are surrounded by brackets

                if ':' in forward_hop and forward_hop[-1] != ']':
                    forwarded_for.append('[{}]'.format(forward_hop))
                else:
                    forwarded_for.append(forward_hop)

            client_addr, forwarded_for = forwarded_for[0], forwarded_for[1:]

        forwarded_host = headers.get('X_FORWARDED_HOST', None)
        forwarded_proto = headers.get('X_FORWARDED_PROTO', None)
        forwarded_port = headers.get('X_FORWARDED_PORT', None)
        forwarded_by = headers.get('X_FORWARDED_BY', None)
        forwarded = headers.get('FORWARDED', None)

        # If the Forwarded header exists, it gets priority, and will warn if
        # the other headers were not None and discard them.

        if forwarded:
            if (
                    (
                        forwarded_host or
                        forwarded_proto or
                        forwarded_port or
                        forwarded_by
                    ) and self.logged_multi_proxy_headers is False
            ):
                self.logger.warning(
                    'The Forwarded header was found to exist alongside '
                    'one or more of the older X-Forwarded-Host, '
                    'X-Forwarded-Proto, X-Forwarded-For, '
                    'X-Forwarded-Port, X-Forwarded-By headers. Waitress will '
                    'ignore the older style headers, but this could be a '
                    'security issue. Please make sure to remove the invalid '
                    'headers before passing the request to Waitress.'
                )

                for header in {
                    'X_FORWARDED_FOR', 'X_FORWARDED_HOST',
                    'X_FORWARDED_PROTO', 'X_FORWARDED_PORT',
                    'X_FORWARDED_BY',
                }:
                    headers.pop(header, None)

                forwarded_for = forwarded_host = forwarded_proto = None
                forwarded_port = forwarded_by = None

            def _undquote(myval):
                if '"' in myval:
                    return myval.strip().strip('"')

                return myval

            for parameter in forwarded.split(';'):
                parameter = parameter.strip().lower()

                if parameter.startswith('by'):
                    forwarded_by = _undquote(parameter.split('=', 1)[1])

                if parameter.startswith('for'):
                    forwarded_for = []

                    for for_param in parameter.split(','):
                        for_param = for_param.strip()

                        if not for_param.startswith('for'):
                            raise ValueError(
                                'Invalid Forwarded For parameter provided.')

                        forwarded_for.append(
                            _undquote(parameter.split('=', 1)[1]))

                    client_addr, forwarded_for = (
                        forwarded_for[0], forwarded_for[1:])

                if parameter.startswith("host"):
                    forwarded_host = _undquote(parameter.split('=', 1)[1])

                if parameter.startswith("proto"):
                    forwarded_proto = parameter.split('=', 1)[1].strip()

        if forwarded_proto:
            forwarded_proto = forwarded_proto.lower()

            if forwarded_proto not in {'http', 'https'}:
                raise ValueError(
                    'Invalid "Forwarded Proto=" or "X-Forwarded-For" value.')

            # Set the URL scheme to the proxy privided proto
            environ['wsgi.url_scheme'] = forwarded_proto

            if not forwarded_port:
                if forwarded_proto == 'http':
                    forwarded_port = 80

                if forwarded_proto == 'https':
                    forwarded_port = 443

        if forwarded_host:
            if ':' in forwarded_host and forwarded_host[-1] != ']':
                host, port = forwarded_host.rsplit(':', 1)
                host, port = host.strip(), str(port)

                if forwarded_port != port:
                    forwarded_port = port
            else:
                host = forwarded_host.strip()

            # We trust the proxy server's forwarded Host
            environ['SERVER_NAME'] = host
            environ['HTTP_HOST'] = host

        if forwarded_port:
            environ['SERVER_PORT'] = forwarded_port

        if client_addr:
            if ':' in client_addr and client_addr[-1] != ']':
                addr, port = client_addr.rsplit(':', 1)
                environ['REMOTE_ADDR'] = addr.strip()
                environ['REMOTE_PORT'] = port.strip()
            else:
                environ['REMOTE_ADDR'] = client_addr.strip()

    def get_environment(self):
        """Returns a WSGI environment."""
        environ = self.environ
        if environ is not None:
            # Return the cached copy.
            return environ

        request = self.request
        path = request.path
        channel = self.channel
        server = channel.server
        url_prefix = server.adj.url_prefix

        if path.startswith('/'):
            # strip extra slashes at the beginning of a path that starts
            # with any number of slashes
            path = '/' + path.lstrip('/')

        if url_prefix:
            # NB: url_prefix is guaranteed by the configuration machinery to
            # be either the empty string or a string that starts with a single
            # slash and ends without any slashes
            if path == url_prefix:
                # if the path is the same as the url prefix, the SCRIPT_NAME
                # should be the url_prefix and PATH_INFO should be empty
                path = ''
            else:
                # if the path starts with the url prefix plus a slash,
                # the SCRIPT_NAME should be the url_prefix and PATH_INFO should
                # the value of path from the slash until its end
                url_prefix_with_trailing_slash = url_prefix + '/'
                if path.startswith(url_prefix_with_trailing_slash):
                    path = path[len(url_prefix):]

        environ = {}
        environ['REQUEST_METHOD'] = request.command.upper()
        environ['SERVER_PORT'] = str(server.effective_port)
        environ['SERVER_NAME'] = server.server_name
        environ['SERVER_SOFTWARE'] = server.adj.ident
        environ['SERVER_PROTOCOL'] = 'HTTP/%s' % self.version
        environ['SCRIPT_NAME'] = url_prefix
        environ['PATH_INFO'] = path
        environ['QUERY_STRING'] = request.query
        remote_peer = environ['REMOTE_ADDR'] = channel.addr[0]

        headers = dict(request.headers)

        if remote_peer == server.adj.trusted_proxy or server.trusted_proxy:
            self.parse_proxy_headers(environ, headers)
        else:
            # If we are not relying on a proxy, we still want to try and set
            # the REMOTE_PORT to something useful, maybe None though.
            environ['REMOTE_PORT'] = str(channel.addr[1])

        # Nah, we aren't actually going to look up the reverse DNS for
        # REMOTE_ADDR, but we will happily set this environment variable for
        # the WSGI application. Spec says we can just set this to REMOTE_ADDR,
        # so we do.
        environ['REMOTE_HOST'] = environ['REMOTE_ADDR']

        for key, value in headers.items():
            value = value.strip()
            mykey = rename_headers.get(key, None)
            if mykey is None:
                mykey = 'HTTP_%s' % key
            if mykey not in environ:
                environ[mykey] = value

        # the following environment variables are required by the WSGI spec
        environ['wsgi.version'] = (1, 0)

        # May have already been set by the proxy

        if 'wsgi.url_scheme' not in environ:
            environ['wsgi.url_scheme'] = request.url_scheme
        # apps should use the logging module
        environ['wsgi.errors'] = sys.stderr
        environ['wsgi.multithread'] = True
        environ['wsgi.multiprocess'] = False
        environ['wsgi.run_once'] = False
        environ['wsgi.input'] = request.get_body_stream()
        environ['wsgi.file_wrapper'] = ReadOnlyFileBasedBuffer
        environ['wsgi.input_terminated'] = True  # wsgi.input is EOF terminated

        self.environ = environ
        return environ
