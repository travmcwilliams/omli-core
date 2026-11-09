"""One generate payload path + reused HTTP opener."""

from omli.models import ollama as ol


def test_build_generate_payload_is_the_single_shape():
    p = ol.build_generate_payload("example-model:3b", "hello", num_ctx=16384, temperature=0.2)
    assert p["model"] == "example-model:3b"
    assert p["stream"] is False
    assert p["options"]["num_ctx"] == 16384
    assert "num_ctx" not in p


def test_generate_completion_uses_shared_payload_and_transport():
    captured = {}

    def fake(url, body, timeout):
        captured["url"] = url
        captured["body"] = body
        captured["timeout"] = timeout
        return b'{"response":"ok","prompt_eval_count":3,"eval_count":1}'

    class E:
        serving_tag = "example-model:3b"
        endpoint = "http://127.0.0.1:11434"

    text, data = ol.generate_completion(E(), "hi", transport=fake, timeout_s=12)
    assert text == "ok"
    assert data["eval_count"] == 1
    assert captured["url"].endswith("/api/generate")
    assert b'"num_ctx": 16384' in captured["body"]


def test_urllib_transport_uses_module_opener(monkeypatch):
    """The transport really goes through the module opener (the one that refuses
    cross-origin redirects), with the caller's headers on the request."""
    from omli.net import SameOriginRedirects

    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_open(request, timeout=None):
        seen.append(request)
        return Response()

    monkeypatch.setattr(ol._OPENER, "open", fake_open)
    assert ol._urllib_transport("http://127.0.0.1:1/x", b"{}", 1, headers={"X-Test": "1"}) == b"{}"
    assert seen and seen[0].get_header("X-test") == "1"
    assert any(isinstance(h, SameOriginRedirects) for h in ol._OPENER.handlers)
