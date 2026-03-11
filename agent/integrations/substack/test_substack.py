"""Tests for substack.py."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from substack import html_to_prosemirror, _digest_title, SubstackError, SubstackApi, cmd_post


# ============================================================
# html_to_prosemirror
# ============================================================

class TestHtmlToProseMirror:
    def test_paragraph(self):
        nodes = html_to_prosemirror("<p>Hello</p>")
        assert len(nodes) == 1
        assert nodes[0]["type"] == "paragraph"
        assert nodes[0]["content"][0]["text"] == "Hello"

    def test_heading_levels(self):
        for level in (1, 2, 3):
            nodes = html_to_prosemirror(f"<h{level}>Title</h{level}>")
            assert nodes[0]["type"] == "heading"
            assert nodes[0]["attrs"]["level"] == level

    def test_bold(self):
        nodes = html_to_prosemirror("<p><strong>bold</strong></p>")
        text = nodes[0]["content"][0]
        assert text["text"] == "bold"
        assert {"type": "bold"} in text["marks"]

    def test_italic(self):
        nodes = html_to_prosemirror("<p><em>italic</em></p>")
        text = nodes[0]["content"][0]
        assert {"type": "italic"} in text["marks"]

    def test_link(self):
        nodes = html_to_prosemirror('<p><a href="https://example.com">link</a></p>')
        text = nodes[0]["content"][0]
        link_mark = next(m for m in text["marks"] if m["type"] == "link")
        assert link_mark["attrs"]["href"] == "https://example.com"

    def test_ordered_list(self):
        nodes = html_to_prosemirror("<ol><li>one</li><li>two</li></ol>")
        assert nodes[0]["type"] == "ordered_list"
        assert len(nodes[0]["content"]) == 2

    def test_unordered_list(self):
        nodes = html_to_prosemirror("<ul><li>a</li></ul>")
        assert nodes[0]["type"] == "bullet_list"

    def test_list_item_wraps_paragraph(self):
        nodes = html_to_prosemirror("<ul><li>text</li></ul>")
        item = nodes[0]["content"][0]
        assert item["type"] == "list_item"
        assert item["content"][0]["type"] == "paragraph"

    def test_no_marks_when_plain(self):
        nodes = html_to_prosemirror("<p>plain text</p>")
        text = nodes[0]["content"][0]
        assert "marks" not in text

    def test_full_digest(self):
        html = """<h2>Summary</h2>
        <ol><li>Item 1</li></ol>
        <h3>Details</h3>
        <p>Text with <strong>bold</strong> and <a href="https://t.me/ch/1">link</a>.</p>"""
        nodes = html_to_prosemirror(html)
        types = [n["type"] for n in nodes]
        assert types == ["heading", "ordered_list", "heading", "paragraph"]

    def test_b_and_i_tags(self):
        nodes = html_to_prosemirror("<p><b>bold</b> and <i>italic</i></p>")
        assert {"type": "bold"} in nodes[0]["content"][0]["marks"]
        assert {"type": "italic"} in nodes[0]["content"][2]["marks"]


# ============================================================
# _digest_title
# ============================================================

class TestDigestTitle:
    @patch("substack.datetime")
    def test_no_dates_gives_daily(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, tzinfo=timezone.utc)
        mock_dt.strptime = datetime.strptime
        title = _digest_title()
        assert title == "[Daily] AI Digest — March 11, 2026"

    def test_weekly_range_6_days(self):
        title = _digest_title(start_date="2026-03-04", end_date="2026-03-10")
        assert title.startswith("[Weekly]")

    def test_weekly_range_7_days(self):
        """Real workflow scenario: start=7 days ago, end=today."""
        title = _digest_title(start_date="2026-03-04", end_date="2026-03-11")
        assert title.startswith("[Weekly]")

    def test_single_day(self):
        title = _digest_title(start_date="2026-03-10", end_date="2026-03-10")
        assert title.startswith("[Daily]")

    def test_start_date_only(self):
        title = _digest_title(start_date="2026-03-10")
        assert "AI Digest" in title

    def test_long_range(self):
        title = _digest_title(start_date="2026-01-01", end_date="2026-03-10")
        assert title.startswith("AI Digest")


# ============================================================
# SubstackError
# ============================================================

class TestSubstackError:
    def test_json_errors(self):
        err = SubstackError(400, '{"errors": [{"msg": "bad"}]}')
        assert "bad" in str(err)

    def test_json_error_field(self):
        err = SubstackError(400, '{"error": "unauthorized"}')
        assert "unauthorized" in err.message

    def test_invalid_json(self):
        err = SubstackError(500, "not json")
        assert "Invalid response" in str(err)

    def test_empty_errors(self):
        err = SubstackError(400, '{"errors": []}')
        assert err.status_code == 400


# ============================================================
# SubstackApi
# ============================================================

class TestSubstackApi:
    def _mock_api(self, subdomain="howai", custom_domain=None):
        """Create a SubstackApi with mocked HTTP."""
        with patch("substack.requests") as mock_requests:
            session = MagicMock()
            mock_requests.Session.return_value = session

            profile = {
                "id": 42,
                "publicationUsers": [{
                    "publication": {
                        "subdomain": subdomain,
                        "custom_domain": custom_domain,
                        "custom_domain_optional": False if custom_domain else True,
                    }
                }]
            }

            profile_resp = MagicMock()
            profile_resp.status_code = 200
            profile_resp.json.return_value = profile
            session.get.return_value = profile_resp

            api = SubstackApi(
                cookies_string="connect.sid=abc123",
                publication_url=f"https://{subdomain}.substack.com",
            )
            return api, session

    def test_init_success(self):
        api, _ = self._mock_api()
        assert api.user_id == 42

    def test_init_custom_domain(self):
        api, _ = self._mock_api(subdomain="howai", custom_domain="blog.example.com")
        assert "blog.example.com" in api.publication_api

    def test_init_fallback_to_subdomain_api(self):
        with patch("substack.requests") as mock_requests:
            session = MagicMock()
            mock_requests.Session.return_value = session

            # First call fails (native API blocked)
            fail_resp = MagicMock()
            fail_resp.status_code = 403
            fail_resp.text = '{"error": "blocked"}'

            # Second call succeeds (subdomain API)
            profile = {"id": 42, "publicationUsers": [{"publication": {"subdomain": "howai", "custom_domain_optional": True}}]}
            ok_resp = MagicMock()
            ok_resp.status_code = 200
            ok_resp.json.return_value = profile

            session.get.side_effect = [fail_resp, ok_resp]

            api = SubstackApi(
                cookies_string="connect.sid=abc",
                publication_url="https://howai.substack.com",
            )
            assert api.user_id == 42
            assert "howai.substack.com" in api.base_url

    def test_init_fallback_primary_publication(self):
        with patch("substack.requests") as mock_requests:
            session = MagicMock()
            mock_requests.Session.return_value = session

            profile = {
                "id": 42,
                "publicationUsers": [{"publication": {"subdomain": "other"}}],
                "primaryPublication": {"subdomain": "howai", "custom_domain_optional": True},
            }
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = profile
            session.get.return_value = resp

            api = SubstackApi(
                cookies_string="connect.sid=abc",
                publication_url="https://howai.substack.com",
            )
            assert api.user_id == 42

    def test_init_fallback_is_primary(self):
        with patch("substack.requests") as mock_requests:
            session = MagicMock()
            mock_requests.Session.return_value = session

            profile = {
                "id": 42,
                "publicationUsers": [
                    {"publication": {"subdomain": "other"}},
                    {"is_primary": True, "publication": {"subdomain": "howai", "custom_domain_optional": True}},
                ],
            }
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = profile
            session.get.return_value = resp

            api = SubstackApi(
                cookies_string="connect.sid=abc",
                publication_url="https://howai.substack.com",
            )
            assert api.user_id == 42

    def test_init_no_publication_raises(self):
        with patch("substack.requests") as mock_requests:
            session = MagicMock()
            mock_requests.Session.return_value = session

            profile = {"id": 42, "publicationUsers": []}
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = profile
            session.get.return_value = resp

            with pytest.raises(SubstackError):
                SubstackApi(cookies_string="connect.sid=abc", publication_url="https://howai.substack.com")

    def test_handle_error(self):
        api, _ = self._mock_api()
        resp = MagicMock()
        resp.status_code = 404
        resp.text = '{"error": "not found"}'
        with pytest.raises(SubstackError):
            api._handle(resp)

    def test_create_draft(self):
        api, session = self._mock_api()
        draft_resp = MagicMock()
        draft_resp.status_code = 200
        draft_resp.json.return_value = {"id": "draft-1"}
        session.post.return_value = draft_resp

        result = api.create_draft(title="Test", subtitle="", body_content=[])
        assert result["id"] == "draft-1"

    def test_publish(self):
        api, session = self._mock_api()
        prepub_resp = MagicMock()
        prepub_resp.status_code = 200
        prepub_resp.json.return_value = {}
        publish_resp = MagicMock()
        publish_resp.status_code = 200
        publish_resp.json.return_value = {"slug": "test-post"}
        session.get.return_value = prepub_resp
        session.post.return_value = publish_resp

        result = api.publish("draft-1", send_email=False)
        assert result["slug"] == "test-post"

    def test_cookie_parsing(self):
        with patch("substack.requests") as mock_requests:
            session = MagicMock()
            mock_requests.Session.return_value = session

            profile = {"id": 42, "publicationUsers": [{"publication": {"subdomain": "howai", "custom_domain_optional": True}}]}
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = profile
            session.get.return_value = resp

            SubstackApi(
                cookies_string="connect.sid=abc123; other=val",
                publication_url="https://howai.substack.com",
            )
            assert session.cookies.set.call_count == 2


# ============================================================
# cmd_post
# ============================================================

class TestCmdPost:
    def test_missing_file(self, tmp_path):
        import substack
        substack.LLM_RESPONSE_TMP = tmp_path / "nonexistent.txt"
        with pytest.raises(SystemExit):
            cmd_post()

    def test_empty_file(self, tmp_path):
        import substack
        substack.LLM_RESPONSE_TMP = tmp_path / "empty.txt"
        substack.LLM_RESPONSE_TMP.write_text("")
        cmd_post()  # returns silently

    def test_missing_cookie(self, tmp_path):
        import substack
        substack.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        substack.LLM_RESPONSE_TMP.write_text("<p>content</p>")
        with patch.dict("os.environ", {"SUBSTACK_COOKIE": "", "SUBSTACK_PUBLICATION_URL": "x"}, clear=True):
            with pytest.raises(SystemExit):
                cmd_post()

    def test_missing_pub_url(self, tmp_path):
        import substack
        substack.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        substack.LLM_RESPONSE_TMP.write_text("<p>content</p>")
        with patch.dict("os.environ", {"SUBSTACK_COOKIE": "abc", "SUBSTACK_PUBLICATION_URL": ""}, clear=True):
            with pytest.raises(SystemExit):
                cmd_post()

    @patch("substack.SubstackApi")
    def test_publish_success(self, mock_api_cls, tmp_path):
        import substack
        substack.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        substack.LLM_RESPONSE_TMP.write_text("<p>content</p>")

        mock_api = MagicMock()
        mock_api.user_id = 42
        mock_api.create_draft.return_value = {"id": "d1"}
        mock_api.publish.return_value = {"slug": "test-post"}
        mock_api_cls.return_value = mock_api

        with patch.dict("os.environ", {"SUBSTACK_COOKIE": "abc", "SUBSTACK_PUBLICATION_URL": "https://howai.substack.com"}, clear=True):
            cmd_post()
        mock_api.publish.assert_called_once()

    @patch("substack.SubstackApi")
    def test_draft_mode(self, mock_api_cls, tmp_path):
        import substack
        substack.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        substack.LLM_RESPONSE_TMP.write_text("<p>content</p>")

        mock_api = MagicMock()
        mock_api.user_id = 42
        mock_api.create_draft.return_value = {"id": "d1"}
        mock_api_cls.return_value = mock_api

        with patch.dict("os.environ", {"SUBSTACK_COOKIE": "abc", "SUBSTACK_PUBLICATION_URL": "https://howai.substack.com"}, clear=True):
            cmd_post(draft_only=True)
        mock_api.publish.assert_not_called()


# ============================================================
# main (CLI)
# ============================================================

class TestMain:
    def test_no_args_exits(self):
        from substack import main
        with patch("sys.argv", ["substack.py"]):
            with pytest.raises(SystemExit):
                main()

    @patch("substack.cmd_post")
    def test_post_flag(self, mock_cmd):
        from substack import main
        with patch("sys.argv", ["substack.py", "--post"]):
            main()
        mock_cmd.assert_called_once()

    @patch("substack.cmd_post")
    def test_post_with_all_flags(self, mock_cmd):
        from substack import main
        with patch("sys.argv", ["substack.py", "--post", "--draft", "--start-date", "2026-01-01", "--end-date", "2026-01-02"]):
            main()
        mock_cmd.assert_called_once_with(draft_only=True, start_date="2026-01-01", end_date="2026-01-02")
