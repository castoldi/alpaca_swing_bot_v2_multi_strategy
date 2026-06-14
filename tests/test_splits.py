import strategy as S

def test_split_take_profit_thirds():
    tp1, tp2, tp3 = S.split_take_profit(100.0, 108.0)
    assert round(tp1, 4) == 102.6667
    assert round(tp2, 4) == 105.3333
    assert tp3 == 108.0

def test_split_take_profit_zero_distance():
    assert S.split_take_profit(100.0, 100.0) == (100.0, 100.0, 100.0)

def test_split_qty_divisible():
    assert S.split_qty(9) == [3, 3, 3]

def test_split_qty_remainder_on_last():
    assert S.split_qty(10) == [3, 3, 4]
    assert S.split_qty(4) == [1, 1, 2]

def test_split_qty_below_three_is_empty():
    assert S.split_qty(2) == []
    assert S.split_qty(0) == []
