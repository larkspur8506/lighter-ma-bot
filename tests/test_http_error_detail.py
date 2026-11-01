"""`_http_error_detail` 与 `_default_http_json` 的告警降噪测试。

2026-11-01 Lighter 网络升级期间，503 维护页的整段 HTML 被原样塞进
`executor_error` 告警——这类页面必须被压成一行、只留标题。
"""

import io
import unittest
import urllib.error
import urllib.request
from unittest import mock

from app.executor import (ReaderError, _clean_alert_text, _default_http_json,
                          _http_error_detail)

MAINTENANCE_PAGE = (
    "<html>\r\n<head><title>503 Service Temporarily Unavailable</title></head>\r\n"
    "<body><center><h1>503 Service Temporarily Unavailable</h1></center></body>\r\n</html>"
)


def _raise_http_error(url: str, timeout: float = 20.0, **_kw):  # noqa: ARG001
    raise urllib.error.HTTPError(
        url, 503, "Service Temporarily Unavailable", hdrs=None,
        fp=io.BytesIO(MAINTENANCE_PAGE.encode("utf-8")))


class HttpErrorDetailTests(unittest.TestCase):

    def test_html_page_collapses_to_title(self):
        self.assertEqual(
            _http_error_detail(MAINTENANCE_PAGE),
            "HTML 错误页：503 Service Temporarily Unavailable")

    def test_html_without_title_is_tagged_only(self):
        self.assertEqual(_http_error_detail("<html><body>boom</body></html>"),
                         "HTML 错误页")

    def test_json_body_is_kept_and_collapsed(self):
        body = '{\r\n  "code": 20558,\r\n  "message": "restricted"\r\n}'
        self.assertEqual(_http_error_detail(body),
                         '{ "code": 20558, "message": "restricted" }')

    def test_truncation_still_applies(self):
        self.assertEqual(len(_http_error_detail("x" * 9000)), 200)


class CleanAlertTextTests(unittest.TestCase):
    """`_alert`/`_note_failure` 的告警兜底：SDK 异常携带 HTML 页时压成标题。"""

    def test_multiline_alert_text_is_untouched(self):
        text = "第一行\n- 要点 A\n- 要点 B"
        self.assertEqual(_clean_alert_text(text), text)

    def test_html_page_is_reduced_to_title(self):
        text = "SOL 止损补挂/换单失败：ApiException (502)\n" + MAINTENANCE_PAGE
        cleaned = _clean_alert_text(text)
        self.assertIn("HTML 错误页：503 Service Temporarily Unavailable", cleaned)
        self.assertNotIn("<html", cleaned.lower())

    def test_overlong_text_is_truncated(self):
        self.assertEqual(len(_clean_alert_text("y" * 5000)), 801)


class DefaultHttpJsonTests(unittest.TestCase):

    def test_reader_error_carries_no_html_markup(self):
        with mock.patch.object(urllib.request, "urlopen", _raise_http_error):
            with self.assertRaises(ReaderError) as ctx:
                _default_http_json("https://mainnet.zklighter.elliot.ai/api/v1/account"
                                   "?by=index&value=764377")
        text = str(ctx.exception)
        self.assertIn("HTTP 503", text)
        self.assertIn("503 Service Temporarily Unavailable", text)
        self.assertNotIn("<html", text.lower())
        self.assertNotIn("<head>", text.lower())


if __name__ == "__main__":
    unittest.main()
