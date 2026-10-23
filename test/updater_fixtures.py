"""Small deterministic source transport; never a production coverage certification."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from core.catalog_sources import get_profile, parse_source_row
from src.g2b_client import SearchPage


class FixtureSource:
    def __init__(self, rows, tick=0):
        self.rows = rows
        self.tick = tick
        self.calls = []

    def fetch(self, query):
        self.calls.append(query)
        rows = [r for r in self.rows if not query.start_price or
                Decimal(query.start_price) <= Decimal(str(r['ctrtUprc'])) <= Decimal(query.end_price)]
        start = (query.page - 1) * query.page_size
        sliced = rows[start:start + query.page_size]
        return SearchPage(tuple(parse_source_row(r) for r in sliced), {'rsltList': sliced, 'totalSize':len(rows)},
                          len(rows), len(rows), get_profile('shopping-all'),
                          datetime(2026, 10, 20, tzinfo=timezone.utc) + timedelta(hours=self.tick))


def row(key='A', price='100', spec='10mm'):
    return dict(ctrtItemMngNo=key, itemIdnfNo='goods1', itemIdnfNm='품목', itemIndfNmView='품목',
                ctrtUprc=price, ctrtUntVal='개', ctentUntyGrpNm='Seller', mnftrEtpsNm='Maker',
                pdctAtrbCdDtlNm=spec, itemClsfNo='123', rprsCtrtItemMngNo='', ctrtNo='C',
                ctrtBgngYmd='20260101', ctrtEndYmd='20261231')
