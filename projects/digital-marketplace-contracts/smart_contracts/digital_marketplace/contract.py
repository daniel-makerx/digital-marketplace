import typing

from algopy import (
    Account,
    ARC4Contract,
    Asset,
    BoxMap,
    Global,
    ImmutableArray,
    LocalState,
    OnCompleteAction,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    subroutine,
    urange,
)
from algopy.arc4 import abimethod

import smart_contracts.digital_marketplace.errors as err
from smart_contracts.digital_marketplace.subroutines import (
    find_placed_bid,
    placed_bids_box_mbr,
    sales_box_mbr,
)


# TODO: convert these structs to NamedTuples instead once tuples can be serialized directly
#       this should result in cleaner code as native types such as Asset, Account and UInt64 can be used directly
#       in the mean time the frozen=True property makes this type immutable and usable within an ImmutableArray
class SaleKey(arc4.Struct, frozen=True):
    owner: arc4.Address
    asset: arc4.UInt64


class Bid(arc4.Struct, frozen=True):
    bidder: arc4.Address
    amount: arc4.UInt64


class Sale(arc4.Struct, frozen=True):
    amount: arc4.UInt64
    cost: arc4.UInt64
    # Ideally we'd like to write:
    #  bid: Optional[Bid]
    # Since there's no Optional in Algorand Python, we use the truthiness of bid.bidder
    # to know if a bid is present
    bid: Bid


class PlacedBid(arc4.Struct, frozen=True):
    sale_key: SaleKey
    bid_amount: arc4.UInt64


class UnencumberedBidsReceipt(typing.NamedTuple):
    total_bids: UInt64
    unencumbered_bids: UInt64


class DigitalMarketplace(ARC4Contract):
    def __init__(self) -> None:
        self.deposited = LocalState(UInt64)

        # TODO: once puyapy supports serialization of native tuples
        #       then NamedTuples can be used here instead of ARC-4 types
        self.sales = BoxMap(SaleKey, Sale)
        self.placed_bids = BoxMap(Account, ImmutableArray[PlacedBid])

    @abimethod(allow_actions=["NoOp", "OptIn"])
    def deposit(self, payment: gtxn.PaymentTransaction) -> None:
        assert payment.sender == Txn.sender, err.DIFFERENT_SENDER
        assert payment.receiver == Global.current_application_address, err.WRONG_RECEIVER

        self.deposited[Txn.sender] = self.deposited.get(Txn.sender, default=UInt64(0)) + payment.amount

    @abimethod(allow_actions=["NoOp", "CloseOut"])
    def withdraw(self, amount: arc4.UInt64) -> None:
        if Txn.on_completion == OnCompleteAction.NoOp:
            self.deposited[Txn.sender] -= amount.native

            itxn.Payment(receiver=Txn.sender, amount=amount.native).submit()
        else:
            itxn.Payment(receiver=Txn.sender, amount=self.deposited[Txn.sender]).submit()

    @abimethod
    def sponsor_asset(self, asset: Asset) -> None:
        assert not Global.current_application_address.is_opted_in(asset), err.ALREADY_OPTED_IN
        assert asset.clawback == Global.zero_address, err.CLAWBACK_ASA

        self.deposited[Txn.sender] -= Global.asset_opt_in_min_balance

        itxn.AssetTransfer(
            xfer_asset=asset,
            asset_receiver=Global.current_application_address,
            asset_amount=0,
        ).submit()

    @abimethod
    def open_sale(self, asset_deposit: gtxn.AssetTransferTransaction, cost: arc4.UInt64) -> None:
        assert asset_deposit.sender == Txn.sender, err.DIFFERENT_SENDER
        assert asset_deposit.asset_receiver == Global.current_application_address, err.WRONG_RECEIVER

        sale_key = SaleKey(arc4.Address(Txn.sender), arc4.UInt64(asset_deposit.xfer_asset.id))
        assert sale_key not in self.sales, err.SALE_ALREADY_EXISTS

        self.deposited[Txn.sender] -= sales_box_mbr(self.sales.key_prefix.length)

        self.sales[sale_key] = Sale(
            arc4.UInt64(asset_deposit.asset_amount),
            cost,
            Bid(bidder=arc4.Address(), amount=arc4.UInt64(0)),
        )

    @abimethod(allow_actions=["NoOp", "OptIn"])
    def close_sale(self, asset: Asset) -> None:
        sale_key = SaleKey(arc4.Address(Txn.sender), arc4.UInt64(asset.id))

        itxn.AssetTransfer(
            xfer_asset=asset,
            asset_receiver=Txn.sender,
            asset_amount=self.sales[sale_key].amount.native,
        ).submit()

        self.deposited[Txn.sender] = self.deposited.get(Txn.sender, default=UInt64(0)) + sales_box_mbr(
            self.sales.key_prefix.length
        )

        del self.sales[sale_key]

    @abimethod
    def buy(self, sale_key: SaleKey) -> None:
        assert Txn.sender != sale_key.owner.native, err.SELLER_CANT_BE_BUYER

        self.deposited[Txn.sender] -= self.sales[sale_key].cost.native
        self.deposited[sale_key.owner.native] += self.sales[sale_key].cost.native + sales_box_mbr(
            self.sales.key_prefix.length
        )

        itxn.AssetTransfer(
            xfer_asset=sale_key.asset.native,
            asset_receiver=Txn.sender,
            asset_amount=self.sales[sale_key].amount.native,
        ).submit()

        del self.sales[sale_key]

    @abimethod
    def bid(self, sale_key: SaleKey, new_bid_amount: arc4.UInt64) -> None:
        arc4_sender = arc4.Address(Txn.sender)
        new_bid = Bid(bidder=arc4_sender, amount=new_bid_amount)

        assert arc4_sender != sale_key.owner, err.SELLER_CANT_BE_BIDDER

        sale = self.sales[sale_key]
        if sale.bid.bidder:
            assert sale.bid.amount.native < new_bid_amount.native, err.WORSE_BID

        self.sales[sale_key] = sale._replace(bid=new_bid)

        new_placed_bid = PlacedBid(sale_key, new_bid_amount)
        placed_bids, placed_bids_exist = self.placed_bids.maybe(Txn.sender)
        if placed_bids_exist:
            found, index = find_placed_bid(placed_bids, sale_key)
            if found:
                self.deposited[Txn.sender] += placed_bids[index].bid_amount.native
                self.placed_bids[Txn.sender] = placed_bids.replace(index, new_placed_bid)
            else:
                self.placed_bids[Txn.sender] = placed_bids.append(new_placed_bid)
        else:
            self.deposited[Txn.sender] -= placed_bids_box_mbr()
            self.placed_bids[Txn.sender] = ImmutableArray(new_placed_bid)

        self.deposited[Txn.sender] -= new_bid_amount.native

    @subroutine
    def is_encumbered(self, bid: PlacedBid) -> bool:
        sale, sale_exists = self.sales.maybe(bid.sale_key)
        return sale_exists and bool(sale.bid.bidder) and sale.bid.bidder == Txn.sender

    @abimethod(allow_actions=["NoOp", "OptIn"])
    def claim_unencumbered_bids(self) -> None:
        self.deposited[Txn.sender] = self.deposited.get(Txn.sender, UInt64(0))

        placed_bids = self.placed_bids[Txn.sender]
        encumbered_placed_bids = ImmutableArray[PlacedBid]()

        for placed_bid in placed_bids:
            if self.is_encumbered(placed_bid):
                encumbered_placed_bids = encumbered_placed_bids.append(placed_bid)
            else:
                self.deposited[Txn.sender] += placed_bid.bid_amount.native

        if encumbered_placed_bids:
            self.placed_bids[Txn.sender] = encumbered_placed_bids
        else:
            self.deposited[Txn.sender] += placed_bids_box_mbr()
            del self.placed_bids[Txn.sender]

    @abimethod(readonly=True)
    def get_total_and_unencumbered_bids(self) -> UnencumberedBidsReceipt:
        total_bids = UInt64(0)
        unencumbered_bids = UInt64(0)

        for placed_bid in self.placed_bids[Txn.sender]:
            total_bids += placed_bid.bid_amount.native
            if not self.is_encumbered(placed_bid):
                unencumbered_bids += placed_bid.bid_amount.native

        return UnencumberedBidsReceipt(total_bids, unencumbered_bids)

    @abimethod(allow_actions=["NoOp", "OptIn"])
    def accept_bid(self, asset: arc4.UInt64) -> None:
        sale_key = SaleKey(owner=arc4.Address(Txn.sender), asset=asset)
        sale = self.sales[sale_key]
        current_best_bid = sale.bid

        self.deposited[Txn.sender] = (
            self.deposited.get(Txn.sender, default=UInt64(0))
            + current_best_bid.amount.native
            + sales_box_mbr(self.sales.key_prefix.length)
        )
        itxn.AssetTransfer(
            xfer_asset=asset.native,
            asset_receiver=current_best_bid.bidder.native,
            asset_amount=sale.amount.native,
        ).submit()

        del self.sales[sale_key]
