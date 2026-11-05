"""Compose the geographic and regional-index enrichments for final records."""

from __future__ import annotations

from dataclasses import replace

from .enrichment import GeoEnricher
from .market_index import MarketIndexSnapshot
from .models import TransactionRecord


class FinalRecordEnricher:
    """Apply the agreed geographic factors and official regional price index.

    The public column retains the user's requested name
    ``district_price_index``. Its source is HKP's published regional monthly
    series, rather than an unavailable neighbourhood-level historic series.
    """

    def __init__(
        self,
        geographic: GeoEnricher,
        market_index: MarketIndexSnapshot | None,
    ) -> None:
        self.geographic = geographic
        self.market_index = market_index
        # Exposed for summary accounting only; no key is stored here.
        self.google_maps = geographic.google_maps

    @property
    def geo_snapshot_date(self) -> str:
        return self.geographic.snapshot.snapshot_date

    def enrich(self, record: TransactionRecord) -> TransactionRecord:
        return self.geographic.enrich(self.index_record(record))

    def index_record(self, record: TransactionRecord) -> TransactionRecord:
        """Apply only the official index, without consuming Google requests."""

        if self.market_index is not None:
            return replace(
                record,
                district_price_index=self.market_index.value_for(
                    record.region,
                    record.transaction_date,
                ),
            )
        return record
