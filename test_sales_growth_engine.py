import sales_growth_engine as growth


def test_scale_requires_real_profit_after_ads():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 20, "sales": 100, "clicks": 20, "orders": 4, "impressions": 1000},
        {
            "gross_margin": 0.40,
            "break_even_acos": 0.40,
            "profitable_acos": 0.30,
            "economics_source": "product_costs",
        },
    )
    assert item["decision"] == "SCALE"
    assert item["metrics"]["estimated_profit_after_ads"] == 20.0


def test_listing_problem_beats_more_ad_spend():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 25, "sales": 0, "clicks": 20, "orders": 0, "impressions": 1500},
        {
            "gross_margin": 0.40,
            "break_even_acos": 0.40,
            "profitable_acos": 0.30,
            "economics_source": "default_margin",
        },
    )
    assert item["decision"] == "FIX_LISTING"


def test_low_inventory_blocks_scaling():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 15, "sales": 100, "clicks": 20, "orders": 5, "impressions": 1000},
        {
            "gross_margin": 0.40,
            "break_even_acos": 0.40,
            "profitable_acos": 0.30,
            "economics_source": "product_costs",
        },
        inventory_units=5,
    )
    assert item["decision"] == "RESTOCK"


def test_borderline_winner_is_recommendation_not_scale():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 15, "sales": 50, "clicks": 10, "orders": 1, "impressions": 700},
        {
            "gross_margin": 0.45,
            "break_even_acos": 0.45,
            "profitable_acos": 0.35,
            "economics_source": "product_costs",
        },
    )
    assert item["decision"] == "PROMOTE"


def test_product_economics_uses_real_costs_when_available():
    econ = growth.product_economics({
        "Selling Price": "39.99",
        "COGS": "10.00",
        "Amazon Fees": "6.00",
        "Shipping Cost": "8.00",
    })
    assert econ["economics_source"] == "product_costs"
    assert econ["break_even_acos"] > 0
    assert econ["profitable_acos"] < econ["break_even_acos"]
