"""Offline regression suite. IBKR is mocked; no broker connection is opened."""
import asyncio
import datetime as dt
import importlib.util
import itertools
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
for sub in ('shared','backtest','ibkr_feed'):
    sys.path.insert(0,str(ROOT/sub))
import pandas as pd
import httpx
from ib_insync import Future, MarketOrder
import ibkr_executor as execution
import ibkr_feed as feed
import signal_runner as runner
import risk_module as risk
from signal_base import SignalGenerator
from live_feed import BarStore, normalize_bar
from indicators import rsi
from engine.account import Account
from engine.broker import SimBroker
from engine.bar import Bar
from engine.orders import Order
from engine.loop import BacktestEngine, RunResult
from engine.constraints import NewsBlackout
from engine.metrics import compute_metrics
from adapter import SignalGeneratorAdapter
spec=importlib.util.spec_from_file_location('test_strategy',ROOT/'strategy_01_MES_INTRADAY_MOMENTUM/signal_generator.py')
strategy=importlib.util.module_from_spec(spec);spec.loader.exec_module(strategy)

def make_generator(cls=strategy.IntradayMomentum):
    return cls({'strategy_id':'MES_INTRADAY_MOMENTUM','live_symbol':'MESM6','contracts':1,'vol_ratio_min':0},
               {'mode':'PAPER','risk_server':{'webhook_url':'http://localhost/webhook'}})

def row(ts, close=100):
    return dict(timestamp=ts,open=100,high=max(101,close),low=min(99,close),close=close,volume=1)

class FakeTrade:
    def __init__(self,contract,order):
        self.contract,self.order=contract,order
        self.orderStatus=NS(status='Submitted')
    def isDone(self):
        return self.orderStatus.status in ('Filled','Cancelled','ApiCancelled','Inactive')

class FakeIB:
    def __init__(self):
        self.ids=itertools.count(1)
        self.client=NS(getReqId=lambda:next(self.ids))
        self.trades=[];self.positions_list=[];self.fill_list=[]
    def isConnected(self): return True
    def positions(self): return self.positions_list
    def fills(self): return self.fill_list
    def openTrades(self): return [t for t in self.trades if not t.isDone()]
    def accountValues(self): return [NS(account='DU123',tag='NetLiquidation',currency='USD',value='50000')]
    def placeOrder(self,contract,order):
        if not order.orderId: order.orderId=next(self.ids)
        trade=FakeTrade(contract,order);self.trades.append(trade);return trade
    def cancelOrder(self,order):
        for t in self.trades:
            if t.order.orderId==order.orderId:t.orderStatus.status='Cancelled'
    async def reqPositionsAsync(self): return self.positions_list

def executor():
    ib=FakeIB()
    with patch.object(execution,'IB',return_value=ib): ex=execution.IBKRExecutor()
    ex._account='DU123';ex._positions_ready=ex._executions_ready=True
    ex._balance_updated=ex._pnl_updated=time.monotonic()
    ex.day_realized_pnl_val=ex.day_unrealized_pnl=0
    contract=Future(localSymbol='MESM6',exchange='CME',currency='USD',conId=123)
    ex._contracts['MESM6']=contract
    return ex,contract

class BarsTests(unittest.TestCase):
    def test_duplicate_upsert_and_order(self):
        store=BarStore(2)
        store.push('MES',5,row('2026-01-01T10:05Z'))
        store.push('MES',5,row('2026-01-01T10:00Z'))
        self.assertFalse(store.push('MES',5,row('2026-01-01T10:00Z')))
        store.push('MES',5,row('2026-01-01T10:00Z',102))
        self.assertEqual(store.bar_count('MES',5),2)
        self.assertEqual(store.get_df('MES',5)['close'].iloc[0],102)
    def test_reject_naive_and_nonfinite(self):
        for bad in [row('2026-01-01T10:00'),row('2026-01-01T10:00Z',float('nan'))]:
            with self.assertRaises(ValueError):normalize_bar(bad)
    def test_only_complete_htf(self):
        store=BarStore()
        for ts in pd.date_range('2026-01-01T10:00Z',periods=4,freq='5min'):store.push('MES',5,row(ts))
        result=store.resample('MES',5,15)
        self.assertEqual(len(result),1)
        self.assertEqual(result['volume'].iloc[0],3)
    def test_cache_serializes_timestamp_and_obeys_current_limit(self):
        store=BarStore()
        for ts in pd.date_range('2026-01-01T10:00Z',periods=4,freq='5min'):store.push('MES',5,row(ts))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'cache.json';store.save(path)
            restored=BarStore(2);restored.load(path)
            self.assertEqual(restored.bar_count('MES',5),2)
    def test_rsi_extremes(self):
        self.assertEqual(rsi(pd.Series(range(30))).iloc[-1],100)
        self.assertEqual(rsi(pd.Series(range(30,0,-1))).iloc[-1],0)
        self.assertEqual(rsi(pd.Series([1]*30)).iloc[-1],50)

class StrategyTests(unittest.TestCase):
    def test_restart_recovers_exact_open(self):
        gen=make_generator()
        df=pd.DataFrame([row(t,110) for t in pd.date_range('2024-01-02T14:30Z',periods=20,freq='5min')])
        self.assertIsNotNone(gen.check_signal(df))
        self.assertEqual(gen._session_open,100)
        self.assertFalse(gen._traded_today) # detecting != executing
    def test_missing_open_does_not_substitute_late_price(self):
        gen=make_generator()
        df=pd.DataFrame([row(t,110) for t in pd.date_range('2024-01-02T15:00Z',periods=20,freq='5min')])
        self.assertIsNone(gen.check_signal(df))
    def test_failed_exit_is_retryable(self):
        gen=make_generator();now=dt.datetime(2024,1,2,20,55,tzinfo=dt.timezone.utc)
        signal=gen.check_exit(now)
        gen.on_signal_result(signal,{'status':'error'})
        self.assertIsNotNone(gen.check_exit(now))
        gen.on_signal_result(signal,{'status':'close_submitted'})
        self.assertIsNone(gen.check_exit(now))
        gen.on_broker_state({'position_qty':1,'exit_pending':False})
        self.assertIsNotNone(gen.check_exit(now))
    def test_rejected_entry_does_not_commit(self):
        gen=make_generator();gen.on_signal_result({'action':'buy'},{'status':'rejected'})
        self.assertFalse(gen._traded_today)
        gen.on_signal_result({'action':'buy'},{'status':'submitted'})
        self.assertTrue(gen._traded_today)

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_completed_bar_is_used(self):
        class Spy(SignalGenerator):
            def check_signal(self,df,htf):self.seen=df['timestamp'].iloc[-1]
        gen=make_generator(Spy);gen._broker_state=AsyncMock(return_value=True)
        store=BarStore();latest=pd.Timestamp.now(tz='UTC')-pd.Timedelta(minutes=5)
        for ts in pd.date_range(end=latest,periods=3,freq='5min'):store.push('MES',5,row(ts))
        await gen.on_bar(store)
        self.assertEqual(gen.seen,latest)
        await gen.close()
    async def test_old_bars_do_not_generate_entries(self):
        gen=make_generator();gen.check_signal=NS() # would fail if called
        store=BarStore()
        for ts in pd.date_range('2024-01-01T10:00Z',periods=3,freq='5min'):store.push('MES',5,row(ts))
        await gen.on_bar(store)
        await gen.close()
    async def test_history_endpoint_does_not_dispatch(self):
        spy=NS(on_bar=AsyncMock())
        with patch.object(runner,'_TV_SECRET','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),patch.object(runner,'bar_store',BarStore()),patch.object(runner,'_dispatch',{('MES',5):[spy]}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app),base_url='http://localhost') as client:
                response=await client.post('/history/MES/5',headers={'Authorization':'Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'},json={'bars':[row('2026-01-01T10:00Z')],'contract_ticker':'MESM6'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(runner.bar_store.bar_count('MES',5),1)
                spy.on_bar.assert_not_awaited()
    async def test_duplicate_flatten_submits_one_close(self):
        ex,contract=executor()
        ex.ib.positions_list=[NS(account='DU123',contract=contract,position=1)]
        first=await ex.flatten(contract);second=await ex.flatten(contract)
        self.assertEqual(first['status'],'close_submitted')
        self.assertEqual(second['status'],'exit_pending')
        self.assertEqual(len(ex.ib.trades),1)
    async def test_failed_close_can_be_retried(self):
        ex,contract=executor();ex.ib.positions_list=[NS(account='DU123',contract=contract,position=1)]
        await ex.flatten(contract);ex.ib.trades[-1].orderStatus.status='Inactive'
        self.assertEqual((await ex.flatten(contract))['status'],'close_submitted')
        self.assertEqual(len(ex.ib.trades),2)
    async def test_bracket_account_and_pending_exposure(self):
        ex,contract=executor()
        result=await ex.submit({'ticker':'MESM6','action':'buy','quantity':2,'stop_loss':99,'take_profit':102})
        self.assertEqual(result['status'],'submitted')
        self.assertEqual(ex.open_contracts(),2)
        self.assertTrue(all(t.order.account=='DU123' for t in ex.ib.trades))
        self.assertFalse(ex.ib.trades[0].order.transmit)
        self.assertTrue(ex.ib.trades[-1].order.transmit)
    async def test_position_filter_excludes_other_accounts_and_stock(self):
        ex,contract=executor()
        ex.ib.positions_list=[NS(account='OTHER',contract=contract,position=50),NS(account='DU123',contract=NS(secType='STK'),position=100),NS(account='DU123',contract=contract,position=2)]
        self.assertEqual(ex.open_contracts(),2)
        self.assertEqual(ex._position_size(contract),2)
    async def test_invalid_bracket_never_places_order(self):
        ex,_=executor()
        for stop,target in [(None,None),(105,100),(float('nan'),102)]:
            result=await ex.submit({'ticker':'MESM6','action':'buy','quantity':1,'stop_loss':stop,'take_profit':target})
            self.assertEqual(result['status'],'error')
        self.assertEqual(ex.ib.trades,[])
    async def test_stale_broker_state_not_ready(self):
        ex,_=executor();self.assertTrue(ex.state_ready())
        ex._pnl_updated=0;self.assertFalse(ex.state_ready())
        self.assertTrue(ex.state_ready(require_pnl=False))
    async def test_risk_idempotency_and_capacity(self):
        ex,contract=executor();ex.ib.positions_list=[NS(account='DU123',contract=contract,position=2)]
        with patch.object(risk,'_executor',ex),patch.object(risk,'_RISK_API_KEY','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),patch.object(risk,'MAX_OPEN_CONTRACTS',3),patch.object(risk,'CALENDAR',{'blackout_rules':{'enabled':False}}),patch.object(risk,'_within_session',return_value=True),patch.object(risk,'_entry_results',{}),patch.object(risk,'_write_decision_log'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=risk.app),base_url='http://localhost') as client:
                data={'ticker':'MESM6','action':'buy','quantity':4,'stop_loss':99,'take_profit':102,'extras':{'signal_ts':dt.datetime.now(dt.timezone.utc).isoformat(),'signal_id':'same'}}
                first=(await client.post('/webhook/MES_INTRADAY_MOMENTUM',json=data,headers={'X-Api-Key':'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'})).json()
                self.assertEqual(first['quantity'],1)
                duplicate=(await client.post('/webhook/MES_INTRADAY_MOMENTUM',json=data,headers={'X-Api-Key':'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'})).json()
                self.assertEqual(first,duplicate)
                self.assertEqual(len(ex.ib.trades),3)
                data['extras']['signal_id']='second'
                second=(await client.post('/webhook/MES_INTRADAY_MOMENTUM',json=data,headers={'X-Api-Key':'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'})).json()
                self.assertEqual(second['status'],'rejected')
    async def test_feed_seed_timezone_and_worker(self):
        class Event:
            def __init__(self):self.handlers=[]
            def __iadd__(self,fn):self.handlers.append(fn);return self
            def __isub__(self,fn):self.handlers.remove(fn);return self
        class Bars(list):pass
        bars=Bars([NS(date=dt.datetime(2024,1,2,14,30+i*5,tzinfo=dt.timezone.utc),open=100,high=101,low=99,close=100,volume=1) for i in range(3)])
        bars.updateEvent=Event();ib=NS(reqHistoricalDataAsync=AsyncMock(return_value=bars),cancelHistoricalData=lambda _:None,disconnect=lambda:None)
        posts=[]
        async def handler(request):posts.append(request.url.path);return httpx.Response(200,json={'status':'ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            handle=await feed.subscribe_bars(ib,NS(localSymbol='MESM6'),'MES',5,client,asyncio.get_running_loop())
            self.assertEqual(posts,['/history/MES/5'])
            self.assertEqual(ib.reqHistoricalDataAsync.call_args.kwargs['formatDate'],2)
            handle.delivery_task.cancel();await asyncio.gather(handle.delivery_task,return_exceptions=True)

class ReplayTests(unittest.TestCase):
    def test_stop_limits_drawdown(self):
        broker=SimBroker('MES',.25,1.25,0,0)
        broker.submit_order(Order('long',1,99,110,'review'))
        ts=pd.Timestamp('2024-01-02T15:00Z');broker.on_bar(Bar(ts,100,100,100,100,1,'MES'))
        class Empty:
            def on_session_start(self,*a):pass
            def on_session_end(self,*a):pass
            def on_bar(self,*a):return None
        acct=Account(48010,max_drawdown=10)
        result=BacktestEngine(pd.DataFrame([row(ts+pd.Timedelta('1min')) | {'low':90}]),Empty(),acct,broker).run()
        self.assertEqual(result.total_pnl,-5)
        self.assertFalse(acct.halted)
        self.assertEqual(result.max_drawdown_dollar,5)
    def test_gap_stop_fills_at_open(self):
        broker=SimBroker('MES',.25,1.25,0,0);ts=pd.Timestamp('2024-01-02T15:00Z')
        broker.submit_order(Order('long',1,99,110,'review'));broker.on_bar(Bar(ts,100,100,100,100,1,'MES'))
        events=broker.on_bar(Bar(ts+pd.Timedelta('1min'),90,91,89,90,1,'MES'))
        self.assertEqual(events,[('stop_hit',-50)])
    def test_completed_bucket_evaluated_at_last_minute(self):
        gen=make_generator();gen.check_signal=lambda df,htf:None
        adapter=SignalGeneratorAdapter(gen)
        for ts in pd.date_range('2024-01-02T14:30Z',periods=5,freq='1min'):
            adapter.on_bar(Bar(ts,100,101,99,100,1,'MES'))
        self.assertEqual(len(adapter._bars),1)
        self.assertEqual(adapter._bars[0]['volume'],5)
    def test_inactive_days_recorded_for_annualization(self):
        acct=Account()
        for i in range(10):
            acct.start_session(dt.date(2024,1,2)+dt.timedelta(days=i))
            if i==0:acct.realize_pnl(500)
            acct.end_session()
        result=RunResult(50000,500,50500,0,acct.days_traded,[500],[],[(r.trading_date,r.realized_pnl) for r in acct.get_day_records()],acct.get_day_records())
        self.assertEqual(acct.days_traded,1)
        self.assertEqual(len(result.daily_pnl),10)
        self.assertAlmostEqual(compute_metrics(result)['annualized_return_pct'],.252)

class AdditionalTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_close_submits_once(self):
        ex,contract=executor();ex.ib.positions_list=[NS(account='DU123',contract=contract,position=1)]
        results=await asyncio.gather(ex.flatten(contract),ex.flatten(contract))
        self.assertEqual([r['status'] for r in results],['close_submitted','exit_pending'])
        self.assertEqual(len(ex.ib.trades),1)
    async def test_partial_bracket_failure_cancels_and_blocks(self):
        ex,_=executor();place=ex.ib.placeOrder;calls=0
        def fail_second(contract,order):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('mock transport failure')
            return place(contract,order)
        ex.ib.placeOrder=fail_second
        result=await ex.submit({'ticker':'MESM6','action':'buy','quantity':1,'stop_loss':99,'take_profit':102})
        self.assertEqual(result['status'],'error')
        self.assertTrue(ex.ib.trades[0].isDone())
        self.assertFalse(ex.state_ready())
    async def test_news_checks_use_base_symbol(self):
        signal={'ticker':'MESM6','quantity':1,'extras':{'signal_ts':dt.datetime.now(dt.timezone.utc).isoformat()}}
        with patch.object(risk,'_is_news_blackout',return_value=True) as check:
            result=risk.pre_trade_checks(risk.AccountState(),signal,'MES_INTRADAY_MOMENTUM')
        self.assertFalse(result.accept)
        check.assert_called_once_with('MES')
    async def test_invalid_pnl_disables_entries(self):
        ex,_=executor()
        ex._on_pnl(NS(account='DU123',dailyPnL=0,realizedPnL=float('nan'),unrealizedPnL=0))
        self.assertFalse(ex.state_ready())
    async def test_feed_connection_failure_closes_http(self):
        ib=NS(disconnectedEvent=NS(),connectAsync=AsyncMock(side_effect=ConnectionRefusedError()),disconnect=lambda:None)
        class Event:
            def __iadd__(self,fn):return self
        ib.disconnectedEvent=Event()
        client=NS(aclose=AsyncMock())
        with patch.object(feed,'IB',return_value=ib),patch.object(feed.httpx,'AsyncClient',return_value=client),patch.object(feed,'_setup_connection_handlers'),patch.object(feed,'TV_WEBHOOK_SECRET','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'):
            with self.assertRaises(ConnectionRefusedError):await feed.main('PAPER')
        client.aclose.assert_awaited_once()
    async def test_month_only_expiry_supported(self):
        contract=NS(lastTradeDateOrContractMonth='209912',localSymbol='MESZ9')
        ib=NS(reqContractDetailsAsync=AsyncMock(return_value=[NS(contract=contract)]))
        self.assertIs(await feed.get_front_month(ib,'MES'),contract)
    async def test_incomplete_higher_timeframe_not_replayed(self):
        gen=make_generator();adapter=SignalGeneratorAdapter(gen);adapter.htf_bar_size=15
        df=pd.DataFrame([row(ts) for ts in pd.date_range('2024-01-02T14:30Z',periods=4,freq='5min')])
        htf=adapter._build_htf(df)
        self.assertEqual(len(htf),1)
        self.assertEqual(htf.iloc[0]['timestamp'],df.iloc[0]['timestamp'])
    async def test_partial_manual_sync_does_not_refresh_risk(self):
        with patch.object(risk,'_ADMIN_API_KEY','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'),patch.object(risk,'_RISK_API_KEY','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),patch.object(risk,'_manual_synced_at',0):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=risk.app),base_url='http://localhost') as client:
                response=await client.post('/set_state',json={'drawdown_floor':None},headers={'X-Api-Key':'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(risk._manual_synced_at,0)
                response=await client.post('/set_state',json={'open_positions':-1},headers={'X-Api-Key':'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'})
                self.assertEqual(response.status_code,422)

if __name__=='__main__':unittest.main()
