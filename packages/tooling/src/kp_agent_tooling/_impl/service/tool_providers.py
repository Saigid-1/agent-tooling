"""Optional tool providers for the navigation server, discovered by entry point.

The core registers its own operations. An installed distribution may add more by
declaring an entry point in the ``kp_agent_tooling.tools`` group. The entry point
names a factory; the server calls it once with itself (the serving ``AgentTooling``)
and uses the returned provider object:

- ``names()``: every operation name the provider owns, including names the current
  configuration makes unavailable (a configuration may enable any of them).
- ``tools(schemas)``: descriptors for its available operations, each as
  ``(after, descriptor)``; ``after`` names the tool it follows in the listing.
- ``call(name, args, navigation_config=..., selected_snapshot_id=...)``: the result,
  or ``NOT_HANDLED`` for a name the provider does not serve.

Optional contributions: ``knowledge_provider(current)``, ``leading_tools(...)``,
``remote(name, local_names)``, ``before_call(name, catalog)``,
``check_arguments(name, args, schema)``, ``doctor_preflight()``, ``doctor(...)`` and
``identity(result)``.

The core never imports a provider by name. With none installed it serves only its
own operations; configured names nothing installed serves are reported as a gap
(``AgentTooling.unavailable_tools``) instead of refusing the configuration.
"""
from importlib.metadata import entry_points

GROUP = 'kp_agent_tooling.tools'

# Returned by a provider's call() for an operation it does not serve.
NOT_HANDLED = object()


def discover():
    """Installed entry points of the provider group, once each, in name order."""
    seen, found = set(), []
    for entry in sorted(entry_points(group=GROUP), key=lambda e: (e.name, e.value)):
        if (entry.name, entry.value) not in seen:
            seen.add((entry.name, entry.value))
            found.append(entry)
    return found


def load(host):
    """Instantiate every installed provider for one server; import failures propagate."""
    return [entry.load()(host) for entry in discover()]
