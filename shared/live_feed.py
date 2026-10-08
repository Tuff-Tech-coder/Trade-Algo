"""Validated, deduplicated history of CLOSED bars; timestamps label bar opens."""
import json
import logging
import math
import pathlib
from collections import deque
import pandas as pd

log = logging.getLogger(__name__)
_DEFAULT_CACHE = pathlib.Path(__file__).parent / 'bar_cache.json'

def normalize_bar(bar):
    if not isinstance(bar, dict):
        raise ValueError('bar must be an object')
    ts = pd.Timestamp(bar.get('timestamp', bar.get('time')))
    if pd.isna(ts) or ts.tzinfo is None:
        raise ValueError('bar timestamp must include a timezone')
    result = {'timestamp': ts.tz_convert('UTC').isoformat()}
    for key in ('open', 'high', 'low', 'close', 'volume'):
        result[key] = float(bar.get(key, 0) if key == 'volume' else bar[key])
        if not math.isfinite(result[key]):
            raise ValueError('bar values must be finite')
    if result['low'] > min(result['open'], result['close']) or result['high'] < max(result['open'], result['close']) or result['low'] > result['high'] or result['volume'] < 0:
        raise ValueError('invalid OHLCV bar')
    return result

class BarStore:
    def __init__(self, maxlen=300):
        self._maxlen = int(maxlen)
        self._buffers = {}
        self.contracts = {}

    def push(self, symbol, tf_minutes, bar):
        key = (symbol.upper(), int(tf_minutes))
        if key[1] <= 0:
            raise ValueError('timeframe must be positive')
        normalized = normalize_bar(bar)
        rows = {row['timestamp']: row for row in self._buffers.get(key, [])}
        changed = rows.get(normalized['timestamp']) != normalized
        rows[normalized['timestamp']] = normalized
        self._buffers[key] = deque(sorted(rows.values(), key=lambda r: r['timestamp'])[-self._maxlen:], maxlen=self._maxlen)
        return changed

    def get_df(self, symbol, tf_minutes):
        rows = self._buffers.get((symbol.upper(), int(tf_minutes)))
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(list(rows))
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        return df

    def resample(self, symbol, base_tf, target_tf):
        df = self.get_df(symbol, base_tf)
        if df.empty:
            return df
        result = df.set_index('timestamp').resample(f'{target_tf}min', closed='left', label='left').agg(
            open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'))
        counts = df.set_index('timestamp')['close'].resample(f'{target_tf}min').count()
        completed_until = df['timestamp'].iloc[-1] + pd.Timedelta(minutes=base_tf)
        result = result[(result.index + pd.Timedelta(minutes=target_tf) <= completed_until) & (counts == target_tf // base_tf)]
        return result.dropna(subset=['open']).reset_index()

    def bar_count(self, symbol, tf_minutes):
        return len(self._buffers.get((symbol.upper(), int(tf_minutes)), []))

    def save(self, path=_DEFAULT_CACHE):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {'buffers': {f'{s}|{tf}': list(buf) for (s,tf),buf in self._buffers.items()}, 'contracts': self.contracts}
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data), encoding='utf-8')
        temp.replace(path)

    def load(self, path=_DEFAULT_CACHE):
        path = pathlib.Path(path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            restored = BarStore(self._maxlen)
            for key, rows in data.get('buffers', {}).items():
                symbol, _, tf = key.rpartition('|')
                for row in rows:
                    restored.push(symbol, int(tf), row)
            restored.contracts = data.get('contracts', {})
            self._buffers, self.contracts = restored._buffers, restored.contracts
        except (ValueError, KeyError, TypeError, OSError) as exc:
            log.error('bar_cache_load_failed: %s', exc)
