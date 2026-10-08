"""Closed-bar strategy interface, broker reconciliation and authenticated routing."""
from __future__ import annotations
import asyncio
import datetime
import logging
import os
import re
import uuid
from zoneinfo import ZoneInfo
import httpx
import pandas as pd
from live_feed import BarStore
from security import validate_local_url

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')
ACCEPTED = {'submitted', 'forwarded', 'close_submitted', 'exit_pending', 'flat'}

class SignalGenerator:
    strategy_id = ''
    bar_size = 5
    htf_bar_size = None

    def __init__(self, risk_cfg, main_cfg):
        self.strategy_id = risk_cfg['strategy_id']
        self.symbol = risk_cfg['live_symbol'].upper()
        self.base_symbol = re.sub(r'[FGHJKMNQUVXZ]\d{1,2}$', '', self.symbol)
        self.tick = float(risk_cfg.get('tick_size', .25))
        self.contracts = int(risk_cfg.get('contracts', 1))
        self.mode = main_cfg.get('mode', risk_cfg.get('mode', 'PAPER'))
        self.bar_size = int(risk_cfg.get('bar_size', type(self).bar_size))
        self.htf_bar_size = risk_cfg.get('htf_bar_size', type(self).htf_bar_size)
        cfg = main_cfg['risk_server']
        self._webhook_base = validate_local_url(cfg['webhook_url'].rstrip('/'))
        self._risk_api_key = os.environ.get('RISK_SERVER_API_KEY')
        self._stale_seconds = float(cfg.get('stale_signal_threshold_seconds', 30))
        self._http = None
        self._lock = asyncio.Lock()
        self._last_processed_ts = None
        self._last_trade_date = None
        self._reset_state()

    def _reset_state(self):
        self._daily_trades = 0

    def _check_daily_reset(self, today=None):
        today = today or datetime.datetime.now(ET).date()
        if today != self._last_trade_date:
            self._last_trade_date = today
            self._last_processed_ts = None
            self._reset_state()

    async def _get_http(self):
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False)
        return self._http

    async def _broker_state(self):
        try:
            http = await self._get_http()
            base = self._webhook_base.rsplit('/webhook', 1)[0]
            resp = await http.get(f'{base}/strategy-state/{self.strategy_id}', headers={'X-Api-Key': self._risk_api_key or ''})
            resp.raise_for_status()
            state = resp.json()
            if not state.get('ready'):
                return False
            self.on_broker_state(state)
            return True
        except (httpx.HTTPError, ValueError) as exc:
            log.warning('broker_state_unavailable strategy=%s error=%s', self.strategy_id, type(exc).__name__)
            return False

    def on_broker_state(self, state):
        pass

    def on_signal_result(self, signal, result):
        if signal.get('action') in ('buy', 'sell') and result.get('status') in ACCEPTED:
            self._daily_trades += 1

    async def post_signal(self, signal):
        extras = signal.setdefault('extras', {})
        extras.update(strategy=self.strategy_id, mode=self.mode)
        extras.setdefault('signal_ts', datetime.datetime.now(datetime.timezone.utc).isoformat())
        extras.setdefault('signal_id', str(uuid.uuid4()))
        try:
            http = await self._get_http()
            resp = await http.post(f'{self._webhook_base}/{self.strategy_id}', json=signal, headers={'X-Api-Key': self._risk_api_key or ''})
            resp.raise_for_status()
            result = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            result = {'status': 'error', 'detail': 'risk server request failed'}
        self.on_signal_result(signal, result)
        return result

    async def on_bar(self, bar_store):
        async with self._lock:
            self._check_daily_reset()
            df = bar_store.get_df(self.base_symbol, self.bar_size)
            if len(df) < 3:
                return
            ts = df['timestamp'].iloc[-1]
            if self._last_processed_ts is not None and ts <= self._last_processed_ts:
                return
            self.symbol = bar_store.contracts.get(self.base_symbol, self.symbol)
            completed = ts + pd.Timedelta(minutes=self.bar_size)
            age = (pd.Timestamp.now(tz='UTC') - completed).total_seconds()
            if age > self._stale_seconds or age < -1:
                return
            if not await self._broker_state():
                return
            htf = bar_store.resample(self.base_symbol, self.bar_size, int(self.htf_bar_size)) if self.htf_bar_size else None
            signal = self.check_signal(df, htf)
            self._last_processed_ts = ts
            if signal:
                signal.setdefault('extras', {})['signal_ts'] = completed.isoformat()
                signal['extras']['signal_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{self.strategy_id}:{ts}:{signal["action"]}'))
                result = await self.post_signal(signal)
                if result.get('status') == 'error':
                    self._last_processed_ts = None  # an HTTP retry can re-evaluate safely

    def check_exit(self, now):
        return None

    async def on_timer(self, now=None):
        async with self._lock:
            now = now or datetime.datetime.now(ET)
            self._check_daily_reset(now.astimezone(ET).date())
            await self._broker_state()  # exits still run when entry readiness is unavailable
            signal = self.check_exit(now)
            if signal:
                await self.post_signal(signal)

    def check_signal(self, df, htf_df=None):
        raise NotImplementedError

    async def close(self):
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
