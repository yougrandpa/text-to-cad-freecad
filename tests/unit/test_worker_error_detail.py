"""What a failed worker call actually says.

A real request ("创建一个手机支架模型") failed with the message

    [compile] handler reported failure

and the user's report was "一直有报错" — that string names no cause and no feature,
so neither the model nor the person could act on it. The normalisation below is
where that string came from; it lives in the zero-dependency wire module so it can
be tested without FreeCAD.

Two rules:

  * a handler's own structured error wins, in full (kind, message, feature_id);
  * if there is no usable detail, the fallback still says what the payload
    contained — a dead end is the one thing it must not be.
"""

from __future__ import annotations

from tcad.worker.protocol import first_error, no_detail_message

# ══════════════════════════════════════════════════════════════════════════
# picking the error out of a handler's payload
# ══════════════════════════════════════════════════════════════════════════


def test_the_first_structured_error_is_used_verbatim():
    """compile/export collect several errors; the first one is the actionable one."""
    result = {
        "ok": False,
        "errors": [
            {"kind": "compile", "message": "sketch 'sk1' does not form a closed wire",
             "feature_id": "sk1"},
            {"kind": "compile", "message": "second, less specific"},
        ],
    }
    detail = first_error(result)
    assert detail["message"] == "sketch 'sk1' does not form a closed wire"
    assert detail["feature_id"] == "sk1"
    assert detail["kind"] == "compile"


def test_a_plain_string_error_is_carried_through():
    """tessellate reports `error` as a string."""
    assert first_error({"ok": False, "error": "no mesh"})["message"] == "no mesh"


def test_an_error_dict_is_carried_through():
    assert first_error(
        {"ok": False, "error": {"kind": "runtime", "message": "boom"}}
    )["message"] == "boom"


def test_a_non_dict_entry_in_errors_is_still_readable():
    assert first_error({"ok": False, "errors": ["flat string"]})["message"] == "flat string"


def test_no_detail_at_all_yields_an_empty_dict():
    """The caller must be able to tell "nothing to show" from "showed something"."""
    assert first_error({"ok": False, "measurements": {}}) == {}


# ══════════════════════════════════════════════════════════════════════════
# the last-resort wording
# ══════════════════════════════════════════════════════════════════════════


def test_the_fallback_names_what_the_payload_contained():
    """Not "handler reported failure" — that was the whole complaint."""
    message = no_detail_message(
        {"ok": False, "errors": [], "fcstd": None, "measurements": {"solids": 0},
         "elapsed_s": 0.02}
    )
    assert "handler reported failure" not in message.replace(
        "handler reported failure with no error detail", ""
    ), "回退文案仍然只是一个没有信息量的短语"
    assert "errors" in message and "measurements" in message
    assert "elapsed_s" not in message, "把耗时也算进「结果里有什么」是噪音"


def test_the_fallback_survives_an_empty_payload():
    message = no_detail_message({"ok": False})
    assert "none" in message
    assert len(message) > 20


def test_repair_guidance_survives_rpc_validation_and_tool_client():
    from tcad.core.types import RpcResponse
    from tcad.core.worker_client import WorkerCallFailed
    from tcad.core.wiring import SyncWorkerClient

    response = RpcResponse.model_validate({'id': 1, 'ok': False, 'error': {
        'kind': 'compile', 'message': 'Fillet invalid', 'feature_id': 'rounded',
        'hint': 'Use the preceding solid feature; reduce radius.'}})

    class Handle:
        def request_sync(self, *args, **kwargs):
            raise WorkerCallFailed(response.error)

    result = SyncWorkerClient(Handle()).request('compile_ir', {})
    assert result['error']['hint'] == response.error.hint
