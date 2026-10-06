import pytest

from tcad.context.digest import render_digest_text
from tcad.core.types import CheckStatus
from tcad.verify.checks_solid import SketchFullyConstrainedCheck
from tests.fixtures.gate_fixtures import make_digest, make_ir
from tests.unit.test_verify_checks import _ctx


@pytest.mark.parametrize("status", [-6,-5,-4,-3,-2,-1,1,2,3])
def test_sketch_object_failure_is_not_a_fully_constrained_success(tmp_path, status):
    digest = make_digest()
    digest.key_dimensions["sk_base__fully_constrained"] = 1
    digest.key_dimensions["sk_base__solve_status"] = status
    result = SketchFullyConstrainedCheck().run(_ctx(tmp_path, digest=digest))
    assert result.status == CheckStatus.FAIL and result.feature_id == "sk_base"
    assert "SOLVER-FAILED" in render_digest_text(digest, make_ir())


def test_legacy_digest_without_solver_status_remains_readable(tmp_path):
    digest = make_digest()
    digest.key_dimensions["sk_base__fully_constrained"] = 1
    assert SketchFullyConstrainedCheck().run(_ctx(tmp_path,digest=digest)).status == CheckStatus.PASS
