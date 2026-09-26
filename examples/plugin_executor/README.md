# External executor-wrapper example

This independently installable distribution registers the explicitly selectable wrapper `example-executor-wrapper:audit`. Pass that exact ID in `Coordinator(..., executor_wrappers=(... ,))` to decorate the selected executor. The wrapper uses only the public `Executor`, `ExecutionRequest`, `ExecutionResult`, and `ExecutorCapabilities` contracts, forwards each call exactly once, and propagates cancellation/errors without replaying side-effecting execution.

The coordinator starts and closes the wrapper; the wrapper owns forwarding lifecycle calls to its delegate. Wrapper selection is opt-in and ordered by the caller, not by plugin registration/import order. The example declares the delegate's capabilities unchanged.
