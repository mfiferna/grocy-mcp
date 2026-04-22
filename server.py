import difflib
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
        "Call get_system_info at the start of each session to learn available locations and quantity units — "
        "always use those exact names, never invent new ones unless explicitly creating them. "
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
        if resp.is_error:
            body = resp.text[:500] if resp.content else "(empty body)"
            raise httpx.HTTPStatusError(
                f"{resp.status_code} {resp.reason_phrase} for {path}: {body}",
                request=resp.request,
                response=resp,
            )
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


def _get_or_create_product(name: str, location: Optional[str] = None) -> int:
    pid, matches = _find_product(name)
    if pid:
        return pid
    if matches:
        raise ValueError(
            f"Ambiguous product '{name}'. Did you mean: {[m['name'] for m in matches[:5]]}?"
        )

    # Similarity guard — catch near-duplicates before creating
    all_products = api("get", "/objects/products") or []
    existing_names = [p["name"] for p in all_products]
    close = difflib.get_close_matches(name, existing_names, n=3, cutoff=0.75)
    if close:
        raise ValueError(
            f"Product '{name}' not found, but similar products exist: {close}. "
            "Use one of those names or confirm you want a new product by calling create_product explicitly."
        )

    units = api("get", "/objects/quantity_units") or []
    if not units:
        r = api("post", "/objects/quantity_units", json={"name": "piece", "name_plural": "pieces"})
        default_unit_id = r["created_object_id"]
    else:
        default_unit_id = units[0]["id"]

    locations = api("get", "/objects/locations") or []
    if not locations:
        r = api("post", "/objects/locations", json={"name": "Home"})
        default_location_id = r["created_object_id"]
    elif location:
        loc_id = _location_id(location)
        if loc_id is None:
            valid = [l["name"] for l in locations]
            raise ValueError(f"Location '{location}' not found. Valid locations: {valid}")
        default_location_id = loc_id
    else:
        default_location_id = locations[0]["id"]

    result = api("post", "/objects/products", json={
        "name": name,
        "qu_id_stock": default_unit_id,
        "qu_id_purchase": default_unit_id,
        "location_id": default_location_id,
    })
    return result["created_object_id"]


def _unit_id(name: str) -> Optional[int]:
    units = api("get", "/objects/quantity_units")
    name_lower = name.lower()
    for u in units:
        if u["name"].lower() == name_lower or (u.get("name_plural") or "").lower() == name_lower:
            return u["id"]
    return None


def _location_id(name: str) -> Optional[int]:
    locations = api("get", "/objects/locations") or []
    name_lower = name.lower()
    for loc in locations:
        if loc["name"].lower() == name_lower:
            return loc["id"]
    # fuzzy
    fuzzy = [loc for loc in locations if name_lower in loc["name"].lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]["id"]
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
# Tools — System Info
# ---------------------------------------------------------------------------

@mcp.tool
def get_system_info() -> dict:
    """Return available locations, quantity units, and stock summary.
    Call this at the start of a session before creating products or stock entries.
    """
    locations = api("get", "/objects/locations") or []
    units = api("get", "/objects/quantity_units") or []
    products = api("get", "/objects/products") or []
    stock = api("get", "/stock") or []
    return {
        "locations": [l["name"] for l in locations],
        "quantity_units": [u["name"] for u in units],
        "total_products": len(products),
        "products_in_stock": len(stock),
    }


@mcp.tool
def create_location(name: str) -> str:
    """Create a new storage location (e.g. 'Wine Cellar'). Only call if location doesn't already exist.

    Args:
        name: Location name
    """
    existing = api("get", "/objects/locations") or []
    if any(l["name"].lower() == name.lower() for l in existing):
        return f"Location '{name}' already exists."
    api("post", "/objects/locations", json={"name": name})
    return f"Created location '{name}'."


@mcp.tool
def create_quantity_unit(name: str, name_plural: Optional[str] = None) -> str:
    """Create a new quantity unit (e.g. 'kg', 'tbsp'). Only call if unit doesn't already exist.

    Args:
        name: Unit name singular (e.g. 'kilogram')
        name_plural: Unit name plural (e.g. 'kilograms'), defaults to name + 's'
    """
    existing = api("get", "/objects/quantity_units") or []
    if any(u["name"].lower() == name.lower() for u in existing):
        return f"Quantity unit '{name}' already exists."
    api("post", "/objects/quantity_units", json={
        "name": name,
        "name_plural": name_plural or f"{name}s",
    })
    return f"Created quantity unit '{name}'."


# ---------------------------------------------------------------------------
# Tools — Inventory
# ---------------------------------------------------------------------------

@mcp.tool
def get_stock(location: Optional[str] = None, search: Optional[str] = None, expiring_first: bool = False) -> list[dict]:
    """Return items currently in stock with product name, amount, unit and best-before date.

    Args:
        location: Filter by storage location name (e.g. 'Fridge', 'Freezer', 'Pantry')
        search: Filter by product name substring
        expiring_first: If true, sort by soonest best-before date first
    """
    items = api("get", "/stock")

    # Build location filter
    loc_id = None
    if location:
        loc_id = _location_id(location)

    out = []
    for item in items:
        product = item.get("product") or {}
        product_name = product.get("name") or f"product_{item['product_id']}"

        if search and search.lower() not in product_name.lower():
            continue
        if loc_id is not None and product.get("location_id") != loc_id:
            continue

        unit_name = (item.get("quantity_unit_stock") or {}).get("name", "")
        bbd = item.get("best_before_date") or ""
        out.append({
            "product": product_name,
            "amount": float(item.get("stock_amount") or item.get("amount", 0)),
            "unit": unit_name,
            "best_before": None if bbd == "2999-12-31" else bbd or None,
            "location": product.get("location_id"),
        })

    if expiring_first:
        out.sort(key=lambda x: x["best_before"] or "9999-99-99")
    else:
        out.sort(key=lambda x: x["product"])

    # Resolve location IDs to names in output
    if any(x["location"] for x in out):
        locations = {l["id"]: l["name"] for l in (api("get", "/objects/locations") or [])}
        for x in out:
            x["location"] = locations.get(x["location"])

    return out


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
def purchase(product_name: str, amount: float, use_by: Optional[str] = None, location: Optional[str] = None) -> str:
    """Add stock for a product. Creates the product automatically if it doesn't exist yet.

    Args:
        product_name: Name of the product
        amount: Quantity to add
        use_by: Best-before date in YYYY-MM-DD format (optional, agent should estimate if known)
        location: Storage location e.g. 'Fridge', 'Freezer', 'Pantry' (used when creating a new product)
    """
    try:
        pid = _get_or_create_product(product_name, location=location)
    except ValueError as e:
        return str(e)
    payload: dict = {"amount": amount, "transaction_type": "purchase"}
    if use_by:
        payload["best_before_date"] = use_by
    api("post", f"/stock/products/{pid}/add", json=payload)
    suffix = f" (use by {use_by})" if use_by else ""
    return f"Added {amount} × '{product_name}' to stock{suffix}."


@mcp.tool
def batch_purchase(items: list[dict]) -> str:
    """Add multiple products to stock in one call — ideal for logging a shopping trip.

    Args:
        items: List of {product_name: str, amount: float, use_by: str (optional YYYY-MM-DD), location: str (optional)}
    """
    results = []
    for item in items:
        name = item.get("product_name", "")
        amount = float(item.get("amount", 1))
        use_by = item.get("use_by")
        location = item.get("location")
        try:
            pid = _get_or_create_product(name, location=location)
            payload: dict = {"amount": amount, "transaction_type": "purchase"}
            if use_by:
                payload["best_before_date"] = use_by
            api("post", f"/stock/products/{pid}/add", json=payload)
            suffix = f" (use by {use_by})" if use_by else ""
            results.append(f"✓ {amount} × {name}{suffix}")
        except Exception as e:
            results.append(f"✗ {name}: {e}")
    return "\n".join(results)


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
def create_product(name: str, default_unit: str = "piece", location: Optional[str] = None) -> str:
    """Explicitly create a new product in the system.

    Args:
        name: Product name
        default_unit: Default quantity unit (e.g. 'gram', 'piece', 'liter', 'milliliter')
        location: Storage location e.g. 'Fridge', 'Freezer', 'Pantry'
    """
    uid = _unit_id(default_unit)
    if uid is None:
        units = api("get", "/objects/quantity_units") or []
        if not units:
            return f"No quantity units found. Call get_system_info to see available units."
        valid = [u["name"] for u in units]
        return f"Unit '{default_unit}' not found. Available units: {valid}"

    locations = api("get", "/objects/locations") or []
    if location:
        loc_id = _location_id(location)
        if loc_id is None:
            valid = [l["name"] for l in locations]
            return f"Location '{location}' not found. Available locations: {valid}"
    else:
        loc_id = locations[0]["id"] if locations else None

    payload: dict = {"name": name, "qu_id_stock": uid, "qu_id_purchase": uid}
    if loc_id:
        payload["location_id"] = loc_id
    api("post", "/objects/products", json=payload)
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
def suggest_recipes_from_expiring(days: int = 5) -> list[dict]:
    """Suggest recipes that use products expiring soon.

    Args:
        days: Look ahead this many days for expiring products (default 5)
    """
    volatile = api("get", "/stock/volatile", params={"due_soon_days": days})
    expiring = volatile.get("expiring_products", []) + volatile.get("expired_products", [])
    if not expiring:
        return []

    expiring_ids = {item["product_id"] for item in expiring}
    expiring_names = {
        item["product_id"]: (item.get("product") or {}).get("name", f"product_{item['product_id']}")
        for item in expiring
    }

    recipes = api("get", "/objects/recipes") or []
    suggestions = []
    for recipe in recipes:
        positions = api("get", "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe['id']}"}) or []
        uses_expiring = [expiring_names[p["product_id"]] for p in positions if p.get("product_id") in expiring_ids]
        if uses_expiring:
            try:
                f = api("get", f"/recipes/{recipe['id']}/fulfillment")
                can_make = f.get("need_fulfilled", False)
            except Exception:
                can_make = None
            suggestions.append({
                "recipe": recipe["name"],
                "uses_expiring": uses_expiring,
                "can_make_now": can_make,
            })

    return sorted(suggestions, key=lambda x: -len(x["uses_expiring"]))


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
def plan_week(entries: list[dict]) -> str:
    """Schedule multiple recipes for the week in one call.

    Args:
        entries: List of {recipe_name: str, date: str (YYYY-MM-DD)}
    """
    results = []
    missing_summary = []
    for entry in entries:
        recipe_name = entry.get("recipe_name", "")
        date = entry.get("date", "")
        recipe = _recipe_by_name(recipe_name)
        if not recipe:
            results.append(f"✗ {recipe_name}: recipe not found")
            continue
        api("post", "/objects/meal_plan", json={
            "recipe_id": recipe["id"],
            "day": date,
            "recipe_servings": recipe.get("desired_servings", 1),
            "type": "recipe",
        })
        line = f"✓ {date}: {recipe_name}"
        try:
            f = api("get", f"/recipes/{recipe['id']}/fulfillment")
            if not f.get("need_fulfilled"):
                missing = f.get("missing_products_count", "?")
                line += f" ⚠️ ({missing} missing)"
                missing_summary.append(recipe_name)
        except Exception:
            pass
        results.append(line)

    if missing_summary:
        results.append(f"\nMissing ingredients for: {', '.join(missing_summary)}. Consider calling add_missing_to_shopping_list for each.")
    return "\n".join(results)

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
    msg = f"Scheduled '{recipe_name}' for {date}."
    try:
        f = api("get", f"/recipes/{recipe['id']}/fulfillment")
        if not f.get("need_fulfilled"):
            missing = f.get("missing_products_count", "?")
            msg += f" ⚠️ Stock check: {missing} ingredient(s) missing — consider adding them to the shopping list."
    except Exception:
        pass
    return msg


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
