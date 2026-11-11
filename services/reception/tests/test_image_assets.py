import base64
import io
import os
import time
import unittest
from unittest.mock import patch

from PIL import Image
from reception_service import image_assets as assets


def image_bytes(fmt="PNG", size=(32, 24), **kwargs):
    out = io.BytesIO()
    Image.new("RGB", size, "red").save(out, format=fmt, **kwargs)
    return out.getvalue()


class ImageAssetsTests(unittest.TestCase):
    def test_decode_formats_and_sanitized_preview(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            with self.subTest(fmt=fmt):
                exif = Image.Exif()
                exif[270] = "private metadata"
                data = image_bytes(fmt, (1500, 30), exif=exif)
                result = assets.decode_image(data, mime)
                self.assertEqual((result["width"], result["height"], result["size"]), (1500, 30, len(data)))
                self.assertEqual(base64.b64decode(result["original_b64"]), data)
                preview = base64.b64decode(result["preview_data_url"].split(",", 1)[1])
                self.assertLessEqual(len(preview), 1024 * 1024)
                with Image.open(io.BytesIO(preview)) as decoded:
                    decoded.load()
                    self.assertLessEqual(max(decoded.size), 1280)
                    self.assertFalse(decoded.getexif())

    def test_invalid_mismatch_size_and_dimensions(self):
        for data, mime in ((b"bad secret", "image/png"), (image_bytes(), "image/jpeg"),
                           (b"x" * (assets.MAX_BYTES + 1), "image/png"),
                           (image_bytes(size=(8193, 1)), "image/png"),
                           (image_bytes(size=(5000, 4001)), "image/png"),
                           (image_bytes()[:-15], "image/png")):
            with self.subTest(length=len(data)):
                with self.assertRaises(assets.ImageAssetError) as caught:
                    assets.decode_image(data, mime)
                self.assertNotIn("secret", str(caught.exception))

    def test_animation_rejected(self):
        out = io.BytesIO()
        first = Image.new("RGB", (3, 3), "red")
        first.save(out, "PNG", save_all=True, append_images=[Image.new("RGB", (3, 3), "blue")], duration=100)
        with self.assertRaises(assets.ImageAssetError):
            assets.decode_image(out.getvalue(), "image/png")

    def test_fetch_allowlist_and_private_dns(self):
        data = image_bytes()
        seen = []
        def transport(host, path, addresses, deadline):
            seen.append((host, path, addresses))
            return "image/png", data
        resolve = lambda host, deadline: ["8.8.8.8"]
        for url in ("https://clouddisk.alibaba.com/picture.htm?token=private", "https://img.alicdn.com/a"):
            self.assertEqual(assets.fetch_image(url, resolver=resolve, transport=transport)["mime"], "image/png")
        self.assertEqual(seen[0][1], "/picture.htm?token=private")
        for url in ("https://alibaba.com/a", "https://alicdn.com/a", "https://badalicdn.com/a", "https://img.alicdn.com.evil/a", "https://user:secret@img.alicdn.com/a", "http://img.alicdn.com/a"):
            with self.subTest(url=url), self.assertRaises(assets.ImageAssetError) as caught:
                assets.fetch_image(url, resolver=resolve, transport=transport)
            self.assertNotIn("secret", str(caught.exception))
        for addresses in (["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["::ffff:8.8.8.8"], ["not an ip"]):
            with self.subTest(addresses=addresses), self.assertRaises(assets.ImageAssetError):
                assets.fetch_image("https://img.alicdn.com/a", resolver=lambda h, d: addresses, transport=transport)

    def test_timeout_and_exception_are_sanitized(self):
        def timeout(host, deadline):
            raise TimeoutError("secret url?token=123")
        with self.assertRaises(assets.ImageAssetError) as caught:
            assets.fetch_image("https://img.alicdn.com/a?token=123", resolver=timeout)
        self.assertNotIn("123", str(caught.exception))
        with patch.object(assets, "DEADLINE_SECONDS", -1), self.assertRaises(assets.ImageAssetError):
            assets.fetch_image("https://img.alicdn.com/a", resolver=lambda h, d: ["8.8.8.8"])

    def test_redirects_are_bounded_and_each_destination_is_validated(self):
        hosts = []
        deadlines = []
        target = 'https://hz-icbuim-file.oss-cn-beijing.aliyuncs.com/photo.png?signature=private'
        def transport(host, path, addresses, deadline):
            hosts.append(host)
            deadlines.append(deadline)
            if host == 'clouddisk.alibaba.com':
                raise assets._ImageRedirect(target)
            return 'image/png', image_bytes()
        image = assets.fetch_image('https://clouddisk.alibaba.com/a.htm',
            resolver=lambda h, d: ['8.8.8.8'], transport=transport)
        self.assertEqual(image['mime'], 'image/png')
        self.assertEqual(hosts, ['clouddisk.alibaba.com', 'hz-icbuim-file.oss-cn-beijing.aliyuncs.com'])
        self.assertEqual(len(set(deadlines)), 1)
        for location in ('https://other.oss-cn-beijing.aliyuncs.com/a.png', 'http://img.alicdn.com/a',
                         'https://user:secret@img.alicdn.com/a', 'https://127.0.0.1/a',
                         '\nhttps://img.alicdn.com/a', 'https://img.alicdn.com/a#fragment'):
            def redirect(*args):
                raise assets._ImageRedirect(location)
            with self.subTest(location=location), self.assertRaises(assets.ImageAssetError):
                assets.fetch_image('https://clouddisk.alibaba.com/a', resolver=lambda h, d: ['8.8.8.8'], transport=redirect)
        calls = []
        def loop(*args):
            calls.append(1)
            raise assets._ImageRedirect('/again')
        with self.assertRaises(assets.ImageAssetError):
            assets.fetch_image('https://clouddisk.alibaba.com/a', resolver=lambda h, d: ['8.8.8.8'], transport=loop)
        self.assertEqual(len(calls), 3)
        with self.assertRaises(assets.ImageAssetError):
            assets.fetch_image('https://clouddisk.alibaba.com/a',
                resolver=lambda h, d: ['8.8.8.8'] if h == 'clouddisk.alibaba.com' else ['10.0.0.1'], transport=transport)

    def test_transport_surfaces_redirect_and_sends_no_credentials(self):
        requests = []
        class Response:
            status = 302
            def getheader(self, name):
                return 'https://img.alicdn.com/a.png' if name == 'Location' else None
        class Connection:
            sock = None
            def __init__(self, host, address, deadline):
                pass
            def request(self, method, path, headers):
                requests.append((method, path, headers))
            def getresponse(self):
                return Response()
            def close(self):
                pass
        with patch.object(assets, "_PinnedHTTPS", Connection), patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pass@localhost", "HTTP_COOKIE": "private"}):
            with self.assertRaises(assets._ImageRedirect):
                assets._transport("img.alicdn.com", "/a", ["8.8.8.8"], time.monotonic() + 2)
        self.assertEqual(len(requests), 1)
        headers = {key.lower(): value for key, value in requests[0][2].items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertNotIn("proxy-authorization", headers)


if __name__ == "__main__":
    unittest.main()
