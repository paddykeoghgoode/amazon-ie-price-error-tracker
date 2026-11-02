"""Buy-box seller validation: fulfilment by Amazon is not an Amazon sale."""
import unittest

from app.scraper.amazon_ie import (
    is_amazon_direct_offer, is_eligible_amazon_offer, eligible_offer_type, parse_product_html,
)


def offer_html(merchant_html: str, price: str = "€49.99", availability: str = "In stock",
               title: str = "Sony PS5 Controller") -> str:
    return (
        f'<span id="productTitle">{title}</span>'
        f'<div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">{price}</span></div>'
        f'<div id="availability">{availability}</div>'
        + merchant_html
    )


def parse(html: str):
    return parse_product_html(html, "B000000001", "https://www.amazon.ie/dp/B000000001")


class TestAmazonDirectSeller(unittest.TestCase):
    def test_explicit_amazon_sold_by(self):
        item = parse(offer_html('<div id="merchant-info">Dispatches from Amazon. Sold by Amazon.</div>'))
        self.assertEqual(item.seller_status, "amazon")
        self.assertTrue(is_amazon_direct_offer(item))

    def test_amazon_eu_legal_entity(self):
        item = parse(offer_html('<div id="merchant-info">Sold by Amazon EU S.à r.l. Returns within 30 days</div>'))
        self.assertEqual(item.seller_status, "amazon")
        self.assertTrue(is_amazon_direct_offer(item))

    def test_third_party_fulfilled_by_amazon_is_eligible(self):
        item = parse(offer_html('<div id="merchant-info">Dispatched from Amazon. Sold by GreatDeals Ltd.</div>'))
        self.assertEqual(item.seller_status, "third_party")
        self.assertFalse(is_amazon_direct_offer(item))
        self.assertTrue(is_eligible_amazon_offer(item))
        self.assertEqual(eligible_offer_type(item), "marketplace_fba")

    def test_amazon_mentioned_outside_buybox_is_not_proof(self):
        item = parse(offer_html('<div>Amazon fulfilled, Amazon choice, Amazon reviews</div>'))
        self.assertEqual(item.seller_status, "unknown")
        self.assertFalse(is_amazon_direct_offer(item))

    def test_third_party_seller_link_cannot_pass(self):
        item = parse(offer_html(
            '<div id="merchant-info">Sold by Cheap Gadget World '
            '<a id="sellerProfileTriggerId" href="/sp?seller=123">seller</a></div>'
        ))
        self.assertFalse(is_amazon_direct_offer(item))

    def test_tabular_buybox(self):
        item = parse(offer_html(
            '<div id="tabular-buybox">'
            '<div class="a-row"><span class="tabular-buybox-label">Sold by</span>'
            '<span class="tabular-buybox-text">Amazon</span></div></div>'
        ))
        self.assertTrue(is_amazon_direct_offer(item))

    def test_scoped_seller_profile_link_with_amazon_dispatch_is_fba(self):
        item = parse(offer_html(
            '<div id="desktop_buybox">'
            '<div>Ships from Amazon</div>'
            '<a id="sellerProfileTriggerId" href="/sp?seller=123">Gadget Store Ltd</a>'
            '</div>'
        ))
        self.assertEqual(item.seller_status, "third_party")
        self.assertEqual(item.fulfilment_status, "amazon")
        self.assertTrue(is_eligible_amazon_offer(item))

    def test_profile_link_outside_current_buybox_does_not_confirm_fba(self):
        item = parse(offer_html(
            '<div id="merchant-info">Dispatched from Amazon</div>'
            '<div id="other-offers"><a id="sellerProfileTriggerId">Gadget Store Ltd</a></div>'
        ))
        self.assertFalse(is_eligible_amazon_offer(item))

    def test_new_desktop_buybox_price_selector(self):
        item = parse(
            '<span id="productTitle">Sony Gaming Headset</span>'
            '<div id="apex_desktop"><span class="a-price"><span class="a-offscreen">€39.99</span></span></div>'
            '<div id="merchant-info">Sold by Amazon</div>'
        )
        self.assertEqual(item.price_eur, 39.99)
        self.assertTrue(is_eligible_amazon_offer(item))

    def test_third_party_seller_fulfilled_by_amazon_is_eligible(self):
        item = parse(offer_html('<div id="merchant-info">Sold by Gadget Shop. Fulfilled by Amazon.</div>'))
        self.assertEqual(item.seller_status, "third_party")
        self.assertEqual(item.fulfilment_status, "amazon")
        self.assertTrue(is_eligible_amazon_offer(item))

    def test_third_party_prime_only_does_not_qualify(self):
        item = parse(offer_html('<div id="merchant-info">Sold by Gadget Shop. Prime eligible.</div>'))
        self.assertFalse(is_eligible_amazon_offer(item))

    def test_third_party_with_fulfilment_elsewhere_does_not_qualify(self):
        item = parse(offer_html('<div id="merchant-info">Sold by Gadget Shop.</div>'
                                '<div>Other offers fulfilled by Amazon</div>'))
        self.assertFalse(is_eligible_amazon_offer(item))

    def test_third_party_seller_dispatch_unknown_does_not_qualify(self):
        item = parse(offer_html('<div id="merchant-info">Sold by Gadget Shop. Dispatched by Gadget Shop.</div>'))
        self.assertFalse(is_eligible_amazon_offer(item))

    def test_unknown_seller_fails_closed(self):
        item = parse(offer_html('<div id="merchant-info">Ships from Amazon</div>'))
        self.assertFalse(is_amazon_direct_offer(item))

    def test_renewed_or_out_of_stock_rejected(self):
        renewed = parse(offer_html('<div id="merchant-info">Sold by Amazon</div>',
                                    title="Sony PS5 Renewed Controller"))
        out = parse(offer_html('<div id="merchant-info">Sold by Amazon</div>',
                                availability="Currently unavailable"))
        self.assertFalse(is_amazon_direct_offer(renewed))
        self.assertFalse(is_amazon_direct_offer(out))

    def test_no_price_rejected(self):
        item = parse('<div id="merchant-info">Sold by Amazon</div>')
        self.assertFalse(is_amazon_direct_offer(item))


if __name__ == "__main__":
    unittest.main()
