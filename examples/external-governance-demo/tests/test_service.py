from demo_app.service import calculate_total


def test_calculate_total_adds_tax():
    total = calculate_total([{"price": 10, "quantity": 2}])
    assert total == 22.6

