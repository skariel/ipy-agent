# External command and transformation example

This separately installable distribution contributes `/greet`, an async context stage, and an async model-request stage. Both stages are named and are ordered by the host's deterministic before/after pipeline topology. The command and stages receive immutable configuration values from the `example-command-context.*` namespace.

Install the distribution, explicitly activate `example-command-context`, and select the runtime in a coordinator. Installation alone never activates plugin code. `/greet Ada` returns the configured greeting; model calls receive the prefixed context and a `example_request_tag` option. The package imports only public `py_agent.configuration`, `py_agent.contracts`, and `py_agent.plugins` modules.

These transformations are trusted plugin code and run with the host's Python privileges. They should be deterministic and avoid external side effects; cancellation is propagated and a cancelled generation is never replayed automatically.
