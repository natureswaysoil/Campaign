"""Enhanced Campaign Engine for Nature's Way Soil - Growth Focused
   Target ACOS: 35% | Product Margin: 40%"""

from __future__ import annotations
import re
from typing import Dict, List, Any, Optional
from datetime import datetime
import pandas as pd


class CampaignEngine:
    def __init__(
        self,
        target_acos: float = 0.35,
        product_margin: float = 0.40,
        max_bid: float = 3.00,          # Higher ceiling for aggressive growth
        min_bid: float = 0.30,
    ):
        self.target_acos = target_acos
        self.product_margin = product_margin
        self.max_bid = max_bid
        self.min_bid = min_bid

    def generate_long_tail_keywords(self, product: Dict[str, str], num_variations: int = 12) -> List[str]:
        """Generate high-intent long-tail keywords using product attributes."""
        title = product.get("title", "").lower()
        attributes = [
            product.get("brand", ""),
            product.get("category", ""),
            product.get("type", ""),
            product.get("size", ""),
            product.get("feature", ""),
            product.get("organic", ""),
            product.get("living", ""),
        ]
        
        base_words = re.findall(r'\w+', title)[:8]
        long_tails = set()

        # Core long-tails
        for attr in attributes:
            if attr and len(attr) > 2:
                for base in base_words:
                    if base in ["soil", "compost", "fertilizer", "organic"]:
                        long_tails.add(f"{base} {attr.lower()}".strip())
                        long_tails.add(f"organic {base} {attr.lower()}".strip())

        # Common high-conversion patterns
        patterns = [
            f"{title.split()[0]} for raised beds",
            f"best {title.split()[0]} for vegetables",
            f"organic living {title.split()[0]}",
            f"{title.split()[0]} with beneficial microbes",
        ]
        
        long_tails.update([p.lower() for p in patterns])
        
        return list(long_tails)[:num_variations]

    def build_broad_match_campaign(self, product: Dict[str, Any], search_terms_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """Create Broad Match campaign with tight negative controls."""
        campaign_name = f"Broad - {product.get('sku', 'PRODUCT')} - Discovery"
        
        keywords = self.generate_long_tail_keywords(product, num_variations=15)
        
        # Strong negatives (from poor search terms)
        negatives = []
        if search_terms_df is not None:
            bad_terms = search_terms_df[
                (search_terms_df.get("acos", 999) > self.target_acos * 1.8) |
                (search_terms_df.get("orders", 0) == 0) & (search_terms_df.get("clicks", 0) > 8)
            ]["search_term"].head(30).tolist()
            negatives = [{"keywordText": term, "matchType": "negativePhrase"} for term in bad_terms]

        return {
            "campaignName": campaign_name,
            "campaignType": "sponsoredProducts",
            "targetingType": "auto",           # or manual for more control
            "matchType": "broad",
            "dailyBudget": 25.0,
            "keywords": [{"keywordText": kw, "matchType": "broad"} for kw in keywords],
            "negativeKeywords": negatives,
            "biddingStrategy": "dynamicBidsUpAndDown",
            "premiumBidAdjustment": True
        }

    def harvest_search_terms_to_exact_phrase(self, search_terms_df: pd.DataFrame, min_orders: int = 2, 
                                           max_acos: float = 0.32) -> List[Dict]:
        """Auto-harvest top search terms into Exact & Phrase match."""
        winners = search_terms_df[
            (search_terms_df.get("orders", 0) >= min_orders) &
            (search_terms_df.get("acos", 999) <= max_acos) &
            (search_terms_df.get("clicks", 0) >= 5)
        ].copy()

        harvested = []
        for _, row in winners.iterrows():
            term = str(row.get("search_term") or row.get("keywordText"))
            if len(term.split()) > 5:  # prefer longer tails for exact
                match_type = "exact"
            else:
                match_type = "phrase"

            harvested.append({
                "keywordText": term,
                "matchType": match_type,
                "bid": round(float(row.get("bid", 1.2)) * 1.15, 2),   # slight bid boost
                "campaignName": f"Harvested - {term[:40]}",
                "reason": f"{row.get('orders',0)} orders @ ACOS {row.get('acos',0):.1%}"
            })
        
        return harvested[:50]  # limit per run

    def build_campaign_structure(self, products: List[Dict], search_terms_df: Optional[pd.DataFrame] = None) -> Dict[str, List]:
        """Main function: Build full campaign structure."""
        structure = {
            "broad_campaigns": [],
            "harvested_exact_phrase": [],
            "existing_campaigns_to_scale": []
        }

        for product in products:
            # 1. Broad Match Discovery Campaign
            broad_camp = self.build_broad_match_campaign(product, search_terms_df)
            structure["broad_campaigns"].append(broad_camp)

            # 2. Harvest winners into Exact/Phrase
            if search_terms_df is not None:
                harvested = self.harvest_search_terms_to_exact_phrase(search_terms_df)
                structure["harvested_exact_phrase"].extend(harvested)

        return structure

    def decide_bid(self, row: Dict[str, Any], current_bid: float) -> Dict[str, Any]:
        """Aggressive bid decision (integrated with scaling logic)."""
        orders = int(row.get("orders", 0) or row.get("purchases7d", 0))
        acos = float(row.get("acos", 999))
        clicks = int(row.get("clicks", 0))
        conv_rate = (orders / clicks) if clicks > 0 else 0.0

        new_bid = current_bid

        # === AGGRESSIVE GROWTH LOGIC ===
        if orders >= 3 and acos <= self.target_acos * 0.90:
            multiplier = 1.32 if conv_rate >= 0.13 else 1.24
            new_bid = round(min(current_bid * multiplier, self.max_bid), 2)

        elif orders >= 2 and acos <= self.target_acos * 0.95:
            new_bid = round(min(current_bid * 1.22, self.max_bid), 2)

        elif orders >= 1 and acos <= self.target_acos * 1.08:   # Volume ramp near target
            new_bid = round(min(current_bid * 1.15, self.max_bid), 2)

        # Conservative downscaling
        elif acos > self.target_acos * 1.45 or (clicks > 15 and orders == 0):
            new_bid = round(max(current_bid * 0.75, self.min_bid), 2)

        action = "increase" if new_bid > current_bid else "decrease" if new_bid < current_bid else "hold"

        return {
            "action": action,
            "new_bid": new_bid,
            "reason": f"Orders: {orders}, ACOS: {acos:.1%}, CR: {conv_rate:.1%}"
        }


# ====================== USAGE EXAMPLE ======================
if __name__ == "__main__":
    engine = CampaignEngine(
        target_acos=0.35,
        product_margin=0.40,
        max_bid=3.50,
        min_bid=0.30
    )

    # Example call
    products = [...]          # Load from your CSV / Sheet
    search_terms = pd.read_csv("search_term_report.csv")   # or from API

    structure = engine.build_campaign_structure(products, search_terms)
    
    print(f"Created {len(structure['broad_campaigns'])} Broad campaigns")
    print(f"Harvested {len(structure['harvested_exact_phrase'])} winning keywords")
