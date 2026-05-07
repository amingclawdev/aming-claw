import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_app.service import calculate_total


def test_calculate_total_adds_tax():
    total = calculate_total([{"price": 10, "quantity": 2}])
    assert total == 22.6
