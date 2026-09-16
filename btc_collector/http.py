"""Bounded public HTTPS GET client using runner-provided curl; never handles keys."""
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit
from pathlib import Path

class FetchError(RuntimeError):
    pass

class Client:
    def __init__(self, timeout=30, attempts=3):
        self.timeout=timeout; self.attempts=attempts
        self.lock=threading.Lock(); self.next_call={}; self.blocked={}; self.failures={}; self.cooldown={}; self.audit=[]

    def get(self, base, path, **params):
        url=base+path+('?' + urlencode(params) if params else '')
        host=urlsplit(url).netloc
        # Shared per-host throttle across all concurrent collectors, including retries.
        spacing=0.26 if 'deribit' in host or 'coinbase' in host else 0.12
        if path.endswith('get_instruments'): spacing=1.05
        for attempt in range(self.attempts):
            with self.lock:
                if host in self.blocked: raise FetchError(self.blocked[host])
                delay=max(0,self.next_call.get(host,0)-time.monotonic())
                self.next_call[host]=time.monotonic()+delay+spacing
            if delay: time.sleep(delay)
            # A 429 response pauses the whole host, including already queued work.
            while True:
                with self.lock: pause=max(0,self.cooldown.get(host,0)-time.monotonic())
                if not pause: break
                time.sleep(pause)
            started=time.time(); status=None; headers={}
            try:
                with tempfile.TemporaryDirectory() as folder:
                    hp=os.path.join(folder,'headers'); bp=os.path.join(folder,'body')
                    p=subprocess.run(['curl','--silent','--show-error','--proto','=https','--connect-timeout','20',
                        '--max-time',str(self.timeout),'--max-filesize','20000000','--dump-header',hp,'--output',bp,
                        '--write-out','%{http_code}','--header','Accept: application/json',
                        '--user-agent','btc-market-data-collector/1.0 (public-data-only)',url],
                        capture_output=True,text=True,timeout=self.timeout+5)
                    if p.returncode: raise FetchError('curl: '+p.stderr.strip()[:250])
                    status=int(p.stdout)
                    for line in Path(hp).read_text(encoding='utf-8').splitlines():
                        if line.startswith('HTTP/'): headers={}
                        elif ':' in line:
                            k,v=line.split(':',1); headers[k.lower()]=v.strip()
                    if status != 200: raise FetchError(f'HTTP {status}')
                    with open(bp,encoding='utf-8') as f: data=json.load(f)
                if isinstance(data,dict) and ('error' in data or isinstance(data.get('code'),int) and data['code']<0):
                    raise FetchError('API error: '+str(data.get('error',data))[:250])
                with self.lock: self.failures[host]=0
                self._record(url,started,status,'ok')
                return data,headers,time.time()
            except (FetchError, subprocess.TimeoutExpired, OSError, ValueError) as e:
                self._record(url,started,status,'error',str(e))
                with self.lock:
                    self.failures[host]=self.failures.get(host,0)+1
                    if self.failures[host]>=8 and (status is None or status>=500):
                        self.blocked[host]=f'{host}: circuit opened after repeated failures; retry next run'
                if status in (401,403,418,451):
                    with self.lock: self.blocked[host]=f'{host}: HTTP {status}; access denied, no bypass attempted'
                    raise FetchError(self.blocked[host]) from e
                if status is not None and 400<=status<500 and status!=429:
                    raise FetchError(str(e)) from e
                if attempt+1==self.attempts: raise FetchError(str(e)) from e
                retry=headers.get('retry-after','')
                try: wait=max(2**(attempt+1),float(retry))
                except ValueError: wait=2**(attempt+1)
                if wait>30:
                    with self.lock: self.blocked[host]=f'{host}: Retry-After {wait}s; deferred to next run'
                    raise FetchError(self.blocked[host]) from e
                logging.warning('%s failed (%s); retry in %.1fs',host,e,wait)
                if status==429:
                    with self.lock: self.cooldown[host]=max(self.cooldown.get(host,0),time.monotonic()+wait)
                time.sleep(wait)

    def _record(self,url,started,code,status,error=None):
        with self.lock:
            self.audit.append(dict(url=url,requested_at=started,received_at=time.time(),http_status=code,status=status,error=error))
