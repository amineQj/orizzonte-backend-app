"""Audience-aware serializers for price negotiation.

The driver sees the commission rate, the platform commission and their net;
the client sees the total amounts only. Neither audience receives any personal
data (no names, e-mails or phone numbers) from these serializers.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import Negotiation, NegotiationOffer
from .negotiation import ROLE_DRIVER, STATUS_OPEN, expected_role
from .pricing import CURRENCY, commission_split, to_decimal_string


def _requester_role(negotiation: Negotiation, context) -> str | None:
    request = context.get("request")
    user = getattr(request, "user", None)
    return negotiation.requester_role(user)


def _offers(negotiation: Negotiation) -> list[NegotiationOffer]:
    # ``prefetch_related("offers")`` makes this cheap; the relationship is
    # capped at three rows anyway.
    return list(negotiation.offers.all())


class NegotiationOfferSerializer(serializers.ModelSerializer):
    commission_amount = serializers.SerializerMethodField()
    driver_net = serializers.SerializerMethodField()

    class Meta:
        model = NegotiationOffer
        fields = [
            "id",
            "sequence",
            "amount",
            "author_role",
            "is_final",
            "commission_amount",
            "driver_net",
            "created_at",
        ]
        read_only_fields = fields

    def _split(self, obj: NegotiationOffer) -> tuple[int, int]:
        return commission_split(obj.amount, obj.negotiation.commission_rate)

    def get_commission_amount(self, obj: NegotiationOffer) -> int:
        commission, _net = self._split(obj)
        return commission

    def get_driver_net(self, obj: NegotiationOffer) -> int:
        _commission, net = self._split(obj)
        return net

    def to_representation(self, instance: NegotiationOffer) -> dict:
        data = super().to_representation(instance)
        if _requester_role(instance.negotiation, self.context) != ROLE_DRIVER:
            data.pop("commission_amount", None)
            data.pop("driver_net", None)
        return data


class NegotiationSerializer(serializers.ModelSerializer):
    currency = serializers.SerializerMethodField()
    current_amount = serializers.SerializerMethodField()
    offer_count = serializers.SerializerMethodField()
    is_final = serializers.SerializerMethodField()
    your_turn = serializers.SerializerMethodField()
    offers = serializers.SerializerMethodField()
    hold_expires_at = serializers.SerializerMethodField()
    commission_rate = serializers.SerializerMethodField()
    commission_amount = serializers.SerializerMethodField()
    driver_net = serializers.SerializerMethodField()

    class Meta:
        model = Negotiation
        fields = [
            "id",
            "mission_id",
            "status",
            "currency",
            "initial_price",
            "current_amount",
            "min_amount",
            "max_amount",
            "min_fare",
            "max_offers",
            "min_step",
            "offer_count",
            "is_final",
            "your_turn",
            "offers",
            "calibrated",
            "config_version",
            "expires_at",
            "hold_expires_at",
            "accepted_amount",
            "commission_rate",
            "commission_amount",
            "driver_net",
            "created_at",
            "accepted_at",
            "closed_at",
        ]
        read_only_fields = fields

    def get_currency(self, obj: Negotiation) -> str:
        return CURRENCY

    def get_current_amount(self, obj: Negotiation) -> int | None:
        offers = _offers(obj)
        return offers[-1].amount if offers else None

    def get_offer_count(self, obj: Negotiation) -> int:
        return len(_offers(obj))

    def get_is_final(self, obj: Negotiation) -> bool:
        return len(_offers(obj)) >= obj.max_offers

    def get_your_turn(self, obj: Negotiation) -> bool:
        if obj.status != STATUS_OPEN:
            return False
        role = _requester_role(obj, self.context)
        if role is None:
            return False
        offers = _offers(obj)
        last_role = offers[-1].author_role if offers else None
        return role == expected_role(last_role)

    def get_offers(self, obj: Negotiation) -> list[dict]:
        return NegotiationOfferSerializer(
            _offers(obj), many=True, context=self.context
        ).data

    def get_hold_expires_at(self, obj: Negotiation):
        if obj.mission.held_by_id == obj.driver_id:
            return obj.mission.hold_expires_at
        return None

    def get_commission_rate(self, obj: Negotiation) -> str:
        return to_decimal_string(obj.commission_rate)

    def _amount_for_commission(self, obj: Negotiation) -> int | None:
        if obj.accepted_amount is not None:
            return obj.accepted_amount
        offers = _offers(obj)
        return offers[-1].amount if offers else None

    def get_commission_amount(self, obj: Negotiation) -> int | None:
        if obj.commission_amount is not None:
            return obj.commission_amount
        amount = self._amount_for_commission(obj)
        if amount is None:
            return None
        commission, _net = commission_split(amount, obj.commission_rate)
        return commission

    def get_driver_net(self, obj: Negotiation) -> int | None:
        if obj.driver_net is not None:
            return obj.driver_net
        amount = self._amount_for_commission(obj)
        if amount is None:
            return None
        _commission, net = commission_split(amount, obj.commission_rate)
        return net

    def to_representation(self, instance: Negotiation) -> dict:
        data = super().to_representation(instance)
        if _requester_role(instance, self.context) != ROLE_DRIVER:
            # The client only receives the total amounts. ``current_amount``
            # and ``accepted_amount`` remain visible.
            data.pop("commission_rate", None)
            data.pop("commission_amount", None)
            data.pop("driver_net", None)
        return data
