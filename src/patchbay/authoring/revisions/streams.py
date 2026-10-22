"""Checked, deadline-bound Git pipe reads; intentional prefixes stay explicit."""
from __future__ import annotations

import io
import math
import os
import selectors
import subprocess
import time

from ..errors import AuthorError,require


class GitReadStream(io.BufferedIOBase):
    def __init__(self,process,*,expected_bytes=None,timeout=30):
        super().__init__()
        self.process=process
        require(isinstance(timeout, (int, float)) and math.isfinite(timeout) and 0 < timeout <= 120,
                "INVALID_REQUEST", "Git read deadline must be finite and within 120 seconds")
        self.expected_bytes=expected_bytes
        self.delivered=0
        self.eof=False
        self.stderr=bytearray()
        self.deadline=time.monotonic()+timeout
        self.selector=selectors.DefaultSelector()
        try:
            for stream,label in [(process.stdout,'stdout'),(process.stderr,'stderr')]:
                os.set_blocking(stream.fileno(),False)
                self.selector.register(stream,selectors.EVENT_READ,label)
        except BaseException:
            self.close()
            raise

    def readable(self):return True

    def _failure(self,message):
        return AuthorError('SOURCE_READ_FAILED',message,expected_bytes=self.expected_bytes,
            observed_bytes=self.delivered,exit_code=self.process.poll(),
            stderr=bytes(self.stderr).decode('utf-8','replace'))

    def _chunk(self,size):
        while not self.eof:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:
                raise AuthorError('SOURCE_READ_TIMEOUT','Git source producer exceeded its read deadline',observed_bytes=self.delivered)
            for key,_ in self.selector.select(min(.1,remaining)):
                try:data=os.read(key.fileobj.fileno(),min(size,65536) if key.data=='stdout' else 65536)
                except BlockingIOError:continue
                except OSError as exc:
                    raise self._failure('Git source pipe read failed') from exc
                if not data:
                    self.selector.unregister(key.fileobj)
                    if key.data=='stdout':self.eof=True
                elif key.data=='stderr':
                    self.stderr.extend(data)
                    if len(self.stderr)>4000:del self.stderr[:-4000]
                else:
                    self.delivered+=len(data)
                    if self.expected_bytes is not None and self.delivered>self.expected_bytes:
                        raise self._failure('Git source producer exceeded the advertised object size')
                    return data
        if self.expected_bytes is not None and self.delivered!=self.expected_bytes:
            raise self._failure('Git source producer ended before the advertised object size')
        return b''

    def read(self,size=-1):
        require(not self.closed,'SOURCE_READ_FAILED','Git source stream is closed')
        if size==0:return b''
        parts=[];count=0
        while size<0 or count<size:
            chunk=self._chunk(65536 if size<0 else size-count)
            if not chunk:break
            parts.append(chunk);count+=len(chunk)
        return b''.join(parts)

    def read1(self,size=-1):
        require(not self.closed,'SOURCE_READ_FAILED','Git source stream is closed')
        return self._chunk(65536 if size<0 else max(1,size)) if size else b''

    def readinto(self,buffer):
        data=self.read(len(buffer));buffer[:len(data)]=data;return len(data)

    def finish(self):
        # A complete consumer must actually consume the object. Do not drain an
        # unread suffix and then imply that the caller hashed/read those bytes.
        if self.expected_bytes is not None and self.delivered!=self.expected_bytes:
            raise self._failure('Complete source consumer stopped before the full object')
        if not self.eof and self._chunk(1):
            raise self._failure('Complete source consumer stopped before producer EOF')
        # Keep draining bounded diagnostics while waiting; stderr cannot block
        # the producer after stdout closes.
        while self.selector.get_map():
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise AuthorError('SOURCE_READ_TIMEOUT','Git producer did not complete before its deadline')
            for key,_ in self.selector.select(min(.1,remaining)):
                try:data=os.read(key.fileobj.fileno(),65536)
                except BlockingIOError:continue
                except OSError as exc:
                    raise self._failure('Git source pipe read failed') from exc
                if not data:self.selector.unregister(key.fileobj)
                else:
                    self.stderr.extend(data)
                    if len(self.stderr)>4000:del self.stderr[:-4000]
        try:self.process.wait(timeout=max(.001,self.deadline-time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise AuthorError('SOURCE_READ_TIMEOUT','Git producer did not exit before its deadline') from exc
        if self.process.returncode!=0:raise self._failure('Git source producer reported failure')

    def close(self):
        if self.closed:return
        try:
            self.selector.close()
            self.process.stdout.close();self.process.stderr.close()
            if self.process.poll() is None:self.process.terminate()
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    raise AuthorError('SOURCE_READ_FAILED','Owned Git reader could not be reaped after termination') from exc
        finally:super().close()
