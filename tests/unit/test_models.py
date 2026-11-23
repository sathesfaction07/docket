from case_forge.models import GMS, VALID_TRANSITIONS, Status


class TestGMS:
    def test_basic_computation(self):
        gms = GMS(shipped_units=100, unit_price=15.0)
        assert gms.t4w_gms == 1500.0
        assert gms.is_high_value is True

    def test_all_components(self):
        gms = GMS(
            shipped_units=50, unit_price=10.0,
            shipping_charge=100, gift_wrap_charge=20,
            price_discount=-50, shipping_discount=-10, gift_wrap_discount=-5,
        )
        # (50*10) + 100 + 20 + (-50) + (-10) + (-5) = 500 + 55 = 555
        assert gms.t4w_gms == 555.0
        assert gms.is_high_value is False

    def test_threshold_boundary(self):
        gms = GMS(shipped_units=100, unit_price=10.0)
        assert gms.t4w_gms == 1000.0
        assert gms.is_high_value is True

    def test_below_threshold(self):
        gms = GMS(shipped_units=99, unit_price=10.0)
        assert gms.t4w_gms == 990.0
        assert gms.is_high_value is False

    def test_zero_values(self):
        gms = GMS()
        assert gms.t4w_gms == 0.0
        assert gms.is_high_value is False


class TestStatusTransitions:
    def test_new_can_go_to_in_progress(self):
        assert Status.IN_PROGRESS in VALID_TRANSITIONS[Status.NEW]

    def test_new_cannot_go_to_resolved(self):
        assert Status.RESOLVED not in VALID_TRANSITIONS[Status.NEW]

    def test_under_review_can_resolve(self):
        assert Status.RESOLVED in VALID_TRANSITIONS[Status.UNDER_REVIEW]

    def test_resolved_can_reopen(self):
        assert Status.REOPENED in VALID_TRANSITIONS[Status.RESOLVED]

    def test_closed_cannot_transition(self):
        assert len(VALID_TRANSITIONS[Status.CLOSED]) == 0

    def test_reopened_goes_to_in_progress(self):
        assert Status.IN_PROGRESS in VALID_TRANSITIONS[Status.REOPENED]
