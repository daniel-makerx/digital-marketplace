from algopy import UInt64, ImmutableArray, subroutine, uenumerate

from smart_contracts.digital_marketplace.contract import PlacedBid, SaleKey


@subroutine
def sales_box_mbr(prefix_length: UInt64) -> UInt64:
    # fmt: off
    return 2_500 + 400 * (
        # Domain separator
        prefix_length +
        # SaleKey
        32 + 8 +
        # Sale
        # amount & cost fields
        8 + 8 +
        # Bid
        (32 + 8)
    )
    # fmt: on


@subroutine
def placed_bids_box_mbr() -> UInt64:
    return UInt64(
        2_500
        + 400
        * (
            # assuming it's possible to fill an entire box
            64 + 32768
        )
    )


@subroutine
def find_placed_bid(placed_bids: ImmutableArray[PlacedBid], key: SaleKey) -> tuple[bool, UInt64]:
    for i, placed_bid in uenumerate(placed_bids):
        if placed_bid.sale_key == key:
            return True, i
    return False, UInt64(0)
