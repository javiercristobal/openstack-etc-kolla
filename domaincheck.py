"""
Does this domain already exist? Used by the order site (before charging) and by the webhook
(right before registering). Shared file: keep the copies in site/ and webhook/ identical.

  DOMAIN_CHECK=auto     GoDaddy if GODADDY_TOKEN is set, otherwise RDAP  (default)
  DOMAIN_CHECK=rdap     the public registry data service. Needs no account and no token.
                        A registered name answers 200, an unregistered one 404.
  DOMAIN_CHECK=godaddy  GoDaddy's availability API (read only, free). Also knows about
                        reserved and premium names that RDAP reports as "not found".

Registration is a separate step (registrar.py) and is fake unless you switch it on.
When the answer is uncertain the checker raises CheckError instead of guessing "available".
"""

import os
import socket
import time

import requests
from urllib3.util import connection as _urllib3_connection

from godaddy import GoDaddy, GoDaddyError, valid_domain

# Use IPv4 only for every connection this module makes. Some hosts have an
# IPv6 address that is unreachable, and Python then fails with "Network is
# unreachable" even though IPv4 works.
_urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

SUPPORTED_TLDS = ("com", "net", "eu")
BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
FALLBACK_RDAP = "https://rdap.org/"        # public redirector, used only if the IANA list is unreachable
_BOOTSTRAP_TTL = 24 * 3600


class CheckError(Exception):
    """The check could not be completed. Not an answer: ask again later."""


class RdapChecker:
    source = "rdap"

    def __init__(self, session=None, timeout=10):
        self.http = session or requests.Session()
        self.http.headers.update({"Accept": "application/rdap+json", "User-Agent": "kb-order-site"})
        self.timeout = timeout
        self._services = None
        self._loaded = 0.0

    def _base_for(self, tld):
        if self._services is None or time.time() - self._loaded > _BOOTSTRAP_TTL:
            try:
                data = self.http.get(BOOTSTRAP_URL, timeout=self.timeout).json()
                services = {}
                for tlds, urls in data["services"]:
                    https = [u for u in urls if u.startswith("https://")]
                    if https:
                        for t in tlds:
                            services[t.lower()] = https[0]
                self._services, self._loaded = services, time.time()
            except (requests.RequestException, ValueError, KeyError, TypeError):
                if self._services is None:
                    return FALLBACK_RDAP
        base = (self._services or {}).get(tld, FALLBACK_RDAP)
        return base if base.endswith("/") else base + "/"

    def check(self, domain):
        tld = domain.rpartition(".")[2]
        if not valid_domain(domain) or tld not in SUPPORTED_TLDS:
            raise CheckError(f"{domain!r} is not a name this site can check")
        url = f"{self._base_for(tld)}domain/{domain}"
        try:
            resp = self.http.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise CheckError(f"Cannot reach the registry for .{tld}: {exc}") from exc
        if resp.status_code == 200:
            return {"available": False, "source": self.source, "detail": "registered"}
        if resp.status_code == 404:
            return {"available": True, "source": self.source, "detail": "not registered"}
        raise CheckError(f"The .{tld} registry answered HTTP {resp.status_code}")


class GoDaddyChecker:
    source = "godaddy"

    def __init__(self, client):
        self.gd = client

    def check(self, domain):
        tld = domain.rpartition(".")[2]
        if not valid_domain(domain) or tld not in SUPPORTED_TLDS:
            raise CheckError(f"{domain!r} is not a name this site can check")
        try:
            info = self.gd.check_availability(domain)
        except GoDaddyError as exc:
            raise CheckError(str(exc)) from exc
        if not info["definitive"]:
            raise CheckError(f"GoDaddy could not confirm {domain} yet")
        return {"available": info["available"], "source": self.source, "detail": "GoDaddy availability check"}


def make_checker(env=None):
    env = os.environ if env is None else env
    mode = env.get("DOMAIN_CHECK", "auto").strip().lower()
    token = env.get("GODADDY_TOKEN", "").strip()
    if mode == "auto":
        mode = "godaddy" if token else "rdap"
    if mode == "rdap":
        return RdapChecker()
    if mode == "godaddy":
        if not token:
            raise RuntimeError("DOMAIN_CHECK=godaddy needs GODADDY_TOKEN")
        return GoDaddyChecker(GoDaddy(token, base_url=env.get("GODADDY_BASE_URL", "https://api.godaddy.com")))
    raise RuntimeError("DOMAIN_CHECK must be auto, rdap or godaddy")
