import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastmcp import FastMCP

GROCY_BASE_URL = os.environ.get("GROCY_BASE_URL", "http://localhost:9283").rstrip("/")
GROCY_API_KEY = os.environ.get("GROCY_API_KEY", "")

mcp = FastMCP(
    "Grocy Food Manager",
    instructions=(
        "Manage food inventory, recipes and meal plans via Grocy. "
        "Always use product names — never IDs. "
        "When a product name is ambiguous, call find_product to disambiguate before acting."
    ),
)


# ---------------------------------------------------------------------------
# Grocy API helper
# ---------------------------------------------------------------------------

def api(method: str, path: str, **kwargs):
    url = f"{GROCY_BASE_URL}/api{path}"
    headers = {"GROCY-API-KEY": GROCY_API_KEY, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30) as client:
        resp = getattr(client, method)(url, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_product(name: str) -> tuple[Optional[int], list[dict]]:
    """Return (product_id, []) on unambiguous match, (None, [matches]) otherwise."""
    products = api("get", "/objects/products")
    name_lower = name.strip().lower()
    exact = [p for p in products if p["name"].lower() == name_lower]
    if exact:
        return exact[0]["id"], []
    fuzzy = [p for p in products if name_lower in p["name"].lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]["id"], []
    return None, fuzzy


def _get_or_create_product(name: str) -> int:
    pid, matches = _find_product(name)
    if pid:
        return pid
    if matches:
        raise ValueError(
            f"Ambiguous product '{name}'. Did you mean: {[m['name'] for m in matches[:5]]}?"
        )
    units = api("get", "/objects/quantity_units")
    default_unit_id = units[0]["id"] if units else 1
    result = api("post", "/objects/products", json={
        "name": name,
        "qu_id_stock": default_unit_id,
        "qu_id_purchase": default_unit_id,
    })
    return result["created_object_id"]


def _unit_id(name: str) -> Optional[int]:
    units = api("get", "/objects/quantity_units")
    name_lower = name.lower()
    for u in units:
        if u["name"].lower() == name_lower or (u.get("name_plural") or "").lower() == name_lower:
            return u["id"]
    return None


def _recipe_by_name(name: str) -> Optional[dict]:
    recipes = api("get", "/objects/recipes")
    name_lower = name.lower()
    exact = [r for r in recipes if r["name"].lower() == name_lower]
    if exact:
        return exact[0]
    fuzzy = [r for r in recipes if name_lower in r["name"].lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None


# ---------------------------------------------------------------------------
# Tools — Inventory
# ---------------------------------------------------------------------------

@mcp.tool
def get_stock() -> list[dict]:
    """Return all items currently in stock with product name, amount, unit and best-before date."""
    items = api("get", "/stock")
    out = []
    for item in items:
        product_name = (item.get("product") or {}).get("name") or f"product_{item['product_id']}"
        unit_name = (item.get("quantity_unit_stock") or {}).get("name", "")
        bbd = item.get("best_before_date") or ""
        out.append({
            "product": product_name,
            "amount": float(item.get("stock_amount") or item.get("amount", 0)),
            "unit": unit_name,
            "best_before": None if bbd == "2999-12-31" else bbd or None,
        })
    return sorted(out, key=lambda x: x["product"])


@mcp.tool
def get_expiring_soon(days: int = 7) -> dict:
    """Return products expiring within `days` days (default 7), and already-expired products."""
    volatile = api("get", "/stock/volatile", params={"due_soon_days": days})

    def fmt(items):
        return [
            {
                "product": (item.get("product") or {}).get("name") or f"product_{item.get('product_id')}",
                "amount": float(item.get("stock_amount") or item.get("amount", 0)),
                "best_before": item.get("best_before_date"),
            }
            for item in items
        ]

    return {
        "expiring_soon": fmt(volatile.get("expiring_products", [])),
        "expired": fmt(volatile.get("expired_products", [])),
    }


@mcp.tool
def find_product(name: str) -> dict:
    """Search for products by name. Use to check existence or disambiguate before acting."""
    products = api("get", "/objects/products")
    matches = [p for p in products if name.lower() in p["name"].lower()]
    return {"matches": [{"name": p["name"]} for p in matches]}


@mcp.tool
def purchase(product_name: str, amount: float, use_by: Optional[str] = None) -> str:
    """Add stock for a product. Creates the product automatically if it doesn't exist yet.

    Args:
        product_name: Name of the product
        amount: Quantity to add
        use_by: Best-before date in YYYY-MM-DD format (optional, agent should estimate if known)
    """
    try:
        pid = _get_or_create_product(product_name)
    except ValueError as e:
        return str(e)
    payload: dict = {"amount": amount, "transaction_type": "purchase"}
    if use_by:
        payload["best_before_date"] = use_by
    api("post", f"/stock/products/{pid}/add", json=payload)
    suffix = f" (use by {use_by})" if use_by else ""
    return f"Added {amount} × '{product_name}' to stock{suffix}."


@mcp.tool
def consume(product_name: str, amount: float) -> str:
    """Consume/use stock for a product.

    Args:
        product_name: Name of the product
        amount: Quantity to consume
    """
    pid, matches = _find_product(product_name)
    if not pid:
        if matches:
            return f"Ambiguous: did you mean one of {[m['name'] for m in matches[:5]]}?"
        return f"Product '{product_name}' not found in stock."
    api("post", f"/stock/products/{pid}/consume", json={"amount": amount, "transaction_type": "consume"})
    return f"Consumed {amount} × '{product_name}'."


@mcp.tool
def adjust_stock(product_name: str, new_amount: float) -> str:
    """Set the absolute quantity of a product (manual inventory correction).

    Args:
        product_name: Name of the product
        new_amount: The correct absolute quantity
    """
    pid, matches = _find_product(product_name)
    if not pid:
        if matches:
            return f"Ambiguous: did you mean one of {[m['name'] for m in matches[:5]]}?"
        return f"Product '{product_name}' not found."
    api("post", f"/stock/products/{pid}/inventory", json={
        "new_amount": new_amount,
        "transaction_type": "inventory-correction",
    })
    return f"'{product_name}' stock set to {new_amount}."


@mcp.tool
def create_product(name: str, default_unit: str = "piece") -> str:
    """Explicitly create a new product in the system.

    Args:
        name: Product name
        default_unit: Default quantity unit (e.g. 'gram', 'piece', 'liter', 'milliliter')
    """
    uid = _unit_id(default_unit)
    if uid is None:
        units = api("get", "/objects/quantity_units")
        uid = units[0]["id"] if units else 1
    api("post", "/objects/products", json={
        "name": name,
        "qu_id_stock": uid,
        "qu_id_purchase": uid,
    })
    return f"Created product '{name}' with unit '{default_unit}'."


# ---------------------------------------------------------------------------
# Tools — Recipes
# ---------------------------------------------------------------------------

@mcp.tool
def get_recipes() -> list[dict]:
    """List all recipes, including whether each can be made with current stock."""
    recipes = api("get", "/objects/recipes")
    out = []
    for r in recipes:
        try:
            f = api("get", f"/recipes/{r['id']}/fulfillment")
            can_make = float(f.get("fulfillment_amount", 0)) >= 1
        except Exception:
            can_make = None
        out.append({
            "name": r["name"],
            "servings": r.get("desired_servings"),
            "can_make_now": can_make,
        })
    return out


@mcp.tool
def get_recipe(name: str) -> dict:
    """Get full details of a recipe including ingredient list.

    Args:
        name: Recipe name (partial match supported)
    """
    recipe = _recipe_by_name(name)
    if not recipe:
        recipes = api("get", "/objects/recipes")
        matches = [r["name"] for r in recipes if name.lower() in r["name"].lower()]
        return {"error": f"Ambiguous matches: {matches}"} if matches else {"error": f"Recipe '{name}' not found."}

    positions = api("get", "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe['id']}"})
    products = {p["id"]: p for p in api("get", "/objects/products")}
    units = {u["id"]: u["name"] for u in api("get", "/objects/quantity_units")}

    ingredients = [
        {
            "product": products.get(pos.get("product_id"), {}).get("name", f"product_{pos.get('product_id')}"),
            "amount": float(pos.get("amount", 0)),
            "unit": units.get(pos.get("qu_id")),
        }
        for pos in positions
    ]

    return {
        "name": recipe["name"],
        "servings": recipe.get("desired_servings"),
        "description": recipe.get("description") or "",
        "ingredients": ingredients,
    }


@mcp.tool
def create_recipe(
    name: str,
    servings: int,
    ingredients: list[dict],
    description: str = "",
) -> str:
    """Create a new recipe with ingredients.

    Args:
        name: Recipe name
        servings: Number of servings
        ingredients: List of {product_name: str, amount: float, unit: str (optional)}
        description: Recipe description or cooking instructions (optional)
    """
    result = api("post", "/objects/recipes", json={
        "name": name,
        "description": description,
        "desired_servings": servings,
        "base_servings": servings,
    })
    recipe_id = result["created_object_id"]
    errors = []
    for ing in ingredients:
        try:
            pid = _get_or_create_product(ing["product_name"])
            pos: dict = {
                "recipe_id": recipe_id,
                "product_id": pid,
                "amount": ing["amount"],
                "only_check_single_unit_in_stock": 0,
            }
            if ing.get("unit"):
                uid = _unit_id(ing["unit"])
                if uid:
                    pos["qu_id"] = uid
            api("post", "/objects/recipes_pos", json=pos)
        except Exception as e:
            errors.append(f"{ing.get('product_name')}: {e}")

    msg = f"Created recipe '{name}' with {len(ingredients) - len(errors)} ingredients."
    if errors:
        msg += f" Errors: {errors}"
    return msg


@mcp.tool
def update_recipe(
    name: str,
    servings: Optional[int] = None,
    ingredients: Optional[list[dict]] = None,
    description: Optional[str] = None,
) -> str:
    """Update an existing recipe. Providing ingredients replaces all existing ones.

    Args:
        name: Exact recipe name
        servings: New serving count (optional)
        ingredients: Replacement ingredient list — replaces all existing (optional)
        description: New description (optional)
    """
    recipe = _recipe_by_name(name)
    if not recipe:
        return f"Recipe '{name}' not found."
    rid = recipe["id"]

    patch = {**recipe}
    if servings is not None:
        patch["desired_servings"] = servings
        patch["base_servings"] = servings
    if description is not None:
        patch["description"] = description
    api("put", f"/objects/recipes/{rid}", json=patch)

    if ingredients is not None:
        existing = api("get", "/objects/recipes_pos", params={"query[]": f"recipe_id={rid}"})
        for pos in existing:
            api("delete", f"/objects/recipes_pos/{pos['id']}")
        for ing in ingredients:
            pid = _get_or_create_product(ing["product_name"])
            pos: dict = {
                "recipe_id": rid,
                "product_id": pid,
                "amount": ing["amount"],
                "only_check_single_unit_in_stock": 0,
            }
            if ing.get("unit"):
                uid = _unit_id(ing["unit"])
                if uid:
                    pos["qu_id"] = uid
            api("post", "/objects/recipes_pos", json=pos)

    return f"Updated recipe '{name}'."


@mcp.tool
def cook_recipe(recipe_name: str) -> str:
    """Mark a recipe as cooked — automatically consumes its ingredients from stock.

    Args:
        recipe_name: Name of the recipe
    """
    recipe = _recipe_by_name(recipe_name)
    if not recipe:
        return f"Recipe '{recipe_name}' not found."
    api("post", f"/recipes/{recipe['id']}/consume")
    return f"Cooked '{recipe_name}' — stock updated."


@mcp.tool
def add_missing_to_shopping_list(recipe_name: str) -> str:
    """Add all missing ingredients for a recipe to the shopping list.

    Args:
        recipe_name: Name of the recipe
    """
    recipe = _recipe_by_name(recipe_name)
    if not recipe:
        return f"Recipe '{recipe_name}' not found."
    api("post", f"/recipes/{recipe['id']}/add-not-fulfilled-products-to-shoppinglist")
    return f"Missing ingredients for '{recipe_name}' added to shopping list."


# ---------------------------------------------------------------------------
# Tools — Meal Plan
# ---------------------------------------------------------------------------

@mcp.tool
def get_meal_plan(days_ahead: int = 7) -> list[dict]:
    """Get the meal plan from today for the next N days.

    Args:
        days_ahead: How many days ahead to show (default 7)
    """
    entries = api("get", "/objects/meal_plan")
    recipes = {r["id"]: r["name"] for r in api("get", "/objects/recipes")}
    products = {p["id"]: p["name"] for p in api("get", "/objects/products")}

    today = datetime.now().date()
    cutoff = today + timedelta(days=days_ahead)

    out = []
    for e in entries:
        day_str = (e.get("day") or "")[:10]
        if not day_str:
            continue
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < today or day > cutoff:
            continue
        item: dict = {"date": day_str}
        if e.get("recipe_id"):
            item["recipe"] = recipes.get(e["recipe_id"], f"recipe_{e['recipe_id']}")
        elif e.get("product_id"):
            item["product"] = products.get(e["product_id"], f"product_{e['product_id']}")
        elif e.get("note"):
            item["note"] = e["note"]
        out.append(item)

    return sorted(out, key=lambda x: x["date"])


@mcp.tool
def add_to_meal_plan(recipe_name: str, date: str) -> str:
    """Schedule a recipe on a specific date.

    Args:
        recipe_name: Name of the recipe
        date: Date in YYYY-MM-DD format
    """
    recipe = _recipe_by_name(recipe_name)
    if not recipe:
        return f"Recipe '{recipe_name}' not found."
    api("post", "/objects/meal_plan", json={
        "recipe_id": recipe["id"],
        "day": date,
        "recipe_servings": recipe.get("desired_servings", 1),
        "type": "recipe",
    })
    return f"Scheduled '{recipe_name}' for {date}."


@mcp.tool
def delete_meal_plan_entry(date: str, recipe_name: str) -> str:
    """Remove a recipe from the meal plan on a given date.

    Args:
        date: Date in YYYY-MM-DD format
        recipe_name: Name of the recipe to remove
    """
    recipe = _recipe_by_name(recipe_name)
    if not recipe:
        return f"Recipe '{recipe_name}' not found."
    entries = api("get", "/objects/meal_plan")
    to_delete = [
        e for e in entries
        if (e.get("day") or "")[:10] == date and str(e.get("recipe_id")) == str(recipe["id"])
    ]
    if not to_delete:
        return f"No entry for '{recipe_name}' on {date}."
    for e in to_delete:
        api("delete", f"/objects/meal_plan/{e['id']}")
    return f"Removed '{recipe_name}' from {date}."


# ---------------------------------------------------------------------------
# Tools — Shopping List
# ---------------------------------------------------------------------------

@mcp.tool
def get_shopping_list() -> list[dict]:
    """Get the current shopping list."""
    items = api("get", "/objects/shopping_list")
    products = {p["id"]: p["name"] for p in api("get", "/objects/products")}
    units = {u["id"]: u["name"] for u in api("get", "/objects/quantity_units")}
    return [
        {
            "product": products.get(item["product_id"], f"product_{item['product_id']}"),
            "amount": float(item.get("amount", 1)),
            "unit": units.get(item.get("qu_id")),
            "note": item.get("note") or "",
        }
        for item in items
    ]


@mcp.tool
def add_to_shopping_list(product_name: str, amount: float = 1, note: str = "") -> str:
    """Add an item to the shopping list. Creates the product if it doesn't exist.

    Args:
        product_name: Name of the product
        amount: Quantity needed (default 1)
        note: Optional note (e.g. brand preference)
    """
    try:
        pid = _get_or_create_product(product_name)
    except ValueError as e:
        return str(e)
    api("post", "/objects/shopping_list", json={"product_id": pid, "amount": amount, "note": note})
    return f"Added {amount} × '{product_name}' to shopping list."


@mcp.tool
def remove_from_shopping_list(product_name: str) -> str:
    """Remove a product from the shopping list.

    Args:
        product_name: Name of the product to remove
    """
    items = api("get", "/objects/shopping_list")
    products = {p["id"]: p["name"] for p in api("get", "/objects/products")}
    to_delete = [
        i for i in items
        if products.get(i.get("product_id"), "").lower() == product_name.lower()
    ]
    if not to_delete:
        return f"'{product_name}' not found on shopping list."
    for item in to_delete:
        api("delete", f"/objects/shopping_list/{item['id']}")
    return f"Removed '{product_name}' from shopping list."


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8080)
