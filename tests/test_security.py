"""Attack-path regressions; no services, broker connections, or real orders."""
import asyncio
import datetime as dt
import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shared'))
sys.path.insert(0,str(ROOT/'backtest'))
import httpx
import risk_module as risk
import signal_runner as runner
from live_feed import BarStore
from security import valid_secret,read_body,signal_summary,validate_local_url
from starlette.requests import Request
from fastapi import HTTPException
from engine.reporter import render_html
from engine.metrics import compute_metrics
from engine.loop import RunResult
spec=importlib.util.spec_from_file_location('publication_guard',ROOT/'tools/publication_guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
KEY='a'*64;ADMIN='b'*64

class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_credentials_rejected_even_with_bypass_env(self):
        with patch.object(runner,'_TV_SECRET','replace-with-a-different-64-char-hex-secret'):
            with self.assertRaises(HTTPException) as raised:runner._check_tv_auth('Bearer replace-with-a-different-64-char-hex-secret')
            self.assertEqual(raised.exception.status_code,503)
        with patch.object(risk,'_RISK_API_KEY','replace-with-a-64-char-hex-secret'):
            with self.assertRaises(HTTPException):await risk.verify_api_key('replace-with-a-64-char-hex-secret')
    async def test_docs_and_schema_are_disabled(self):
        for app in (risk.app,runner.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                for path in ('/docs','/redoc','/openapi.json'):
                    self.assertEqual((await client.get(path)).status_code,404)
    async def test_remote_client_rejected(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app,client=('203.0.113.4',123)),base_url='http://localhost') as client:
            self.assertEqual((await client.get('/health')).status_code,403)
    async def test_dns_rebinding_host_rejected(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app),base_url='http://attacker.example') as client:
            self.assertEqual((await client.get('/health')).status_code,400)
    async def test_signal_key_cannot_change_account_state(self):
        with patch.object(risk,'_RISK_API_KEY',KEY),patch.object(risk,'_ADMIN_API_KEY',ADMIN):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=risk.app),base_url='http://localhost') as client:
                self.assertEqual((await client.post('/set_state',json={},headers={'X-Api-Key':KEY})).status_code,403)
                self.assertEqual((await client.post('/reset_day',headers={'X-Api-Key':KEY})).status_code,403)
    async def test_chunked_body_stops_before_buffering_rest(self):
        received=0
        async def receive():
            nonlocal received
            received+=1
            return {'type':'http.request','body':b'X'*40000,'more_body':True}
        request=Request({'type':'http','headers':[]},receive)
        with self.assertRaises(HTTPException) as raised:await read_body(request)
        self.assertEqual(raised.exception.status_code,413);self.assertEqual(received,2)
    async def test_content_length_rejected_without_reading(self):
        receive=AsyncMock()
        request=Request({'type':'http','headers':[(b'content-length',b'999999')]},receive)
        with self.assertRaises(HTTPException):await read_body(request)
        receive.assert_not_awaited()
    async def test_unknown_subscription_does_not_allocate_buffer(self):
        with patch.object(runner,'_TV_SECRET',KEY),patch.object(runner,'_dispatch',{}),patch.object(runner,'bar_store',BarStore()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app),base_url='http://localhost') as client:
                response=await client.post('/history/MES/99',headers={'Authorization':'Bearer '+KEY},json={'bars':[]})
                self.assertEqual(response.status_code,422);self.assertEqual(runner.bar_store._buffers,{})
    async def test_future_bar_does_not_poison_history(self):
        from types import SimpleNamespace
        bar={'timestamp':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat(),'open':100,'high':101,'low':99,'close':100,'volume':1}
        with patch.object(runner,'_TV_SECRET',KEY),patch.object(runner,'_dispatch',{('MES',5):[SimpleNamespace(on_bar=AsyncMock())]}),patch.object(runner,'bar_store',BarStore()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app),base_url='http://localhost') as client:
                response=await client.post('/tv-bar/MES/5',headers={'Authorization':'Bearer '+KEY},json=bar)
                self.assertEqual(response.status_code,422);self.assertEqual(runner.bar_store._buffers,{})
    async def test_reserved_close_reference_cannot_bypass_capacity(self):
        with patch.object(risk,'_RISK_API_KEY',KEY):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=risk.app),base_url='http://localhost') as client:
                response=await client.post('/webhook/MES_INTRADAY_MOMENTUM',headers={'X-Api-Key':KEY},json={'ticker':'MESM6','action':'buy','quantity':1,'stop_loss':99,'take_profit':101,'extras':{'signal_id':'close:123'}})
                self.assertEqual(response.status_code,422)
    async def test_http_failure_does_not_echo_webhook_token(self):
        token='sensitive-webhook-token'
        failure=httpx.RequestError('https://router.example/webhook/'+token)
        with patch.object(risk,'ORDER_ROUTER','traderspost'),patch.object(risk,'_RISK_API_KEY',KEY),patch.object(risk,'_tp_webhook',return_value='https://router.example/webhook/'+token),patch.object(risk,'_http_client',type('Client',(),{'post':AsyncMock(side_effect=failure)})()),patch.object(risk,'_write_decision_log'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=risk.app),base_url='http://localhost') as client:
                response=await client.post('/webhook/MES_INTRADAY_MOMENTUM',headers={'X-Api-Key':KEY},json={'ticker':'MESM6','action':'exit','quantity':1,'extras':{'signal_ts':dt.datetime.now(dt.timezone.utc).isoformat()}})
                self.assertEqual(response.json()['status'],'error');self.assertNotIn(token,response.text)
    async def test_old_exit_replay_rejected(self):
        with patch.object(risk,'_RISK_API_KEY',KEY),patch.object(risk,'_write_decision_log'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=risk.app),base_url='http://localhost') as client:
                response=await client.post('/webhook/MES_INTRADAY_MOMENTUM',headers={'X-Api-Key':KEY},json={'ticker':'MESM6','action':'exit','quantity':1,'extras':{'signal_ts':'2020-01-01T00:00:00Z'}})
                self.assertEqual(response.json()['status'],'rejected')
    async def test_logs_only_keep_allowlisted_fields(self):
        value=signal_summary({'ticker':'MESM6','action':'buy','extras':{'token':'private'},'password':'private'})
        self.assertEqual(value,{'ticker':'MESM6','action':'buy'})
    async def test_external_internal_urls_rejected(self):
        for url in ('http://evil.example/webhook','http://localhost@evil.example','https://localhost/webhook'):
            with self.assertRaises(ValueError):validate_local_url(url)
    async def test_report_text_is_escaped(self):
        result=RunResult(50000,0,50000,0,0,[],[],[],[])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.html'
            render_html(path,title='<script>alert(1)</script>',period='<img src=x>',metrics=compute_metrics(result))
            text=path.read_text(encoding='utf-8');self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text)
    async def test_publication_guard_detects_redacted_secret(self):
        secret='ghp_'+'A'*30
        hits=guard.findings('src.py',secret.encode())
        self.assertTrue(hits);self.assertNotIn(secret,str(hits))
        self.assertTrue(guard.private_path('shared/.env.production'))
        self.assertFalse(guard.private_path('shared/.env.example'))
    async def test_health_details_are_not_public(self):
        with patch.object(runner,'_TV_SECRET',KEY):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app),base_url='http://localhost') as client:
                response=await client.get('/health');self.assertEqual(response.json(),{'status':'ok'})
                self.assertEqual(response.headers['cache-control'],'no-store')
    async def test_publication_guard_checks_ignored_but_tracked_file(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(guard,'ROOT',Path(tmp)):
            subprocess.run(['git','init','-q',tmp],check=True)
            (Path(tmp)/'.env').write_text('private local fixture',encoding='utf-8')
            subprocess.run(['git','-C',tmp,'add','-f','.env'],check=True)
            _,issues=guard.scan()
            self.assertTrue(any(item[0]=='.env' for item in issues))
    async def test_publication_guard_finds_deleted_secret_in_history(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(guard,'ROOT',Path(tmp)):
            subprocess.run(['git','init','-q',tmp],check=True)
            p=Path(tmp)/'old.py';p.write_text('ghp_'+'A'*30,encoding='utf-8')
            subprocess.run(['git','-C',tmp,'add','old.py'],check=True)
            subprocess.run(['git','-C',tmp,'-c','user.name=Offline Test','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture'],check=True)
            p.unlink();subprocess.run(['git','-C',tmp,'add','-u'],check=True)
            _,issues=guard.scan()
            self.assertTrue(any(item[0]=='history:old.py' for item in issues))

if __name__=='__main__':unittest.main()
