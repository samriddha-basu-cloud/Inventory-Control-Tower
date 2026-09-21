from app.optimization.eoq import eoq, apply_lot_size_constraints


def test_eoq_classic_formula():
    # D=1000/yr, S=100, H=2 -> sqrt(2*1000*100/2) = sqrt(100000) = 316.23
    result = eoq(1000, 100, 2)
    assert abs(result["eoq"] - 316.23) < 0.05


def test_eoq_zero_holding_cost_does_not_divide_by_zero():
    result = eoq(1000, 100, 0)
    assert result["eoq"] == 0.0


def test_eoq_zero_demand():
    result = eoq(0, 100, 2)
    assert result["eoq"] == 0.0


def test_lot_size_raises_to_moq():
    result = apply_lot_size_constraints(300, moq=1000, order_multiple=1)
    assert result["final_quantity"] == 1000


def test_lot_size_rounds_to_order_multiple():
    result = apply_lot_size_constraints(2370, moq=1000, order_multiple=500)
    assert result["final_quantity"] == 2500


def test_lot_size_caps_at_capacity():
    result = apply_lot_size_constraints(5000, moq=100, order_multiple=100, capacity_constraint=3000)
    assert result["final_quantity"] == 3000
    assert result["capacity_capped"] is True


def test_lot_size_no_constraints_passthrough():
    result = apply_lot_size_constraints(1234, moq=0, order_multiple=1)
    assert result["final_quantity"] == 1234
