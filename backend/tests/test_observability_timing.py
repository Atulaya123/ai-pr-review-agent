from backend.observability.timing import clock_domain_id, queue_elapsed_ms


def test_queue_duration_uses_matching_monotonic_clock_domain() -> None:
    enqueued_ns = 1_000_000_000
    picked_up_ns = 1_250_000_000

    assert queue_elapsed_ms(enqueued_ns, clock_domain_id(), picked_up_ns) == 250


def test_queue_duration_is_unavailable_for_different_clock_domains() -> None:
    assert queue_elapsed_ms(1_000_000_000, "another-kernel-boot", 1_250_000_000) is None


def test_queue_duration_never_reports_negative_elapsed_time() -> None:
    assert queue_elapsed_ms(1_250_000_000, clock_domain_id(), 1_000_000_000) == 0
