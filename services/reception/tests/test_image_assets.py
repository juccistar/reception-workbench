import base64
import io
import os
import time
import unittest
from unittest.mock import Mock, patch

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

    def test_transparent_previews_preserve_alpha_and_original(self):
        for mode, fmt, mime in (('RGBA', 'PNG', 'image/png'), ('LA', 'PNG', 'image/png'),
                                ('P', 'PNG', 'image/png'), ('RGBA', 'WEBP', 'image/webp')):
            with self.subTest(mode=mode, fmt=fmt):
                if mode == 'P':
                    image = Image.new('P', (20, 20), 0)
                    image.putpalette([255, 255, 255, 255, 255, 255] + [0] * 762)
                    image.paste(1, (5, 5, 15, 15))
                    image.info['transparency'] = 0
                else:
                    image = Image.new(mode, (20, 20), (255, 0) if mode == 'LA' else (255, 255, 255, 0))
                    image.paste((255, 255) if mode == 'LA' else (255, 255, 255, 255), (5, 5, 15, 15))
                stream = io.BytesIO()
                image.save(stream, format=fmt)
                original = stream.getvalue()
                result = assets.decode_image(original, mime)
                self.assertEqual(base64.b64decode(result['original_b64']), original)
                self.assertTrue(result['preview_data_url'].startswith('data:image/png;base64,'))
                preview = base64.b64decode(result['preview_data_url'].split(',', 1)[1])
                self.assertLessEqual(len(preview), assets.MAX_PREVIEW_BYTES)
                with Image.open(io.BytesIO(preview)) as decoded:
                    self.assertEqual(decoded.getchannel('A').getextrema(), (0, 255))
                    self.assertFalse(decoded.getexif())

    def test_legacy_preview_verifies_original_even_on_cache_hit(self):
        image = Image.new('RGBA', (4, 4), (255, 255, 255, 0))
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        asset = assets.decode_image(stream.getvalue(), 'image/png')
        asset['preview_data_url'] = 'data:image/jpeg;base64,b2xk'
        self.assertTrue(assets.derived_preview(asset).startswith('data:image/png;base64,'))
        asset['original_b64'] = base64.b64encode(image_bytes()).decode('ascii')
        with self.assertRaises(assets.ImageAssetError):
            assets.derived_preview(asset)

    def test_unreachable_first_cdn_address_uses_next_validated_address(self):
        first, second, tls, context = Mock(), Mock(), Mock(), Mock()
        first.connect.side_effect = OSError('IPv6 route unavailable')
        context.wrap_socket.return_value = tls
        deadline = time.monotonic() + 15
        with patch.object(assets.socket, 'socket', side_effect=[first, second]), \
                patch.object(assets.ssl, 'create_default_context', return_value=context):
            connection = assets._PinnedHTTPS('sc04.alicdn.com', '2606:4700:4700::1111', deadline)
            connection.addresses = ['2606:4700:4700::1111', '93.184.216.34']
            connection.connect()
            first.connect.assert_called_once_with(('2606:4700:4700::1111', 443))
            second.connect.assert_called_once_with(('93.184.216.34', 443))
            first.close.assert_called_once()
            context.wrap_socket.assert_called_once_with(second, server_hostname='sc04.alicdn.com', do_handshake_on_connect=False)
            self.assertIs(connection.sock, tls)
            self.assertEqual(connection.deadline, deadline)
            self.assertLessEqual(first.settimeout.call_args.args[0], 3)
            connection.close()

    def test_certificate_failure_never_falls_back_to_another_cdn_address(self):
        raw, context = Mock(), Mock()
        context.wrap_socket.side_effect = assets.ssl.SSLCertVerificationError(1, 'certificate invalid')
        with patch.object(assets.socket, 'socket', return_value=raw) as sockets, \
                patch.object(assets.ssl, 'create_default_context', return_value=context):
            connection = assets._PinnedHTTPS('sc04.alicdn.com', '93.184.216.34', time.monotonic() + 15)
            connection.addresses = ['93.184.216.34', '1.1.1.1']
            with self.assertRaises(assets.ssl.SSLCertVerificationError):
                connection.connect()
            self.assertEqual(sockets.call_count, 1)
            raw.close.assert_called_once()

    def test_binary_mime_requires_supported_magic_and_complete_image(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            data = image_bytes(fmt)
            with self.subTest(fmt=fmt):
                decoded = assets.decode_image(data, "APPLICATION/OCTET-STREAM")
                self.assertEqual(decoded["mime"], mime)
                self.assertEqual(base64.b64decode(decoded["original_b64"]), data)
                with self.assertRaises(assets.ImageAssetError) as caught:
                    assets.decode_image(data[:20], "application/octet-stream")
                self.assertEqual((caught.exception.stage, caught.exception.category), ("decode", "invalid_image"))
        for data in (b"<html>secret</html>", b"RIFFxxxxNOTW", b"\xff\xd8", b"GIF89a"):
            with self.subTest(data=data), self.assertRaises(assets.ImageAssetError) as caught:
                assets.decode_image(data, "application/octet-stream")
            self.assertEqual((caught.exception.stage, caught.exception.category), ("validation", "unsupported_type"))
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(assets.ImageAssetError) as caught:
            assets.decode_image(image_bytes(), "image/jpeg")
        self.assertEqual((caught.exception.stage, caught.exception.category), ("validation", "type_mismatch"))
        with self.assertRaises(assets.ImageAssetError):
            assets.decode_image(image_bytes(), "text/html")
        with self.assertRaises(assets.ImageAssetError) as caught:
            assets.decode_image(b"x" * (assets.MAX_BYTES + 1), "application/octet-stream")
        self.assertEqual((caught.exception.stage, caught.exception.category), ("validation", "size_limit"))
        with self.assertRaises(assets.ImageAssetError) as caught:
            assets.decode_image(image_bytes(size=(8193, 1)), "application/octet-stream")
        self.assertEqual((caught.exception.stage, caught.exception.category), ("validation", "dimensions"))

    def test_binary_mime_fetch_and_transport(self):
        data = image_bytes("WEBP")
        class Response:
            status = 200
            def __init__(self):
                self.body = io.BytesIO(data)
            def getheader(self, name, default=None):
                return {"Content-Type": "Application/Octet-Stream; charset=binary",
                        "Content-Length": str(len(data))}.get(name, default)
            def read(self, size):
                return self.body.read(size)
        class Connection:
            sock = None
            def __init__(self, host, address, deadline):
                pass
            def request(self, method, path, headers):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        with patch.object(assets, "_PinnedHTTPS", Connection):
            result = assets.fetch_image("https://img.alicdn.com/a?token=private",
                                        resolver=lambda h, d: ["8.8.8.8"])
        self.assertEqual(result["mime"], "image/webp")
        with self.assertRaises(assets.ImageAssetError) as caught:
            assets.fetch_image("https://img.alicdn.com/a?token=private",
                               resolver=lambda h, d: ["127.0.0.1"])
        self.assertEqual((caught.exception.stage, caught.exception.category), ("source", "unsafe_address"))
        self.assertNotIn("private", str(caught.exception))

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
        for mime in ("image/png", "application/octet-stream"):
            with self.subTest(mime=mime), self.assertRaises(assets.ImageAssetError) as caught:
                assets.decode_image(out.getvalue(), mime)
            self.assertEqual((caught.exception.stage, caught.exception.category), ("validation", "animation"))

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
