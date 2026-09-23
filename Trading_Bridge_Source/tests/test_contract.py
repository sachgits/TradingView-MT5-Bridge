"""The wire-protocol vocabulary shared by the bridge, the board and the EA."""
from bridge import contract


def test_describe_retcode_tells_success_from_rejection():
    assert contract.describe_retcode(0) == "success"
    # MT5's own success codes must never read as a rejection.
    assert contract.describe_retcode(10009) == "request completed"
    assert contract.describe_retcode(10008) == "order placed"
    assert contract.describe_retcode(10010) == "completed partially"
    assert contract.describe_retcode(10006) == "request rejected"
    assert contract.describe_retcode(10019) == "no money"
    assert contract.describe_retcode(12345) == "broker rejection"


def test_describe_retcode_local_guards():
    assert contract.describe_retcode(-13) == "unknown or unimplemented action"
    assert contract.describe_retcode(-10) == "terminal offline"
    assert contract.describe_retcode(-999) == "unrecognised local guard code"


def test_normalize_action_aliases():
    assert contract.normalize_action("BUY") == "buy"
    assert contract.normalize_action(" close_all ") == "close_all"
    assert contract.normalize_action("buy_limit") == "buy_limit"
    # Unknown verbs pass through untouched; refusing them is the validator's job.
    assert contract.normalize_action("teleport") == "teleport"


def test_every_skipped_action_is_refused_by_validation_not_silently_queued():
    for action in contract.SKIPPED_ACTIONS:
        assert action not in contract.EXECUTED_ACTIONS


def test_file_suffix_covers_every_intake_outcome():
    for status in ("queued", "rejected", "invalid", "duplicate"):
        assert status in contract.FILE_SUFFIX_FOR_STATUS, status
    assert contract.FILE_SUFFIX_FOR_STATUS["queued"] == contract.FILE_SUFFIX_PROCESSED
    assert contract.FILE_SUFFIX_FOR_STATUS["invalid"] == contract.FILE_SUFFIX_INVALID


def test_envelope_puts_bridge_keys_before_the_command():
    keys = contract.ENVELOPE_KEYS
    assert keys.index("request_id") < keys.index("normalized")
    assert keys.index("timestamp") < keys.index("normalized")
    assert keys[-1] == "signal"


def test_public_contract_documents_everything_the_board_shows():
    public = contract.public_contract()
    for key in ("success_retcodes", "broker_retcodes", "local_retcodes",
                "signal_statuses", "envelope_keys", "ea_inputs", "unsupported_actions"):
        assert key in public
    assert public["success_retcodes"]["10009"] == "request completed"
