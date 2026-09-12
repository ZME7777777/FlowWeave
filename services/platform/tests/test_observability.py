from flowweave.shared.observability import Metrics, bind_metrics, current_metrics, reset_metrics


def test_operation_metrics_render_fixed_labels_and_item_count() -> None:
    metrics = Metrics()

    metrics.observe_operation("agent_workspace.file_tree_scan", 0.12, outcome="ok", items=3)

    rendered = metrics.render()
    assert (
        "flowweave_operation_duration_seconds_count"
        '{operation="agent_workspace.file_tree_scan",outcome="ok"} 1' in rendered
    )
    assert (
        'flowweave_operation_items_total{operation="agent_workspace.file_tree_scan",outcome="ok"} 3'
        in rendered
    )


def test_metrics_context_is_explicit_and_resettable() -> None:
    metrics = Metrics()
    assert current_metrics() is None

    token = bind_metrics(metrics)
    try:
        assert current_metrics() is metrics
    finally:
        reset_metrics(token)

    assert current_metrics() is None
