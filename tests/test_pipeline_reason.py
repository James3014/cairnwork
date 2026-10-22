"""The verdict->reason mapping (Issue #74 item 3 / #109): the two reason-text mutants that survived
mutation review — `shown = recorded or verdict` and the CORE_NOT_VERIFIED fallback — are killed here
by testing the extracted pure function directly."""

from types import SimpleNamespace

from cairnwork.pipeline import _verdict_reason


def _v(status, reason=""):
    return SimpleNamespace(status=status, reason=reason)


def test_a_verified_attempt_reads_core_status():
    assert _verdict_reason(True, _v("VERIFIED"), _v("VERIFIED")) == "CORE_VERIFIED"


def test_the_readback_reason_is_shown_not_the_notarize_return():
    # Kills `shown = verdict`: the readback (recorded) reason must win over the notarize verdict's,
    # so an unverified readback never reads as the verdict's own (possibly empty / VERIFIED) reason.
    verdict, recorded = (
        _v("VERIFIED", reason="from-verdict"),
        _v("VERIFIED", "CORE_IDENTITY_MISMATCH"),
    )
    assert _verdict_reason(False, verdict, recorded) == "CORE_IDENTITY_MISMATCH"


def test_without_a_readback_the_verdict_reason_is_used():
    assert _verdict_reason(False, _v("VERIFIED", reason="CORE_MANIFEST_HASH_MISMATCH"), None) == (
        "CORE_MANIFEST_HASH_MISMATCH"
    )


def test_verified_status_but_unverified_readback_with_no_reason_is_core_not_verified():
    # Kills the CORE_NOT_VERIFIED fallback mutant: Core said VERIFIED but the readback did not
    # verify and set no reason, so it must read CORE_NOT_VERIFIED, not CORE_VERIFIED.
    assert _verdict_reason(False, _v("VERIFIED"), _v("VERIFIED", "")) == "CORE_NOT_VERIFIED"


def test_a_non_verified_status_with_no_reason_reads_core_status():
    assert _verdict_reason(False, _v("FAILED_VERIFICATION"), _v("FAILED_VERIFICATION")) == (
        "CORE_FAILED_VERIFICATION"
    )
