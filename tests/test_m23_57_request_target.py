"""M23-57: the pure rule deciding which browser origin may act on this server.

Every case is relative to a request's own target: an origin that is legitimate for its own page is
still foreign to another one. The adapter half (headers, transport, environment, diagnostics) is
covered through the registered routes in `test_m23_57_owned_edge_deployment.py`.
"""

from __future__ import annotations

import ipaddress
import unittest

from comfyui_h3_context.core.request_target import (
    MAX_PUBLIC_ORIGINS,
    MAX_PUBLIC_ORIGINS_LENGTH,
    NO_PUBLIC_ORIGINS,
    AdmissionReason,
    LocalEndpoint,
    OriginRequirement,
    PublicOriginsStatus,
    WebOrigin,
    admission_refusal,
    local_endpoint,
    parse_authority,
    parse_origin,
    parse_public_origins,
    resolve_target,
)


def _local(address: str, port: int, *, secure: bool = False) -> LocalEndpoint:
    return LocalEndpoint(ipaddress.ip_address(address), port, secure)


class TheAuthorityGrammarIsWhatABrowserSendsTests(unittest.TestCase):
    def test_accepted_authorities_normalize(self) -> None:
        cases = {
            "127.0.0.1:8188": ("127.0.0.1", 8188),
            "localhost": ("localhost", None),
            "LOCALHOST:8000": ("localhost", 8000),
            "[::1]:8000": ("[::1]", 8000),
            "[0:0:0:0:0:0:0:1]": ("[::1]", None),
            "h3-proxy.localhost:8443": ("h3-proxy.localhost", 8443),
            "xn--bcher-kva.example": ("xn--bcher-kva.example", None),
            "10.20.30.40:65535": ("10.20.30.40", 65535),
            "a" * 63 + ".example": ("a" * 63 + ".example", None),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_authority(value), expected)

    def test_refused_authorities(self) -> None:
        refused = (
            "",
            " localhost",
            "localhost ",
            "local host",
            "localhost\t",
            "localhost:",
            "localhost:0",
            "localhost:65536",
            "localhost:+80",
            "localhost:08188",
            "localhost:80:80",
            "user@localhost",
            "localhost/x",
            "localhost?x",
            "localhost#x",
            "[::1",
            "::1",
            "[::1]x",
            "[::1]:",
            "[fe80::1%22]:80",
            "[127.0.0.1]",
            "01.2.3.4",
            "256.0.0.1",
            "1.2.3",
            "0x7f.1",
            "example.123",
            "-a.example",
            "a-.example",
            "a..b",
            ".example",
            "localhost.",
            "a_b.example",
            "bücher.example",
            "a" * 64 + ".example",
            ".".join(["a" * 63] * 4) + ".b",
            "localhost,127.0.0.1",
            "*.example",
        )
        for value in refused:
            with self.subTest(value=value):
                self.assertIsNone(parse_authority(value))

    def test_a_non_string_is_refused(self) -> None:
        for value in (None, b"localhost", 8188, ["localhost"]):
            with self.subTest(value=value):
                self.assertIsNone(parse_authority(value))


def _parsed(value: str) -> WebOrigin:
    origin = parse_origin(value)
    assert origin is not None, value
    return origin


class TheOriginGrammarIsTheSerializedOriginTests(unittest.TestCase):
    def test_accepted_origins_normalize_the_default_port(self) -> None:
        self.assertEqual(
            parse_origin("http://127.0.0.1:8188"), WebOrigin("http", "127.0.0.1", 8188)
        )
        self.assertEqual(
            parse_origin("HTTP://LOCALHOST:8188"), WebOrigin("http", "localhost", 8188)
        )
        self.assertEqual(parse_origin("https://h.example:443"), parse_origin("https://h.example"))
        self.assertEqual(_parsed("http://h.example").port, 80)
        self.assertEqual(_parsed("http://[::1]:8000").host, "[::1]")
        self.assertEqual(_parsed("https://h.example").serialize(), "https://h.example")
        self.assertEqual(_parsed("http://localhost:8000").serialize(), "http://localhost:8000")

    def test_refused_origins(self) -> None:
        refused = (
            "null",
            "",
            "http://h.example/",
            "http://h.example/path",
            "http://h.example?x",
            "http://h.example#x",
            "ftp://h.example",
            "ws://h.example",
            "http:/h.example",
            "http://",
            "http://user@h.example",
            " http://h.example",
            "http://h.example:",
            "http://h.example,http://i.example",
            "http://*.example",
            "http://bücher.example",
        )
        for value in refused:
            with self.subTest(value=value):
                self.assertIsNone(parse_origin(value))
        self.assertIsNone(parse_origin(None))


class TheLocalEndpointIsTheAcceptingSocketTests(unittest.TestCase):
    def test_socket_names_normalize(self) -> None:
        self.assertEqual(local_endpoint(("127.0.0.1", 8188), False), _local("127.0.0.1", 8188))
        self.assertEqual(local_endpoint(("::1", 8000, 0, 0), False), _local("::1", 8000))
        # A dual-stack socket reports an IPv4 client's local address in mapped form.
        self.assertEqual(
            local_endpoint(("::ffff:192.168.10.20", 8188, 0, 0), False),
            _local("192.168.10.20", 8188),
        )
        self.assertEqual(local_endpoint(("fe80::1%22", 80, 0, 22), False), _local("fe80::1", 80))
        tls = local_endpoint(("127.0.0.1", 8443), True)
        assert tls is not None
        self.assertTrue(tls.secure)
        # Only a literal `True` is TLS: a truthy object is not.
        truthy = local_endpoint(("127.0.0.1", 8443), 1)  # type: ignore[arg-type]
        assert truthy is not None
        self.assertFalse(truthy.secure)

    def test_unusable_socket_names_are_none(self) -> None:
        for sockname in (
            None,
            (),
            ("127.0.0.1",),
            ("127.0.0.1", 8188, 0),
            ["127.0.0.1", 8188],
            ("localhost", 8188),
            ("127.0.0.1", 0),
            ("127.0.0.1", 65536),
            ("127.0.0.1", "8188"),
            ("127.0.0.1", True),
            (b"127.0.0.1", 8188),
        ):
            with self.subTest(sockname=sockname):
                self.assertIsNone(local_endpoint(sockname, False))


class TheOperatorConfigurationIsAllOrNothingTests(unittest.TestCase):
    def test_unset_configures_nothing_and_is_not_an_error(self) -> None:
        self.assertIs(parse_public_origins(None), NO_PUBLIC_ORIGINS)
        self.assertIs(parse_public_origins(None).status, PublicOriginsStatus.UNSET)

    def test_valid_lists(self) -> None:
        one = parse_public_origins("https://h3-proxy.localhost:8443")
        self.assertIs(one.status, PublicOriginsStatus.VALID)
        self.assertEqual(one.origins, (WebOrigin("https", "h3-proxy.localhost", 8443),))
        two = parse_public_origins(" http://localhost:8000 ,\thttps://comfy.example.com ")
        self.assertIs(two.status, PublicOriginsStatus.VALID)
        self.assertEqual(two.entries, 2)
        most = ",".join(f"http://localhost:{8000 + index}" for index in range(MAX_PUBLIC_ORIGINS))
        self.assertIs(parse_public_origins(most).status, PublicOriginsStatus.VALID)

    def test_every_invalid_form_configures_nothing(self) -> None:
        too_many = ",".join(
            f"http://localhost:{8000 + index}" for index in range(MAX_PUBLIC_ORIGINS + 1)
        )
        cases = {
            "": PublicOriginsStatus.EMPTY,
            " \t ": PublicOriginsStatus.EMPTY,
            ",": PublicOriginsStatus.MALFORMED,
            "https://a.example,": PublicOriginsStatus.MALFORMED,
            "*": PublicOriginsStatus.MALFORMED,
            "https://*.example": PublicOriginsStatus.MALFORMED,
            ".example.com": PublicOriginsStatus.MALFORMED,
            "https://comfy.example.com/": PublicOriginsStatus.MALFORMED,
            "https://user@comfy.example.com": PublicOriginsStatus.MALFORMED,
            "https://bücher.example": PublicOriginsStatus.MALFORMED,
            "https://a.example,not an origin": PublicOriginsStatus.MALFORMED,
            "https://a.example,https://A.EXAMPLE:443": PublicOriginsStatus.DUPLICATE,
            too_many: PublicOriginsStatus.TOO_MANY,
            "x" * (MAX_PUBLIC_ORIGINS_LENGTH + 1): PublicOriginsStatus.TOO_LONG,
        }
        for raw, status in cases.items():
            with self.subTest(raw=raw[:40]):
                parsed = parse_public_origins(raw)
                self.assertIs(parsed.status, status)
                # CRITICAL: a partially valid list is never partially applied.
                self.assertEqual(parsed.origins, ())
        self.assertIs(parse_public_origins(8188).status, PublicOriginsStatus.MALFORMED)


class TheTargetIsTheAuthorityThisServerServesTests(unittest.TestCase):
    def test_direct_listener_targets(self) -> None:
        cases = (
            ("127.0.0.1:8188", _local("127.0.0.1", 8188), "http://127.0.0.1:8188"),
            ("localhost:8188", _local("127.0.0.1", 8188), "http://localhost:8188"),
            ("127.0.0.1:8000", _local("127.0.0.1", 8000), "http://127.0.0.1:8000"),
            ("localhost:8000", _local("::1", 8000), "http://localhost:8000"),
            ("[::1]:8000", _local("::1", 8000), "http://[::1]:8000"),
            ("10.20.30.40:8000", _local("10.20.30.40", 8000), "http://10.20.30.40:8000"),
            ("192.168.10.20:8188", _local("192.168.10.20", 8188), "http://192.168.10.20:8188"),
            ("localhost", _local("127.0.0.1", 80), "http://localhost"),
            ("localhost:8443", _local("127.0.0.1", 8443, secure=True), "https://localhost:8443"),
            ("localhost", _local("127.0.0.1", 443, secure=True), "https://localhost"),
        )
        for host, local, expected in cases:
            with self.subTest(host=host, local=local):
                target = resolve_target([host], local, NO_PUBLIC_ORIGINS)
                self.assertIsInstance(target, WebOrigin)
                assert isinstance(target, WebOrigin)
                self.assertEqual(target.serialize(), expected)

    def test_untrusted_authorities(self) -> None:
        cases = (
            # A DNS name is never trusted from the socket, whatever it resolves to (rebinding).
            ("evil.example:8188", _local("127.0.0.1", 8188)),
            ("127.0.0.1.evil.example:8188", _local("127.0.0.1", 8188)),
            ("my-computer:8188", _local("192.168.10.20", 8188)),
            # An address this socket is not.
            ("192.168.10.20:8188", _local("127.0.0.1", 8188)),
            ("127.0.0.2:8188", _local("127.0.0.1", 8188)),
            ("[::1]:8188", _local("127.0.0.1", 8188)),
            # `localhost` only on a loopback socket.
            ("localhost:8000", _local("10.20.30.40", 8000)),
            # A port this socket is not bound to, explicit or by default.
            ("127.0.0.1:8188", _local("127.0.0.1", 8000)),
            ("localhost", _local("127.0.0.1", 8188)),
            ("localhost:443", _local("127.0.0.1", 8443, secure=True)),
        )
        for host, local in cases:
            with self.subTest(host=host, local=local):
                self.assertIs(
                    resolve_target([host], local, NO_PUBLIC_ORIGINS),
                    AdmissionReason.HOST_UNTRUSTED,
                )

    def test_an_unusable_host_list_is_invalid(self) -> None:
        local = _local("127.0.0.1", 8188)
        for hosts in ([], ["127.0.0.1:8188", "127.0.0.1:8188"], "127.0.0.1:8188", None, ["a b"]):
            with self.subTest(hosts=hosts):
                self.assertIs(
                    resolve_target(hosts, local, NO_PUBLIC_ORIGINS), AdmissionReason.HOST_INVALID
                )

    def test_without_socket_facts_only_a_configured_target_resolves(self) -> None:
        self.assertIs(
            resolve_target(["127.0.0.1:8188"], None, NO_PUBLIC_ORIGINS),
            AdmissionReason.AUTHORITY_UNAVAILABLE,
        )
        configured = parse_public_origins("https://h3-proxy.localhost:8443")
        self.assertEqual(
            resolve_target(["h3-proxy.localhost:8443"], None, configured),
            WebOrigin("https", "h3-proxy.localhost", 8443),
        )

    def test_a_configured_target_supplies_its_own_scheme_and_port(self) -> None:
        configured = parse_public_origins("https://comfy.example.com,http://localhost:8000")
        plain_backend = _local("127.0.0.1", 8188)
        self.assertEqual(
            resolve_target(["comfy.example.com"], plain_backend, configured),
            WebOrigin("https", "comfy.example.com", 443),
        )
        self.assertEqual(
            resolve_target(["COMFY.example.com:443"], plain_backend, configured),
            WebOrigin("https", "comfy.example.com", 443),
        )
        # A port mapping: the browser addressed 8000, the container listens on 8188.
        self.assertEqual(
            resolve_target(["localhost:8000"], plain_backend, configured),
            WebOrigin("http", "localhost", 8000),
        )
        # Not a configured authority: back to the direct rule, which refuses a DNS name.
        for host in ("comfy.example.com:8443", "comfy.example.com.evil.example", "example.com"):
            with self.subTest(host=host):
                self.assertIs(
                    resolve_target([host], plain_backend, configured),
                    AdmissionReason.HOST_UNTRUSTED,
                )

    def test_invalid_configuration_configures_no_target(self) -> None:
        ignored = parse_public_origins("https://comfy.example.com,*")
        self.assertIs(
            resolve_target(["comfy.example.com"], _local("127.0.0.1", 8188), ignored),
            AdmissionReason.HOST_UNTRUSTED,
        )


def _refusal(
    requirement: OriginRequirement,
    origins: object,
    *,
    host: str = "127.0.0.1:8188",
    local: LocalEndpoint | None = None,
    sites: object = None,
    public: object = None,
) -> AdmissionReason | None:
    return admission_refusal(
        requirement,
        hosts=[host],
        origins=origins,
        fetch_sites=[] if sites is None else sites,
        local=_local("127.0.0.1", 8188) if local is None else local,
        public=NO_PUBLIC_ORIGINS if public is None else public,  # type: ignore[arg-type]
    )


class TheOriginMustBeTheTargetTests(unittest.TestCase):
    def test_each_deployment_admits_its_own_page(self) -> None:
        cases = (
            ("127.0.0.1:8188", _local("127.0.0.1", 8188), "http://127.0.0.1:8188"),
            ("localhost:8188", _local("127.0.0.1", 8188), "http://localhost:8188"),
            ("127.0.0.1:8000", _local("127.0.0.1", 8000), "http://127.0.0.1:8000"),
            ("[::1]:8000", _local("::1", 8000), "http://[::1]:8000"),
            ("10.20.30.40:8000", _local("10.20.30.40", 8000), "http://10.20.30.40:8000"),
            ("localhost:8443", _local("127.0.0.1", 8443, secure=True), "https://localhost:8443"),
        )
        for host, local, origin in cases:
            for requirement in OriginRequirement:
                with self.subTest(host=host, requirement=requirement):
                    self.assertIsNone(_refusal(requirement, [origin], host=host, local=local))

    def test_a_trusted_alias_never_authorizes_another(self) -> None:
        # N01: every one of these is legitimate for its own page and foreign to this one.
        for origin in (
            "http://localhost:8188",
            "http://127.0.0.1:8000",
            "http://[::1]:8188",
            "https://127.0.0.1:8188",
            "http://192.168.10.20:8188",
            "http://evil.example",
        ):
            with self.subTest(origin=origin):
                self.assertIs(
                    _refusal(OriginRequirement.REQUIRED, [origin]),
                    AdmissionReason.ORIGIN_MISMATCH,
                )

    def test_two_configured_targets_are_not_interchangeable(self) -> None:
        configured = parse_public_origins("https://a.example,https://b.example")
        self.assertIsNone(
            _refusal(
                OriginRequirement.REQUIRED,
                ["https://a.example"],
                host="a.example",
                public=configured,
            )
        )
        self.assertIs(
            _refusal(
                OriginRequirement.REQUIRED,
                ["https://b.example"],
                host="a.example",
                public=configured,
            ),
            AdmissionReason.ORIGIN_MISMATCH,
        )

    def test_the_origin_header_must_be_one_well_formed_value(self) -> None:
        own = "http://127.0.0.1:8188"
        cases = {
            AdmissionReason.ORIGIN_INVALID: (
                [own, own],
                [own, "http://foreign.invalid"],
                [own + ",http://foreign.invalid"],
                ["null"],
                [own + "/"],
                "not a list",
            ),
            AdmissionReason.ORIGIN_MISSING: ([],),
        }
        for reason, values in cases.items():
            for origins in values:
                with self.subTest(origins=origins):
                    self.assertIs(_refusal(OriginRequirement.REQUIRED, origins), reason)

    def test_only_the_optional_requirements_admit_an_absent_origin(self) -> None:
        self.assertIs(_refusal(OriginRequirement.REQUIRED, []), AdmissionReason.ORIGIN_MISSING)
        self.assertIsNone(_refusal(OriginRequirement.OPTIONAL, []))
        self.assertIsNone(_refusal(OriginRequirement.OPTIONAL_SAME_ORIGIN_FETCH, []))

    def test_an_absent_origin_still_needs_a_trusted_authority(self) -> None:
        # N03 for reads: a rebinding page's same-origin GET carries no Origin at all.
        for requirement in (
            OriginRequirement.OPTIONAL,
            OriginRequirement.OPTIONAL_SAME_ORIGIN_FETCH,
        ):
            with self.subTest(requirement=requirement):
                self.assertIs(
                    _refusal(requirement, [], host="evil.example:8188"),
                    AdmissionReason.HOST_UNTRUSTED,
                )

    def test_a_rebinding_page_is_refused_although_its_headers_agree(self) -> None:
        self.assertIs(
            _refusal(
                OriginRequirement.REQUIRED,
                ["http://evil.example:8188"],
                host="evil.example:8188",
            ),
            AdmissionReason.HOST_UNTRUSTED,
        )

    def test_fetch_metadata_is_checked_only_where_required(self) -> None:
        cases = {
            None: ([], ["same-origin"]),
            AdmissionReason.FETCH_SITE_REJECTED: (
                ["same-site"],
                ["cross-site"],
                ["none"],
                ["same-origin", "same-origin"],
                "same-origin",
            ),
        }
        for expected, values in cases.items():
            for sites in values:
                with self.subTest(sites=sites):
                    self.assertIs(
                        _refusal(OriginRequirement.OPTIONAL_SAME_ORIGIN_FETCH, [], sites=sites),
                        expected,
                    )
        self.assertIsNone(_refusal(OriginRequirement.OPTIONAL, [], sites=["cross-site"]))

    def test_the_authority_is_checked_before_the_origin(self) -> None:
        self.assertIs(
            _refusal(OriginRequirement.REQUIRED, ["null"], host="evil.example:8188"),
            AdmissionReason.HOST_UNTRUSTED,
        )
        self.assertIs(
            admission_refusal(
                OriginRequirement.REQUIRED,
                hosts=["127.0.0.1:8188"],
                origins=["http://127.0.0.1:8188"],
                fetch_sites=[],
                local=None,
                public=NO_PUBLIC_ORIGINS,
            ),
            AdmissionReason.AUTHORITY_UNAVAILABLE,
        )


if __name__ == "__main__":
    unittest.main()
