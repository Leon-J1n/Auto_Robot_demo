"""The /events request/response channel (spec v2 §5.1 - §5.8)."""
import asyncio
import json
import unittest

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from local_gateway import DEFAULT_CONSTANT_CONFIG, StandaloneGateway
from digest import scene_digest


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


def response(request_id, *, ok=True, payload=None, error=None):
    message = {'type': 'browser.response', 'request_id': request_id, 'ok': ok}
    if ok:
        message['payload'] = payload or {}
    else:
        message['error'] = error
    return message


class WsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = StandaloneGateway(
            config=config(browser_request_timeout_seconds=0.2, capture_timeout_seconds=5,
                          max_pending_requests=2))
        app = web.Application()
        app.router.add_get('/events', self.gateway.events)
        app.router.add_get('/getWorld', self.gateway.get_world)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, browser_id='browser-1'):
        socket = await self.client.ws_connect('/events')
        await socket.send_json({'type': 'browser.hello', 'browser_id': browser_id, 'role': 'viewer'})
        for _ in range(200):
            if browser_id in self.gateway.browsers:
                return socket
            await asyncio.sleep(0.005)
        self.fail('browser.hello did not register ' + browser_id)

    async def test_hello_then_request_response_roundtrip(self):
        socket = await self.connect()
        task = asyncio.create_task(self.gateway.dispatch('getWorld', {}))
        frame = await socket.receive_json()
        self.assertEqual(frame['type'], 'browser.request')
        self.assertEqual(frame['action'], 'getWorld')
        self.assertEqual(frame['payload'], {})
        self.assertNotIn('expects_binary', frame)
        self.assertTrue(frame['request_id'])
        await socket.send_json(response(frame['request_id'], payload={'scene_id': 7}))
        self.assertEqual(await task, {'scene_id': 7})

    async def nothing_arrives(self, socket) -> None:
        """A tab that should stay quiet: give the loop a chance to deliver, then look."""
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.05)

    async def test_a_write_reaches_the_bound_browser_and_nobody_else(self):
        # §5.2/§6: the fan-out branch of `dispatch` is gone. Two tabs used to race and the winner
        # decided what "the scene" is for every later CAS -- 坑 32. Now one browser holds the
        # session, and the other one is a viewer: it gets broadcasts, never a browser.request.
        first = await self.connect('b1')
        second = await self.connect('b2')
        task = asyncio.create_task(self.gateway.dispatch('addObject', {'object_name_in_scene': 'cube'}))
        frame = await first.receive_json()
        self.assertEqual(frame['payload']['object_name_in_scene'], 'cube')
        await self.nothing_arrives(second)
        await first.send_json(response(frame['request_id'], payload={'object_id_in_scene': 3}))
        self.assertEqual(await task, {'object_id_in_scene': 3})
        # A second answer for a settled request is dropped rather than applied twice, even when
        # the tab that sends it was never asked (§5.2).
        with self.assertLogs('web_frontend.local_gateway', level='WARNING') as logs:
            await second.send_json(response(frame['request_id'], payload={'object_id_in_scene': 99}))
            for _ in range(100):
                if logs.output:
                    break
                await asyncio.sleep(0.005)
        self.assertIn('stale or unknown', logs.output[0])

    async def test_a_request_named_at_a_viewer_is_a_refusal_not_a_redirect(self):
        bound = await self.connect('b1')
        viewer = await self.connect('b2')
        with self.assertRaises(web.HTTPError) as caught:
            await self.gateway.dispatch('addObject', {'object_name_in_scene': 'cube'}, browser_id='b2')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(json.loads(caught.exception.body)['error']['code'], 'NO_BROWSER_CONNECTED')
        await self.nothing_arrives(viewer)
        await self.nothing_arrives(bound)

    async def test_silent_browser_times_out_and_frees_the_slot(self):
        socket = await self.connect()
        with self.assertRaises(web.HTTPError) as caught:
            await self.gateway.dispatch('getWorld', {})
        self.assertEqual(caught.exception.status, 504)
        body = json.loads(caught.exception.body)
        self.assertEqual(body['error']['code'], 'BROWSER_TIMEOUT')
        self.assertTrue(body['error']['request_id'])
        # Both pending slots must be reusable after a timeout (max_pending_requests is 2).
        # The unanswered frame of the timed-out request is still queued, so match by payload.
        for expected in (1, 2):
            task = asyncio.create_task(self.gateway.dispatch('getWorld', {'n': expected}))
            while True:
                frame = await socket.receive_json()
                if frame['payload'].get('n') == expected:
                    break
            await socket.send_json(response(frame['request_id'], payload={'n': expected}))
            self.assertEqual(await task, {'n': expected})

    async def test_render_action_uses_the_capture_timeout(self):
        socket = await self.connect()
        task = asyncio.create_task(self.gateway.dispatch('captureScene', {'id': 1}))
        frame = await socket.receive_json()
        await asyncio.sleep(0.4)
        await socket.send_json(response(frame['request_id'], payload={'width': 64}))
        self.assertEqual(await task, {'width': 64})

    async def test_connection_without_hello_gets_no_greeting_and_no_dispatch(self):
        socket = await self.client.ws_connect('/events')
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(socket.receive(), 0.1)
        with self.assertRaises(web.HTTPError) as caught:
            await self.gateway.dispatch('getWorld', {})
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(json.loads(caught.exception.body)['error']['code'], 'NO_BROWSER_CONNECTED')

    async def test_directed_request_to_an_absent_browser_is_503(self):
        await self.connect('present')
        with self.assertRaises(web.HTTPError) as caught:
            await self.gateway.dispatch('getWorld', {}, browser_id='absent')
        self.assertEqual(caught.exception.status, 503)

    async def test_unknown_action_is_rejected(self):
        await self.connect()
        with self.assertRaises(web.HTTPError) as caught:
            await self.gateway.dispatch('deleteEverything', {})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(json.loads(caught.exception.body)['error']['code'], 'INVALID_PARAM')

    async def test_binary_frame_is_paired_with_the_request_frame(self):
        socket = await self.connect()
        data = b'TRAJ' + bytes(range(256)) * 4
        task = asyncio.create_task(self.gateway.dispatch('updateTraj', {'fps': 60}, binary=data))
        frame = await socket.receive_json()
        self.assertTrue(frame['expects_binary'])
        self.assertEqual(frame['payload']['byte_length'], len(data))
        self.assertEqual(frame['payload']['fps'], 60)
        message = await socket.receive()
        self.assertEqual(message.type, WSMsgType.BINARY)
        self.assertEqual(message.data, data)
        await socket.send_json(response(frame['request_id'], payload={'total_frames': 720}))
        self.assertEqual(await task, {'total_frames': 720})

    async def test_browser_failure_maps_its_code_to_an_http_status(self):
        socket = await self.connect()
        task = asyncio.create_task(self.gateway.dispatch('updateItem', {'id': 4}))
        frame = await socket.receive_json()
        await socket.send_json(response(frame['request_id'], ok=False,
                                        error={'code': 'VERSION_CONFLICT', 'message': 'scene is v9'}))
        with self.assertRaises(web.HTTPError) as caught:
            await task
        self.assertEqual(caught.exception.status, 409)
        body = json.loads(caught.exception.body)
        self.assertEqual(body['error']['code'], 'VERSION_CONFLICT')
        self.assertEqual(body['error']['request_id'], frame['request_id'])

    async def test_pending_cap_is_429(self):
        socket = await self.connect()
        pending = [asyncio.create_task(self.gateway.dispatch('addObject', {'i': index}))
                   for index in (1, 2)]
        frames = [await socket.receive_json() for _ in range(2)]
        with self.assertRaises(web.HTTPError) as caught:
            await self.gateway.dispatch('addObject', {'i': 3})
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(json.loads(caught.exception.body)['error']['code'], 'TOO_MANY_REQUESTS')
        for frame in frames:
            await socket.send_json(response(frame['request_id'], payload={'ok': True}))
        self.assertEqual([await task for task in pending], [{'ok': True}, {'ok': True}])

    async def test_state_sync_caches_the_world_and_broadcasts_scene_applied(self):
        source = await self.connect('a')
        watcher = await self.connect('b')
        world = {'scene_schema_version': 2, 'scene_id': 3, 'scene_name': 'dep',
                 'scene_version': 5, 'objects': [{'object_id_in_scene': 0}]}
        await source.send_json({'type': 'browser.state_sync',
                                'payload': {'world': world, 'report_seq': 7}})
        frame = await watcher.receive_json()
        digest = frame.pop('scene_digest')
        # §5.4: the announcement carries the whole four-tuple, the persisted phase and the session it
        # landed in, and it echoes back the reporting tab's own number (Q147) -- here to every
        # connection, because a watcher cannot tell an acknowledgement from a notice it must act on.
        self.assertEqual(frame, {'type': 'scene.applied', 'scene_id': 3, 'scene_version': 5,
                                 'scene_sub_version': 0, 'session_phase': 'setup',
                                 'session_id': None,
                                 'action': 'updateWorld', 'source_request_id': None,
                                 'ack_report_seq': 7})
        # ...and the digest is the one `digest.py` computes for that exact document (§3 单点).
        self.assertEqual(digest, scene_digest(world))
        self.assertEqual(self.gateway.world_cache, world)

    async def test_the_reporting_socket_gets_its_own_acknowledgement_back(self):
        # Q147: the frame goes to every connection and the reporter picks itself out by the number
        # it sent, so the echo has to reach the socket that sent the report -- that tab is the one
        # that must not re-read and roll the user's screen back to a scene it already left.
        source = await self.connect('a')
        world = {'scene_id': 12, 'scene_version': 1, 'objects': []}
        await source.send_json({'type': 'browser.state_sync',
                                'payload': {'world': world, 'report_seq': 3}})
        frame = await source.receive_json()
        self.assertEqual(frame['type'], 'scene.applied')
        self.assertEqual(frame['ack_report_seq'], 3)
        self.assertEqual(frame['scene_id'], 12)

    async def test_a_report_without_a_number_is_echoed_as_no_acknowledgement(self):
        # A peer that has not learned to number yet must not get a fake ack: `None` is what tells the
        # reader this landing came from a report at all, so an unnumbered report is a plain notice.
        source = await self.connect('a')
        watcher = await self.connect('b')
        await source.send_json({'type': 'browser.state_sync',
                                'payload': {'world': {'scene_id': 13, 'scene_version': 1,
                                                      'objects': []}}})
        frame = await watcher.receive_json()
        self.assertEqual(frame['type'], 'scene.applied')
        self.assertIsNone(frame['ack_report_seq'])

    async def test_a_non_integer_report_number_is_not_echoed(self):
        source = await self.connect('a')
        watcher = await self.connect('b')
        await source.send_json({'type': 'browser.state_sync',
                                'payload': {'world': {'scene_id': 14, 'scene_version': 1,
                                                      'objects': []}, 'report_seq': 'seven'}})
        frame = await watcher.receive_json()
        self.assertEqual(frame['type'], 'scene.applied')
        self.assertIsNone(frame['ack_report_seq'])

    async def test_state_sync_larger_than_the_default_frame_cap_is_accepted(self):
        # 坑1: aiohttp caps inbound WS frames at 4 MiB unless max_msg_size=0.
        source = await self.connect()
        world = {'scene_version': 1, 'scene_name': 'x' * (5 * 1024 * 1024), 'objects': []}
        await source.send_json({'type': 'browser.state_sync', 'payload': {'world': world}})
        for _ in range(200):
            if self.gateway.world_cache:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(self.gateway.world_cache['scene_name']), 5 * 1024 * 1024)

    async def test_malformed_frames_are_ignored_not_fatal(self):
        socket = await self.connect()
        watcher = await self.connect('watcher')
        await socket.send_str('not json at all')
        await socket.send_json({'type': 'browser.response', 'request_id': 'no-such-request', 'ok': True})
        await socket.send_json({'type': 'browser.state_sync', 'payload': {}})
        await socket.send_bytes(b'unsolicited binary')
        task = asyncio.create_task(self.gateway.dispatch('getWorld', {}))
        frame = await socket.receive_json()
        await socket.send_json(response(frame['request_id'], payload={'scene_version': 2}))
        self.assertEqual(await task, {'scene_version': 2})
        self.assertIsNone(self.gateway.world_cache)
        self.assertFalse(watcher.closed)

    async def test_closing_socket_unregisters_the_browser(self):
        socket = await self.connect('bye')
        await socket.close()
        for _ in range(200):
            if 'bye' not in self.gateway.browsers:
                break
            await asyncio.sleep(0.005)
        self.assertNotIn('bye', self.gateway.browsers)

    async def next_request_frame(self, socket):
        """Read until a browser.request, skipping the scene.applied frames of state_sync."""
        while True:
            frame = await socket.receive_json()
            if frame.get('type') == 'browser.request':
                return frame

    async def sync_world(self, socket, world):
        await socket.send_json({'type': 'browser.state_sync', 'payload': {'world': world}})
        for _ in range(200):
            if self.gateway.world_cache:
                return
            await asyncio.sleep(0.005)
        self.fail('browser.state_sync did not fill the cache')

    async def test_get_world_route_serves_the_live_world_of_a_browser(self):
        socket = await self.connect()
        task = asyncio.create_task(self.client.get('/getWorld'))
        frame = await self.next_request_frame(socket)
        self.assertEqual(frame['action'], 'getWorld')
        world = {'scene_schema_version': 2, 'scene_id': 4, 'scene_version': 2,
                 'objects': [{'object_id_in_scene': 0}]}
        await socket.send_json(response(frame['request_id'], payload={'world': world}))
        result = await task
        self.assertEqual(result.status, 200)
        self.assertEqual(await result.json(), world)
        self.assertEqual(result.headers['X-World-Source'], 'live')
        self.assertIn('application/json', result.headers['Content-Type'])
        # §1.4: the freshest world the browsers showed also feeds the cache.
        self.assertEqual(self.gateway.world_cache, world)

    async def test_a_warm_cache_does_not_answer_get_world_after_the_browser_leaves(self):
        # 流程文档 §5.1 (2026-11-05): the cached document stays as §1.4's record of what this gateway
        # saw, and answers nothing. A late report can move the persisted identity onto an older
        # scene, so a reader that gets that older scene back with a 200 cannot tell it from the live
        # one -- which is how a scene switch used to end up as two sides agreeing on the old scene.
        world = {'scene_id': 9, 'scene_version': 1, 'objects': []}
        socket = await self.connect()
        await self.sync_world(socket, world)
        await socket.close()
        for _ in range(200):
            if not self.gateway.browsers:
                break
            await asyncio.sleep(0.005)
        result = await self.client.get('/getWorld')
        self.assertEqual(result.status, 503)
        self.assertEqual((await result.json())['error']['code'], 'NO_BROWSER_CONNECTED')
        self.assertEqual(self.gateway.world_cache, world)
        self.assertNotIn('X-World-Cache-Age', result.headers)

    async def test_a_silent_browser_is_504_even_with_a_warm_cache(self):
        world = {'scene_id': 9, 'scene_version': 1, 'objects': []}
        socket = await self.connect()
        await self.sync_world(socket, world)
        task = asyncio.create_task(self.client.get('/getWorld'))
        await self.next_request_frame(socket)  # never answered -> 504 inside dispatch
        result = await task
        self.assertEqual(result.status, 504)
        self.assertEqual((await result.json())['error']['code'], 'BROWSER_TIMEOUT')
        self.assertNotIn('X-World-Source', result.headers)

    async def test_live_get_world_emits_the_full_header_set_from_one_read(self):
        # §5.1: a live reading carries the whole identity of the document it returns, and the
        # X-Scene-Digest header must be the digest of *this* body -- not a stored value paired to
        # another moment. There is no other kind of reading any more (§5.1, 2026-11-05).
        socket = await self.connect()
        task = asyncio.create_task(self.client.get('/getWorld'))
        frame = await self.next_request_frame(socket)
        world = {'scene_schema_version': 2, 'scene_id': 4, 'scene_version': 3, 'scene_sub_version': 2,
                 'objects': [{'object_id_in_scene': 0}, {'object_id_in_scene': 5}]}
        await socket.send_json(response(frame['request_id'], payload={
            'world': world, 'scene_id': 4, 'scene_version': 3, 'scene_sub_version': 2,
            'playback_state': 'paused'}))
        result = await task
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers['X-World-Source'], 'live')
        self.assertEqual(result.headers['X-Scene-ID'], '4')
        self.assertEqual(result.headers['X-Scene-Version'], '3')
        self.assertEqual(result.headers['X-Scene-Sub-Version'], '2')
        self.assertEqual(result.headers['X-Scene-Digest'], scene_digest(world))
        self.assertEqual(result.headers['X-Session-Phase'], 'setup')
        self.assertEqual(result.headers['X-Browser-ID'], 'browser-1')
        self.assertEqual(result.headers['X-Playback-State'], 'paused')
        self.assertNotIn('X-World-Cache-Age', result.headers)
        # §6 event ②: a live read pushes the water mark over the ids it saw (max id 5 -> next 6).
        self.assertEqual(self.gateway.state.next_object_id, 6)

    async def test_get_world_refuses_a_reply_that_disagrees_with_its_own_document(self):
        # §5.1/§3: header, body and the flat fields must describe the same read. A browser that
        # reports one version beside a document holding another is broken, and serving either
        # answer would be a guess -- so the whole reading is voided with 502 GATEWAY_FRAMING_ERROR.
        socket = await self.connect()
        task = asyncio.create_task(self.client.get('/getWorld'))
        frame = await self.next_request_frame(socket)
        world = {'scene_id': 4, 'scene_version': 3, 'objects': [{'object_id_in_scene': 0}]}
        await socket.send_json(response(frame['request_id'], payload={
            'world': world, 'scene_id': 4, 'scene_version': 9, 'scene_sub_version': 0}))
        result = await task
        self.assertEqual(result.status, 502)
        self.assertEqual((await result.json())['error']['code'], 'GATEWAY_FRAMING_ERROR')

    async def test_get_world_route_is_503_without_a_browser_or_a_cache(self):
        result = await self.client.get('/getWorld')
        self.assertEqual(result.status, 503)
        self.assertEqual((await result.json())['error']['code'], 'NO_BROWSER_CONNECTED')
    async def test_get_world_route_reports_the_browser_error_verbatim(self):
        # A browser that answers is answering: its own code travels to the caller, and there has
        # never been a cache to paper over it (the two used to differ only when a cache was warm --
        # that branch is gone, so this is the whole story).
        socket = await self.connect()
        task = asyncio.create_task(self.client.get('/getWorld'))
        frame = await self.next_request_frame(socket)
        await socket.send_json(response(frame['request_id'], ok=False,
                                        error={'code': 'SCENE_NOT_LOADED', 'message': 'no world yet'}))
        result = await task
        self.assertEqual(result.status, 503)
        self.assertEqual((await result.json())['error']['code'], 'SCENE_NOT_LOADED')

    async def test_get_world_route_needs_a_world_object_from_the_browser(self):
        socket = await self.connect()
        task = asyncio.create_task(self.client.get('/getWorld'))
        frame = await self.next_request_frame(socket)
        await socket.send_json(response(frame['request_id'], payload={'scene_version': 3}))
        result = await task
        self.assertEqual(result.status, 502)
        self.assertEqual((await result.json())['error']['code'], 'GATEWAY_FRAMING_ERROR')


if __name__ == '__main__':
    unittest.main()
