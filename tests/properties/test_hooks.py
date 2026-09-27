"""INV-10: a hook never fails the session, on generated stdin. The fixed
inputs that found each defect are in tests/test_hooks_fuzz.py."""
import json

from hypothesis import HealthCheck, given, settings, strategies as st

from tests.test_hooks_fuzz import FIELDS, HOOKS, hook_env, run_hook  # noqa: F401 (fixture)


JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=100),
                    lambda children: st.lists(children, max_size=4) |
                    st.dictionaries(st.sampled_from(FIELDS + ["tool_input", "command", "file_path",
                                                            "notebook_path"]), children, max_size=5),
                    max_leaves=15)


@settings(max_examples=6, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(payload=st.one_of(st.binary(max_size=512), JSON.map(lambda v: json.dumps(v).encode())))
def test_generated_hook_stdin(hook_env, payload):
    for hook in HOOKS:
        run_hook(hook, payload, hook_env)
