from sacrifice_failure_loop import batch_gate, classify_batch, classify_failure


def test_known_legacy_failures_get_repair_actions():
    failure = classify_failure({"status": "failed", "stage": "send_failed", "error_message": "iframe埋め込み"})
    assert failure.code == "IFRAME_UNSUPPORTED"
    assert failure.repair


def test_unknown_failure_is_fixture_candidate():
    failure = classify_failure({"status": "failed", "stage": "new_dom", "error_message": "unexpected"})
    assert failure.code == "UNKNOWN"
    assert "fixture" in failure.repair


def test_batch_requires_ten_and_seven_without_critical_error():
    results = [{"semantic_success": i < 7, "critical_errors": []} for i in range(10)]
    assert batch_gate(results)["batch_status"] == "PASS"
    results[0]["critical_errors"] = ["IDENTITY_MAPPING_CORRUPT"]
    assert batch_gate(results)["batch_status"] == "FAIL"


def test_batch_classifier_counts_failures():
    result = classify_batch([
        {"status": "skip", "stage": "no_channel_found", "error_message": ""},
        {"status": "failed", "stage": "bot_defense_detected", "error_message": "captcha"},
    ])
    assert result["attempted"] == 2
    assert result["failure_counts"]["NO_CHANNEL_FOUND"] == 1
    assert result["failure_counts"]["BOT_DEFENSE"] == 1

