# External output-observer/backpressure example

This independently installable distribution registers the coordinator observer `example-output-observer:bounded` and a namespaced queue-size setting. The coordinator emits immutable `OutputEvent`s in sequence order and awaits each observer serially, so a full observer queue applies bounded backpressure rather than creating unbounded delivery tasks. The example is best-effort: ordinary observer failures are counted and do not fail execution; cancellation always propagates. A critical observer can be registered with `critical=True`, in which case failures fail the operation.

After constructing a coordinator, consume from `coordinator.output_observers["example-output-observer:bounded"]` and call `acknowledge()` after each received event. Slow or stopped consumers intentionally backpressure execution when their bounded queue fills. Events preserve their originating request/execution IDs; observers cannot mutate the frozen event data.
