"""Experiment timing, observation failures, routing and recovery without cloud faults."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

import httpx

from harness.phase5 import Cloud, Harness, HarnessError, Monitor, Results, compile_report, percentile


CLOUDS = {name: Cloud(name, name + '-region', f'https://{name}.test', f'192.0.2.{i}', name+'-api')
          for i, name in enumerate(('gcp', 'aws', 'azure'), 1)}


class Phase5Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.result = Results(Path(self.folder.name), 'test')
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.result.close()
        self.folder.cleanup()

    def harness(self, handler, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return Harness(CLOUDS, client, self.result, interval=.005, timeout=.15, **kwargs)

    def lookup(self, statuses=None):
        return httpx.Response(200, json={'service_name': 'payments-api', 'endpoints': [
            {'region_id': cloud.region_id, 'endpoint': '10.0.0.1:8081',
             'status': (statuses or {}).get(name, 'UP')}
            for name, cloud in CLOUDS.items()]})

    async def test_lost_fail_response_still_recovers(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/fail'):
                raise httpx.ReadTimeout('response lost', request=request)
            return httpx.Response(200, json={'status': 'UP'})
        harness = self.harness(handler)
        with self.assertRaises(HarnessError):
            await harness.trigger('gcp', 'payments-api', 'fail')
        self.assertIn(('gcp', 'payments-api'), harness.failed)
        await harness.cleanup()
        self.assertEqual(calls, ['/payments-api/simulate/fail', '/payments-api/simulate/recover'])
        self.assertFalse(harness.failed)

    async def test_unreachable_observer_does_not_freeze_other_observers(self):
        release = asyncio.Event()
        async def handler(request):
            if request.url.host == 'aws.test':
                await release.wait()
                raise httpx.ConnectTimeout('offline', request=request)
            return self.lookup()
        harness = self.harness(handler)
        async with Monitor(harness, 1, 'test', 'payments-api', time.perf_counter(), self.result) as monitor:
            await monitor.wait({name: {'azure': 'UP'} for name in ('gcp', 'azure')}, .5)
            self.assertEqual(monitor.states['gcp']['azure'], 'UP')
            self.assertEqual(monitor.states['azure']['gcp'], 'UP')
            self.assertNotIn('aws', monitor.states)
            with self.assertRaises(HarnessError):
                await monitor.wait(harness.expected(), .02)
            release.set()
            for _ in range(50):
                if monitor.errors['aws']:
                    break
                await asyncio.sleep(.01)
            self.assertGreater(monitor.errors['aws'], 0)

    async def test_missing_region_and_http_errors_never_count_as_up(self):
        for response in (httpx.Response(404), httpx.Response(200, json={'endpoints': []}),
                         httpx.Response(200, json={'endpoints': [{'region_id': 'gcp-region', 'status': 'BAD'}]})):
            harness = self.harness(lambda request: response)
            with self.assertRaises(HarnessError):
                await harness.baseline(timeout=.02)

    async def test_new_phase_requires_fresh_observations(self):
        harness = self.harness(lambda request: self.lookup())
        async with Monitor(harness, 1, 'test', 'payments-api', 0, self.result) as monitor:
            await monitor.wait(harness.expected(), .1)
            before = time.perf_counter()
            monitor.first.clear()
            self.assertFalse(monitor.matches(harness.expected()))
            await monitor.wait(harness.expected(), .1)
            self.assertTrue(all(stamp >= before for stamp in monitor.first.values()))

    async def test_convergence_records_both_directions_and_rotates_regions(self):
        states = {name: 'UP' for name in CLOUDS}
        triggers = []
        def handler(request):
            name = request.url.host.split('.')[0]
            if request.url.path == '/lookup':
                return self.lookup(states)
            action = request.url.path.split('/')[-1]
            states[name] = 'DOWN' if action == 'fail' else 'UP'
            triggers.append((name, action))
            return httpx.Response(200, json={'status': states[name]})
        harness = self.harness(handler)
        await harness.convergence(list(CLOUDS), 2, 'payments-api')
        self.assertEqual([name for name, action in triggers if action == 'fail'], list(CLOUDS)*2)
        self.assertEqual(len(self.result.rows), 36)
        self.assertTrue(all(value == 'UP' for value in states.values()))
        self.assertFalse(harness.failed)

    async def test_demo_retries_an_up_but_failed_endpoint_using_proxy_addresses(self):
        called = []
        def handler(request):
            called.append(str(request.url))
            if request.url.path == '/lookup':
                return self.lookup()
            if request.url.host == 'gcp.test':
                return httpx.Response(503, json={'detail': 'failed'})
            return httpx.Response(200, json={'status': 'UP'})
        harness = self.harness(handler)
        response = await harness.client_once('payments-api')
        self.assertEqual(response['cloud'], 'aws')
        self.assertTrue(all('10.0.0.1' not in url for url in called))

    async def test_single_region_service_has_no_invented_failover(self):
        def handler(request):
            return httpx.Response(200, json={'endpoints': [
                {'region_id': 'aws-region', 'status': 'DOWN', 'endpoint': '10.0.0.1:8084'}]})
        response = await self.harness(handler).client_once('recommend-api')
        self.assertEqual(response['status'], 'UNAVAILABLE')

    async def test_rejected_recovery_keeps_recovery_obligation(self):
        harness = self.harness(lambda request: httpx.Response(503))
        harness.failed.add(('gcp', 'payments-api'))
        with self.assertRaises(HarnessError):
            await harness.cleanup()
        self.assertIn(('gcp', 'payments-api'), harness.failed)

    async def test_latency_records_exact_sample_counts_and_surfaces_http_failures(self):
        def handler(request):
            if request.url.path == '/lookup':
                return self.lookup()
            if request.url.host == 'aws.test':
                return httpx.Response(429)
            return httpx.Response(200, json={'status': 'ok'})
        harness = self.harness(handler)
        with self.assertRaises(HarnessError):
            await harness.latency('payments-api', samples=2)
        self.assertEqual(len(self.result.rows), 12)
        failed = [row for row in self.result.rows if not row['passed']]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(row['target_region'] == 'aws' and row['phase'] == '/health' for row in failed))

    async def test_partition_requires_stale_before_claiming_latest_state_convergence(self):
        states = {name: 'UP' for name in CLOUDS}
        cues = []
        async def cue(message):
            cues.append(message)
        def handler(request):
            if request.url.path == '/lookup':
                return self.lookup(states)
            states['gcp'] = 'DOWN' if request.url.path.endswith('fail') else 'UP'
            return httpx.Response(200, json={'status': states['gcp']})
        harness = self.harness(handler, cue=cue)
        # Speed up inter-trigger spacing, retain actual monitoring timeout.
        with self.assertRaises(HarnessError):
            await harness.partition_updates(['gcp'], 1, 'payments-api')
        self.assertTrue(any('confirm both rules' in cue for cue in cues))
        self.assertEqual(states['gcp'], 'UP')
        self.assertEqual(self.result.rows, [])


class SummaryTests(unittest.TestCase):
    def test_report_distinguishes_health_checks_from_unrun_experiments(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'sample-healthcheck'
            folder.mkdir()
            (folder / 'session.json').write_text(json.dumps({'command': 'healthcheck', 'parameters': {}}))
            (folder / 'healthcheck-summary.json').write_text(json.dumps({'outcome': 'PASS', 'metrics': []}))
            report = compile_report(Path(directory)).read_text(encoding='utf-8')
            self.assertIn('| healthcheck | PASS |', report)
            self.assertIn('convergence: no passing cloud run recorded', report)
            self.assertIn('Owner inputs (left blank as requested)', report)

    def test_nearest_rank_p95_and_failed_samples_are_not_success_timings(self):
        self.assertEqual(percentile(list(range(1, 201)), .95), 190)
        with tempfile.TemporaryDirectory() as directory:
            result = Results(Path(directory), 'latency')
            result.outcome = 'FAILED_OR_INTERRUPTED'
            result.metric(1, 'latency', 'gcp', '/lookup', 'request_latency', .02)
            result.metric(2, 'latency', 'gcp', '/lookup', 'request_latency', 2, passed=False)
            result.close()
            summary = json.loads(result.summary_path.read_text())
            self.assertEqual(summary['outcome'], 'FAILED_OR_INTERRUPTED')
            self.assertEqual(summary['metrics'][0]['failed'], 1)
            self.assertEqual(summary['metrics'][0]['max'], .02)


if __name__ == '__main__':
    unittest.main()
