import unittest
from datetime import datetime
from unittest.mock import patch

import server


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 4, 29, 12, 0, 0, tzinfo=tz)


class ServerTests(unittest.TestCase):
    def test_get_meal_plan_includes_recent_past_and_product_details(self):
        responses = {
            ("get", "/objects/meal_plan"): [
                {
                    "id": 1,
                    "day": "2026-04-28",
                    "product_id": 10,
                    "product_amount": 150,
                    "product_qu_id": 1,
                    "section_id": 2,
                    "type": "product",
                },
                {
                    "id": 2,
                    "day": "2026-04-29",
                    "recipe_id": 20,
                    "section_id": 1,
                    "type": "recipe",
                },
            ],
            ("get", "/objects/recipes"): [{"id": 20, "name": "Omelette"}],
            ("get", "/objects/products"): [{"id": 10, "name": "Greek Yogurt"}],
            ("get", "/objects/meal_plan_sections"): [
                {"id": 1, "name": "Breakfast"},
                {"id": 2, "name": "Lunch"},
            ],
            ("get", "/objects/quantity_units"): [{"id": 1, "name": "gram"}],
            ("get", "/userfields/products/10"): {},
            ("get", "/objects/recipes_pos"): [],
        }

        def fake_api(method, path, **kwargs):
            return responses[(method, path)]

        with (
            patch.object(server, "api", side_effect=fake_api),
            patch.object(server, "datetime", FixedDateTime),
        ):
            plan = server.get_meal_plan(days_ahead=0, days_back=1)

        self.assertEqual(
            plan,
            [
                {
                    "date": "2026-04-28",
                    "section": "Lunch",
                    "type": "product",
                    "product": "Greek Yogurt",
                    "amount": 150.0,
                    "unit": "gram",
                    "day_total_incomplete": True,
                },
                {
                    "date": "2026-04-29",
                    "section": "Breakfast",
                    "type": "recipe",
                    "recipe": "Omelette",
                    "day_total_incomplete": True,
                },
            ],
        )

    def test_create_product_accepts_unit_alias(self):
        created_payloads = []

        def fake_api(method, path, **kwargs):
            if method == "get" and path == "/objects/quantity_units":
                return [{"id": 1, "name": "gram", "name_plural": "grams"}]
            if method == "get" and path == "/objects/locations":
                return [{"id": 2, "name": "Pantry"}]
            if method == "post" and path == "/objects/products":
                created_payloads.append(kwargs["json"])
                return {"created_object_id": 99}
            raise AssertionError(f"Unexpected API call: {method} {path}")

        with patch.object(server, "api", side_effect=fake_api):
            result = server.create_product("Chia Seeds", default_unit="g", location="Pantry")

        self.assertEqual(
            created_payloads,
            [
                {
                    "name": "Chia Seeds",
                    "qu_id_stock": 1,
                    "qu_id_purchase": 1,
                    "qu_id_consume": 1,
                    "qu_id_price": 1,
                    "location_id": 2,
                }
            ],
        )
        self.assertEqual(result, "Created product 'Chia Seeds' — unit: gram, location: Pantry.")

    def test_purchase_accepts_unit_alias_for_new_product(self):
        created = False
        created_payloads = []
        stock_payloads = []

        def fake_api(method, path, **kwargs):
            nonlocal created
            if method == "get" and path == "/objects/products":
                if created:
                    return [{"id": 99, "name": "Chia Seeds", "qu_id_stock": 1}]
                return []
            if method == "get" and path == "/objects/quantity_units":
                return [{"id": 1, "name": "gram", "name_plural": "grams"}]
            if method == "get" and path == "/objects/locations":
                return [{"id": 2, "name": "Pantry"}]
            if method == "post" and path == "/objects/products":
                created = True
                created_payloads.append(kwargs["json"])
                return {"created_object_id": 99}
            if method == "post" and path == "/stock/products/99/add":
                stock_payloads.append(kwargs["json"])
                return None
            raise AssertionError(f"Unexpected API call: {method} {path}")

        with patch.object(server, "api", side_effect=fake_api):
            result = server.purchase(
                "Chia Seeds",
                amount=500,
                use_by="never",
                location="Pantry",
                default_unit="g",
            )

        self.assertEqual(created_payloads[0]["qu_id_stock"], 1)
        self.assertEqual(
            stock_payloads,
            [{"amount": 500, "transaction_type": "purchase", "best_before_date": "2999-12-31"}],
        )
        self.assertEqual(
            result,
            "Added 500 gram × 'Chia Seeds' — no expiry — [new product: unit=gram, location=Pantry].",
        )

    def test_delete_meal_plan_entry_deletes_product_entries(self):
        deleted_paths = []

        def fake_api(method, path, **kwargs):
            if method == "get" and path == "/objects/meal_plan":
                return [
                    {
                        "id": 7,
                        "day": "2026-04-28",
                        "product_id": 10,
                        "section_id": 2,
                        "type": "product",
                    }
                ]
            if method == "get" and path == "/objects/recipes":
                return []
            if method == "get" and path == "/objects/products":
                return [{"id": 10, "name": "Protein Bar"}]
            if method == "get" and path == "/objects/meal_plan_sections":
                return [{"id": 2, "name": "Lunch"}]
            if method == "delete" and path == "/objects/meal_plan/7":
                deleted_paths.append(path)
                return None
            raise AssertionError(f"Unexpected API call: {method} {path}")

        with patch.object(server, "api", side_effect=fake_api):
            result = server.delete_meal_plan_entry(
                "2026-04-28",
                "Protein Bar",
                entry_type="product",
                section="Lunch",
            )

        self.assertEqual(deleted_paths, ["/objects/meal_plan/7"])
        self.assertEqual(result, "Removed product 'Protein Bar' from 2026-04-28 [Lunch].")


if __name__ == "__main__":
    unittest.main()
