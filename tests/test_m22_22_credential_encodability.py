"""M22-22. A credential that cannot enter an HTTP header is refused when it is submitted.

An HTTP header value is latin-1 encoded. `RuntimeCredential` rejected C0 control characters and
`DEL` but nothing above `U+00FF`, so a key carrying an invisible character -- a zero-width space, a
BOM picked up from an editor, a smart quote substituted by a chat client -- was accepted, stored,
and failed later inside `http.client.putheader`.

That mattered for two separate reasons. `UnicodeEncodeError` derives from `ValueError` and matches
none of the transport's except clauses, so it escaped the provider boundary uncaught where every
other failure there is a typed outcome with a remediation. And it carries the whole
`Authorization: Bearer <secret>` value in its `args` and `object`, which is precisely the route this
class documents as closed.

`ProviderSettingsState` already turns a construction failure into a content-free
`CREDENTIAL_REJECTED`. The fix is only to complete the validation so the failure lands there.
"""

from __future__ import annotations

import http.client
import ssl
import unittest

from test_provider_settings import REMOTE_ID, select, state

from comfyui_h3_context.core.prompt_model_provider import PromptModelContractError
from comfyui_h3_context.core.provider_settings import (
    ProviderIntentRejection,
    ProviderSettingsIntent,
)
from comfyui_h3_context.core.remote_prompt_model import RuntimeCredential

BODY = "sk-live-" + "F" * 24

#: Characters that cannot be encoded latin-1 and therefore cannot be transmitted at all. The last
#: is the exact boundary: U+00FF is the highest encodable code point, so U+0100 is the first that
#: is not, and an off-by-one in the guard shows up here rather than nowhere.
UNSENDABLE = (
    ("﻿", "byte order mark"),
    ("​", "zero-width space"),
    ("’", "right single quotation mark"),
    ("　", "ideographic space"),
    ("\U0001f511", "key emoji, outside the basic plane"),
    ("Ā", "the first code point past latin-1"),
)


class ACredentialThatCannotBeSentIsRefusedTests(unittest.TestCase):
    def test_every_unsendable_character_is_refused_wherever_it_sits(self) -> None:
        # Position is varied because a guard that inspects only the first or the last character
        # passes a naive test and fails a pasted key, where the stray character is usually at one
        # end but not reliably either end.
        for char, label in UNSENDABLE:
            for placement, secret in (
                ("leading", char + BODY),
                ("trailing", BODY + char),
                ("interior", BODY[:10] + char + BODY[10:]),
            ):
                with self.subTest(character=label, placement=placement):
                    with self.assertRaises(PromptModelContractError) as caught:
                        RuntimeCredential(secret)
                    self.assertEqual("credential_value", caught.exception.code)

    def test_the_refusal_carries_the_code_and_no_fragment_of_the_secret(self) -> None:
        # The whole point of refusing here rather than at transmission is that the object raised
        # holds nothing. `UnicodeEncodeError` held the entire Authorization value in `args`.
        secret = BODY + "​"
        with self.assertRaises(PromptModelContractError) as caught:
            RuntimeCredential(secret)
        error = caught.exception
        self.assertEqual(("credential_value",), error.args)
        for rendering in (str(error), repr(error), repr(error.args)):
            with self.subTest(rendering=rendering[:32]):
                self.assertNotIn(BODY, rendering)
                self.assertNotIn(secret, rendering)

    def test_a_refused_credential_leaves_nothing_behind_in_the_half_built_object(self) -> None:
        # A failing `__init__` still leaves its `self` reachable from the traceback frame, so a
        # formatter that walks frames and reprs the locals meets this object. Two properties are
        # asserted about it, and the second was argued rather than tested until a review pointed out
        # that the argument was imprecise.
        #
        # The renderings stay redacted -- that is the guarantee the class states.
        #
        # And the slot is never written. Refusing before the assignment rather than after is not
        # merely tidy: under the opposite ordering the secret is readable straight off the
        # half-built object, which is a real difference and therefore a testable one. The class
        # documents direct slot access as an undefended channel for any *live* instance, but that
        # disclosure is about objects a caller was handed, and it should not be stretched to cover
        # one whose construction was refused.
        secret = BODY + "​"
        try:
            RuntimeCredential(secret)
        except PromptModelContractError as error:
            frame = error.__traceback__.tb_next.tb_frame  # type: ignore[union-attr]
            subject = frame.f_locals.get("self")
            assert isinstance(subject, RuntimeCredential)
            for rendering in (repr(subject), str(subject), format(subject)):
                self.assertEqual("<RuntimeCredential redacted>", rendering)
            with self.assertRaises(AttributeError):
                getattr(subject, "_secret")  # noqa: B009 - reading it is the assertion
        else:  # pragma: no cover - the construction above must fail
            self.fail("construction was expected to fail")

    def test_the_unsendable_class_is_disjoint_from_every_transport_handler(self) -> None:
        # Why this had to be fixed at construction rather than caught at the boundary: the error it
        # produced is not in any of the families `RemoteHttpsExchange.request` handles, so it left
        # the adapter entirely instead of becoming an outcome with a remediation.
        for handled in (
            TimeoutError,
            ssl.SSLError,
            ConnectionError,
            OSError,
            http.client.HTTPException,
        ):
            with self.subTest(handled=handled.__name__):
                self.assertFalse(issubclass(UnicodeEncodeError, handled))


class EverythingSendableIsStillAcceptedTests(unittest.TestCase):
    def test_the_whole_latin_1_printable_range_is_still_accepted(self) -> None:
        # A strict subset was removed, not a general tightening. 0x80-0xFF is obs-text under
        # RFC 7230 and is transmissible, so it stays accepted exactly as before.
        for code_point in range(0x20, 0x100):
            if code_point == 0x7F:
                continue
            with self.subTest(code_point=f"U+{code_point:04X}"):
                RuntimeCredential(BODY + chr(code_point))

    def test_the_control_characters_rejected_before_are_still_rejected(self) -> None:
        for code_point in [*range(0x00, 0x20), 0x7F]:
            with self.subTest(code_point=f"U+{code_point:04X}"):
                with self.assertRaises(PromptModelContractError):
                    RuntimeCredential(BODY + chr(code_point))

    def test_no_accepted_credential_can_produce_a_header_putheader_will_refuse(self) -> None:
        # AC-05, over both exits rather than the one this defect happened to surface through, and
        # over both ways `http.client.putheader` can refuse a value rather than only the one that
        # was broken. It raises `ValueError` twice: once when the value will not encode latin-1,
        # which is the route this item closes, and once when the value matches its illegal-value
        # pattern -- a bare CR or LF, that is, header injection. The second is the worse leak of
        # the two, because it interpolates the value into the message, so the credential lands in
        # `str(exc)` and `repr(exc)` and not merely in `args`. It is already unreachable, closed by
        # the C0 check this item's clause was added beside, and that is worth pinning rather than
        # assuming: the property wanted here is that an accepted credential yields a header
        # `putheader` accepts, and it should not have to be re-derived the next time someone widens
        # what a credential may contain.
        #
        # Expressed as the property, not by importing the private regex, so this does not break on
        # a CPython refactor that renames it.
        for code_point in range(0x20, 0x100):
            if code_point == 0x7F:
                continue
            credential = RuntimeCredential(BODY + chr(code_point))
            for exit_name in ("authorization_header", "api_key_header"):
                with self.subTest(code_point=f"U+{code_point:04X}", exit=exit_name):
                    value = getattr(credential, exit_name)()
                    value.encode("latin-1")
                    self.assertNotIn("\r", value)
                    self.assertNotIn("\n", value)


class TheProductRefusesItAtSubmissionTests(unittest.TestCase):
    def test_a_submitted_credential_with_an_invisible_character_is_rejected(self) -> None:
        # The behaviour a user actually meets. This path was already correct and simply never
        # reached for this input class, which is the whole shape of the defect.
        for char, label in UNSENDABLE:
            with self.subTest(character=label):
                subject = state()
                select(subject, REMOTE_ID)
                result = subject.apply(
                    ProviderSettingsIntent.SUBMIT_CREDENTIAL, {"credential": BODY + char}
                )
                self.assertFalse(result.accepted)
                self.assertIs(result.rejection, ProviderIntentRejection.CREDENTIAL_REJECTED)

    def test_a_rejected_submission_stores_nothing_and_leaks_nothing(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        secret = BODY + "﻿"
        before = subject.revision
        result = subject.apply(ProviderSettingsIntent.SUBMIT_CREDENTIAL, {"credential": secret})
        self.assertFalse(result.accepted)
        # A refused intent must not move the revision, and the projection must not carry the value.
        self.assertEqual(before, subject.revision)
        self.assertNotIn(BODY, repr(result.projection))

    def test_an_ordinary_credential_is_still_accepted_through_the_same_route(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        result = subject.apply(ProviderSettingsIntent.SUBMIT_CREDENTIAL, {"credential": BODY})
        self.assertTrue(result.accepted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
