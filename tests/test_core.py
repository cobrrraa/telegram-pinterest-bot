import asyncio
import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

import csv_export
from pin_queue import PinQueue
from pinterest import MakeStoppedError, PinterestClient


class CoreTests(unittest.TestCase):
    def test_queue_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'queue.json'
            queue = PinQueue(path)
            batch = queue.add_batch(1, 2, 'board', 'Board', ['a', 'b'])
            queue.paused = True
            queue.save()
            restored = PinQueue(path)
            self.assertTrue(restored.paused)
            self.assertEqual(restored.remaining(batch), 2)

    def test_csv_uses_configured_host_and_skips_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = root / 'photos'
            photos.mkdir()
            Image.new('RGB', (800, 1000)).save(photos / 'one.jpg')
            (photos / 'two.jpg').write_bytes((photos / 'one.jpg').read_bytes())
            args = ['csv_export.py', str(photos), '--board', 'Board', '--out',
                    str(root / 'out'), '--base-url', 'https://photos.example.com/p']
            with patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
                csv_export.main()
            with (root / 'out' / 'pins_001.csv').open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]['Media URL'].startswith('https://photos.example.com/p/'))

    def test_make_success_and_ambiguous_acceptance(self):
        async def run():
            # Bypass construction of a network client: tests must never contact Make.
            client = PinterestClient.__new__(PinterestClient)
            client.webhook_url = 'https://example.com/webhook'
            client.secret = 'test-only'
            client.path_prefix = '/v5'
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(201, json={'id': '123'})))
            try:
                pin = await client.create_pin('board', b'test', 'image/jpeg')
                self.assertEqual(pin['id'], '123')
            finally:
                await client.close()
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text='Accepted')))
            try:
                with self.assertRaises(MakeStoppedError) as caught:
                    await client.create_pin('board', b'test', 'image/jpeg')
                self.assertTrue(caught.exception.queued_in_make)
            finally:
                await client.close()
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
