"""
ponytail 자체검증: plan_windows()가 가격범위를 빈틈/중복 없이 덮고,
각 리프가 SAFE 이하(또는 단일가격)인지 확인. 네트워크 불필요(count를 스텁으로 대체).
  실행:  python test_crawl.py
"""
import crawl_all


def _run(distribution, lo, hi, safe):
    """distribution(price)->count 로 count를 스텁하고 plan_windows 실행."""
    def fake_count(session, a, b):
        return sum(distribution(p) for p in range(a, b + 1))
    crawl_all.count = fake_count
    crawl_all.SAFE = safe
    out = []
    crawl_all.plan_windows(None, lo, hi, out)
    return sorted(out)


def test_covers_contiguously_and_bounded():
    # 가격단위당 1건 균일분포
    leaves = _run(lambda p: 1, 0, 999, safe=100)
    assert leaves[0][0] == 0 and leaves[-1][1] == 999          # 양끝 커버
    for (a, b, tot), (na, nb, ntot) in zip(leaves, leaves[1:]):
        assert b + 1 == na, (b, na)                            # 빈틈/중복 없음
    for lo, hi, tot in leaves:
        assert tot <= 100 or lo == hi, (lo, hi, tot)           # 각 리프 SAFE 이하


def test_total_count_conserved():
    # 한 가격(500)에 몰린 스파이크 + 나머지 균일
    dist = lambda p: 5000 if p == 500 else 1
    leaves = _run(dist, 0, 999, safe=100)
    total = sum(t for _, _, t in leaves)
    assert total == 999 + 5000, total                         # 999개 단일건 + 스파이크5000
    # 스파이크는 단일가격 리프로 떨어져야 함(더 못 쪼갬)
    spike = [w for w in leaves if w[0] <= 500 <= w[1]]
    assert spike == [(500, 500, 5000)], spike


def test_empty_range_skipped():
    leaves = _run(lambda p: 0, 0, 999, safe=100)
    assert leaves == []                                        # 0건이면 리프 없음


if __name__ == "__main__":
    test_covers_contiguously_and_bounded()
    test_total_count_conserved()
    test_empty_range_skipped()
    print("ok")
