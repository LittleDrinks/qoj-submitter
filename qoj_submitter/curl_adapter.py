"""Requests transport using libcurl for the QOJ main site."""

from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pycurl
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict


class CurlAdapter(BaseAdapter):
    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if stream or cert or verify is not True:
            raise requests.RequestException("Unsupported libcurl transport option")
        curl = pycurl.Curl()
        body = BytesIO()
        headers = Message()

        def receive_header(line):
            nonlocal headers
            text = line.decode("iso-8859-1").strip()
            if text.startswith("HTTP/"):
                headers = Message()
            elif ":" in text:
                name, value = text.split(":", 1)
                headers.add_header(name, value.strip())
            return len(line)

        try:
            curl.setopt(pycurl.URL, request.url)
            curl.setopt(pycurl.CUSTOMREQUEST, request.method)
            curl.setopt(pycurl.FOLLOWLOCATION, False)
            curl.setopt(pycurl.WRITEFUNCTION, body.write)
            curl.setopt(pycurl.HEADERFUNCTION, receive_header)
            curl.setopt(pycurl.ACCEPT_ENCODING, "")
            curl.setopt(pycurl.HTTPHEADER, [f"{k}: {v}" for k, v in request.headers.items()
                                           if k.lower() != "accept-encoding"])
            if timeout is not None:
                value = timeout if isinstance(timeout, (int, float)) else max(timeout)
                curl.setopt(pycurl.TIMEOUT_MS, max(1, int(value * 1000)))
            if request.body is not None:
                data = request.body.encode() if isinstance(request.body, str) else request.body
                if not isinstance(data, bytes):
                    raise requests.RequestException("Unsupported streaming request body")
                curl.setopt(pycurl.POSTFIELDSIZE, len(data))
                curl.setopt(pycurl.POSTFIELDS, data)
            curl.perform()
            response = requests.Response()
            response.status_code = curl.getinfo(pycurl.RESPONSE_CODE)
            response.url = request.url
            response.request = request
            response.headers = CaseInsensitiveDict(headers.items())
            response._content = body.getvalue()
            response.encoding = requests.utils.get_encoding_from_headers(response.headers)
            # requests.Session uses the original response headers to update its cookie jar.
            response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response
        except pycurl.error as exc:
            raise requests.RequestException(f"libcurl error {exc.args[0]}") from exc
        finally:
            curl.close()

    def close(self):
        pass
