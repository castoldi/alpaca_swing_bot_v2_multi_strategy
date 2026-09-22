"""The ablation's variants are isolated instances; live weights never change."""
from research import ensemble_vote_ablation as ablation
from strategies.ensemble import ENSEMBLE_WEIGHTS, EnsembleStrategy


def test_variants_are_predeclared_and_do_not_mutate_live_weights():
    before = dict(ENSEMBLE_WEIGHTS)
    panel = ablation.variants()

    assert list(panel) == ["A_live", "B_two_votes", "C_regime_only", "D_equal"]
    assert panel["B_two_votes"][1].ensemble_min_votes == 2
    assert panel["C_regime_only"][0].weights["regime"] == before["regime"]
    assert sum(panel["C_regime_only"][0].weights.values()) == before["regime"]
    assert set(panel["D_equal"][0].weights.values()) == {0.20}
    assert ENSEMBLE_WEIGHTS == before
    assert EnsembleStrategy().weights == before
