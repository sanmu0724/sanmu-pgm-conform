import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from library_service import API_REVISION, Client, LibraryError, create_server, read_or_create_token


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.token = "a-test-token-with-at-least-24-characters"
        self.server = create_server(Path(self.directory.name) / "catalogue.sqlite3", token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = Client(self.url, self.token, "DIT")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.store.close()
        self.directory.cleanup()

    def request(self, payload, token=None):
        request = urllib.request.Request(self.url + "/rpc", data=payload,
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + (token or self.token)})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_health_authentication_and_real_rpc(self):
        with urllib.request.urlopen(self.url + "/health", timeout=3) as response:
            self.assertEqual(json.load(response), {"status": "ok", "version": 1, "api_revision": API_REVISION})
        value = self.client.call("create_production", name="示例剧目")
        self.assertEqual(self.client.call("list_productions")[0]["id"], value["id"])
        with self.assertRaises(LibraryError) as caught:
            Client(self.url, "incorrect-token", "导演").call("list_productions")
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(len(self.server.store.list_productions()), 1)

    def test_real_http_concurrent_edit_conflict(self):
        p = self.client.call("create_production", name="示例剧目")
        s = self.client.call("save_session", production_id=p["id"], name="第三场", config={"fps": 25}, segments=[])
        barrier = threading.Barrier(2)
        def edit(actor):
            barrier.wait()
            try:
                return Client(self.url, self.token, actor).call("save_session", production_id=p["id"], name=actor,
                                                               session_id=s["id"], config={"actor": actor}, expected_revision=1)["name"]
            except LibraryError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ["导演", "场记"]))
        self.assertEqual(results.count("conflict"), 1)
        final = self.client.call("get_session", session_id=s["id"])
        self.assertEqual(final["revision"], 2)
        self.assertEqual(final["name"], final["config"]["actor"])
        audit = self.client.call("audit", production_id=p["id"])
        edits = [item for item in audit if item["action"] == "update_session"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["actor"], final["name"])

    def test_whole_pgm_match_removal_rpc_retains_media_and_reports_stale_snapshot(self):
        p = self.client.call('create_production', name='错配删除')
        s = self.client.call('save_session', production_id=p['id'], name='day01_分镜头',
                             config={'pgm': '/NAS/Day1/001.mov'})
        self.client.call('put_script', production_id=p['id'], title='台本', lines=[dict(text='你来啦。')])
        line = self.client.call('list_script', production_id=p['id'])[0]
        speech = self.client.call('add_transcript', session_id=s['id'], media_path='/NAS/Day1/001.mov',
                                 segments=[dict(start_seconds=1, end_seconds=2, text='你来啦。')])
        self.client.call('replace_candidates', production_id=p['id'], candidates=[dict(
            transcript_id=speech['transcript_ids'][0], script_line_id=line['id'], score=.9)])
        candidate = self.client.call('list_candidates', production_id=p['id'])[0]
        params = dict(production_id=p['id'], session_id=s['id'], media_path='/NAS/Day1/001.mov',
                      expected_session_revision=s['revision'],
                      expected_candidate_revisions={candidate['id']: candidate['revision']},
                      expected_reference_revision=0)
        result = self.client.call('clear_pgm_matches', **params)
        self.assertEqual(result['cleared_count'], 1)
        self.assertEqual(self.client.call('list_candidates', production_id=p['id'])[0]['status'], 'rejected')
        self.assertEqual(self.client.call('list_transcripts', production_id=p['id'])[0]['id'], speech['transcript_ids'][0])
        with self.assertRaises(LibraryError) as caught:
            self.client.call('clear_pgm_matches', **params)
        self.assertEqual(caught.exception.code, 'conflict')

    def test_bad_rpc_envelope_and_methods_rejected(self):
        for payload in (b"not JSON", b"[]", b"{}", b'{"method":"execute","params":{},"actor":"x"}',
                        b'{"method":"list_productions","params":[],"actor":"x"}', b'{"method":{},"actor":"x"}'):
            code, value = self.request(payload)
            self.assertEqual(code, 400)
            self.assertEqual(value["error"]["code"], "invalid")
            self.assertNotIn("Traceback", json.dumps(value))
        code, value = self.request(b'{"method":"list_productions","params":{},"actor":"x"}', token="bad")
        self.assertEqual(code, 401)

    def test_error_codes_reach_client_and_timeout_is_bounded(self):
        with self.assertRaises(LibraryError) as caught:
            self.client.call("get_session", session_id="does-not-exist")
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(LibraryError) as caught:
            self.client.call("audit", production_id="missing", limit=-1)
        self.assertEqual(caught.exception.code, "invalid")
        for url in ("file:///tmp/test", "http://user:pass@localhost", "http://localhost/rpc", "http://localhost?x=1"):
            with self.assertRaises(LibraryError):
                Client(url, self.token)
        for timeout in (0, -1, 121):
            with self.assertRaises(LibraryError):
                Client(self.url, self.token, timeout=timeout)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(LibraryError) as caught:
            Client(f"http://127.0.0.1:{unused_port}", self.token, timeout=.2).call("list_productions")
        self.assertEqual(caught.exception.code, "network")

    def test_token_persistence_permissions_and_ready_no_secret(self):
        path = Path(self.directory.name) / "connection.token"
        token = read_or_create_token(path)
        self.assertGreaterEqual(len(token), 24)
        self.assertEqual(token, read_or_create_token(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        bad = Path(self.directory.name) / "bad.token"
        bad.write_text("short")
        with self.assertRaises(LibraryError):
            read_or_create_token(bad)
        symlink = Path(self.directory.name) / "symlink.token"
        symlink.symlink_to(path)
        with self.assertRaises(LibraryError):
            read_or_create_token(symlink)

    def test_cli_ready_and_rpc_no_media_copy(self):
        ready_path = Path(self.directory.name) / "ready.json"
        token_path = Path(self.directory.name) / "cli.token"
        command = [sys.executable, str(Path(__file__).with_name("library_service.py")), "--db",
                   str(Path(self.directory.name) / "cli.sqlite3"), "--token-file", str(token_path), "--ready-file", str(ready_path)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # CLI emits exactly one startup line; communicate after terminate prevents a blocked reader.
            line = process.stdout.readline()
            self.assertTrue(line, process.stderr.read() if process.poll() is not None else "no ready message")
            ready = json.loads(line)
            self.assertEqual(json.loads(ready_path.read_text()), ready)
            self.assertEqual(ready['api_revision'], API_REVISION)
            self.assertNotIn("token", ready)
            client = Client(ready["url"], token_path.read_text().strip(), "场记")
            p = client.call("create_production", name="测试")
            s = client.call("save_session", production_id=p["id"], name="Day1", config={"proxy": "/NAS/absent/proxy.mov"})
            client.call("add_transcript", session_id=s["id"], media_path="/NAS/absent/proxy.mov",
                        segments=[{"start_seconds": 0, "end_seconds": 1, "text": "你好"}])
            self.assertEqual(client.call("list_transcripts", production_id=p["id"])[0]["media_path"], "/NAS/absent/proxy.mov")
            self.assertFalse((Path(self.directory.name) / "proxy.mov").exists())
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
