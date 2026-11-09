"""One HTTP opener for every outbound call that carries a credential.

urllib's default redirect handler copies every non-content header, `Authorization` included,
onto the redirected request. A server that answers with a redirect to another host would then
receive the credential. `SAFE_OPENER` follows a redirect only to the same scheme, host and
port; any other redirect surfaces as an HTTP error. stdlib only.
"""

from __future__ import annotations

import urllib.parse
import urllib.request


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when scheme, host and port are unchanged."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (old.scheme, old.netloc.lower()) != (new.scheme, new.netloc.lower()):
            return None  # urllib then raises HTTPError for the 3xx
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Built once for the process. It does not pool connections.
SAFE_OPENER = urllib.request.build_opener(SameOriginRedirects())
