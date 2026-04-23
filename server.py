import difflib
import json
import os
import re
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
        "When a product name is ambiguous, call find_product to disambiguate before acting. "
        "When adding stock for packaged goods, amount must be in the product's stock unit: "
        "e.g. 2 × 150g packages → amount=300 (grams). "
        "If unsure about a product's unit, call get_product_details first. "
        "If unsure about use-by date, call suggest_use_by."
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


# ---------------------------------------------------------------------------
# Unit / location inference helpers
# ---------------------------------------------------------------------------

# Maps canonical unit name (as it appears in Grocy) to aliases we recognise
_UNIT_ALIASES: dict[str, list[str]] = {
    "gram":      ["g", "gr", "grams", "gram"],
    "kilogram":  ["kg", "kilogram", "kilograms"],
    "liter":     ["l", "liter", "litre", "liters", "litres"],
    "milliliter":["ml", "milliliter", "millilitre", "milliliters", "millilitres"],
    "piece":     ["piece", "pieces", "pcs", "ks"],
}

# Keyword → canonical unit name (checked against product name, lower-case)
_NAME_TO_UNIT: list[tuple[str, str]] = [
    # weight-sold loose produce — set unit=kilogram
    ("carrot", "kilogram"), ("potato", "kilogram"), ("onion", "kilogram"),
    ("apple", "kilogram"), ("banana", "kilogram"), ("tomato", "kilogram"),
    ("pepper", "kilogram"), ("lemon", "kilogram"), ("lime", "kilogram"),
    ("garlic", "kilogram"), ("ginger", "kilogram"), ("zucchini", "kilogram"),
    # liquid
    ("milk", "liter"), ("juice", "liter"), ("oil", "liter"), ("vinegar", "liter"),
    ("water", "liter"), ("broth", "liter"), ("stock", "liter"),
    # piece-sold
    ("egg", "piece"), ("lettuce", "piece"), ("cucumber", "piece"),
    ("avocado", "piece"), ("bread", "piece"),
]

# Keyword → canonical location name fragment (matched against real location names)
_NAME_TO_LOCATION: list[tuple[str, str]] = [
    # Fridge
    ("milk", "fridge"), ("cheese", "fridge"), ("yogurt", "fridge"),
    ("cottage", "fridge"), ("butter", "fridge"), ("cream", "fridge"),
    ("meat", "fridge"), ("chicken", "fridge"), ("beef", "fridge"),
    ("pork", "fridge"), ("fish", "fridge"), ("salmon", "fridge"),
    ("egg", "fridge"), ("salad", "fridge"), ("lettuce", "fridge"),
    ("cucumber", "fridge"), ("pepper", "fridge"), ("carrot", "fridge"),
    ("lemon", "fridge"), ("lime", "fridge"), ("tomato", "fridge"),
    ("vegetable", "fridge"), ("fruit", "fridge"), ("avocado", "fridge"),
    ("tofu", "fridge"), ("hummus", "fridge"),
    # Freezer
    ("frozen", "freezer"), ("ice cream", "freezer"), ("sorbet", "freezer"),
    # Pantry (checked last — broad defaults)
    ("pasta", "pantry"), ("rice", "pantry"), ("flour", "pantry"),
    ("sugar", "pantry"), ("honey", "pantry"), ("jam", "pantry"),
    ("sauce", "pantry"), ("canned", "pantry"), ("chickpea", "pantry"),
    ("bean", "pantry"), ("lentil", "pantry"), ("coffee", "pantry"),
    ("tea", "pantry"), ("soda", "pantry"), ("cereal", "pantry"),
    ("oat", "pantry"), ("cracker", "pantry"), ("chip", "pantry"),
    ("nut", "pantry"), ("chocolate", "pantry"), ("biscuit", "pantry"),
    ("cookie", "pantry"), ("oil", "pantry"), ("vinegar", "pantry"),
    ("stock", "pantry"), ("broth", "pantry"), ("water", "pantry"),
    ("juice", "pantry"), ("wine", "pantry"), ("beer", "pantry"),
]

# Category → default best-before offset in days
_USE_BY_DAYS: list[tuple[str, int]] = [
    ("frozen", 180),
    ("ice cream", 90),
    ("meat", 3), ("chicken", 3), ("fish", 3), ("salmon", 3), ("beef", 3), ("pork", 3),
    ("milk", 10), ("cream", 7), ("butter", 30),
    ("yogurt", 14), ("cheese", 21), ("cottage", 7),
    ("egg", 28),
    ("lettuce", 5), ("salad", 5), ("spinach", 5),
    ("herb", 7), ("avocado", 5),
    ("tomato", 7), ("cucumber", 7), ("pepper", 10),
    ("carrot", 21), ("onion", 30), ("potato", 30), ("garlic", 60),
    ("lemon", 21), ("lime", 21), ("banana", 5), ("apple", 30),
    ("bread", 5),
    ("pasta", 730), ("rice", 730), ("flour", 365), ("sugar", 730),
    ("oil", 365), ("vinegar", 730), ("honey", 730),
    ("canned", 730), ("chickpea", 730), ("bean", 730), ("lentil", 730),
    ("sauce", 730), ("jam", 365),
    ("coffee", 365), ("tea", 730),
    ("water", 730), ("juice", 365), ("soda", 365),
    ("wine", 730), ("beer", 180),
    ("chocolate", 365), ("biscuit", 180), ("cracker", 180), ("cereal", 365),
]


def _resolve_unit_id(canonical: str, units: list[dict]) -> Optional[int]:
    """Return unit id for a canonical name like 'gram', 'kilogram', 'liter', 'piece'."""
    canonical_lower = canonical.lower()
    aliases = _UNIT_ALIASES.get(canonical_lower, [canonical_lower])
    for u in units:
        u_name = u["name"].lower()
        u_plural = (u.get("name_plural") or "").lower()
        if u_name in aliases or u_plural in aliases:
            return u["id"]
    return None


def _infer_unit(product_name: str, units: list[dict]) -> tuple[Optional[int], Optional[str], Optional[float]]:
    """
    Infer (unit_id, unit_name, package_size_in_unit) from a product name.

    Examples:
      "Cottage Cheese Fit 150g"  → (gram_id, "gram",  150.0)
      "UHT Milk 1.5% 1L"        → (liter_id, "liter", 1.0)
      "Eggs"                     → (piece_id, "piece", None)
      "Carrots"                  → (kg_id,    "kilogram", None)
    """
    name_lower = product_name.lower()

    # Suffix patterns: e.g. "150g", "1.5L", "330ml", "1kg"
    suffix_patterns = [
        (r"(\d+(?:\.\d+)?)\s*kg\b",  "kilogram"),
        (r"(\d+(?:\.\d+)?)\s*g\b",   "gram"),
        (r"(\d+(?:\.\d+)?)\s*ml\b",  "milliliter"),
        (r"(\d+(?:\.\d+)?)\s*l\b",   "liter"),
    ]
    for pattern, canonical in suffix_patterns:
        m = re.search(pattern, name_lower)
        if m:
            uid = _resolve_unit_id(canonical, units)
            if uid:
                return uid, canonical, float(m.group(1))

    # Keyword fallback
    for keyword, canonical in _NAME_TO_UNIT:
        if keyword in name_lower:
            uid = _resolve_unit_id(canonical, units)
            if uid:
                return uid, canonical, None

    return None, None, None


def _infer_location_id(product_name: str, locations: list[dict]) -> Optional[int]:
    """Infer a location id from product name keywords."""
    name_lower = product_name.lower()
    for keyword, fragment in _NAME_TO_LOCATION:
        if keyword in name_lower:
            # match fragment against actual location names
            for loc in locations:
                if fragment in loc["name"].lower():
                    return loc["id"]
    return None


def _suggest_use_by(product_name: str) -> tuple[Optional[str], Optional[str]]:
    """Return (YYYY-MM-DD, reason) conservative use-by estimate. Does NOT persist."""
    name_lower = product_name.lower()
    for keyword, days in _USE_BY_DAYS:
        if keyword in name_lower:
            date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            return date, f"{keyword} → +{days}d"
    return None, None


def _get_or_create_product(name: str, location: Optional[str] = None) -> tuple[int, Optional[str]]:
    """Return (product_id, creation_note). creation_note is non-None when product was just created."""
    pid, matches = _find_product(name)
    if pid:
        return pid, None
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
    locations = api("get", "/objects/locations") or []

    # Resolve location
    if location:
        loc_id = _location_id(location)
        if loc_id is None:
            valid = [l["name"] for l in locations]
            raise ValueError(f"Location '{location}' not found. Valid locations: {valid}")
    else:
        loc_id = _infer_location_id(name, locations)

    if loc_id is None:
        loc_id = next((l["id"] for l in locations if "pantry" in l["name"].lower()), None)
    if loc_id is None and locations:
        loc_id = locations[0]["id"]
    if not loc_id:
        r = api("post", "/objects/locations", json={"name": "Home"})
        loc_id = r["created_object_id"]

    # Resolve unit
    inferred_unit_id, inferred_unit_name, package_size = _infer_unit(name, units)

    if inferred_unit_id:
        unit_id = inferred_unit_id
        unit_note = inferred_unit_name
    else:
        # Fall back to "piece" explicitly, not random first
        unit_id = _resolve_unit_id("piece", units)
        if unit_id is None and units:
            unit_id = units[0]["id"]
        if unit_id is None:
            r = api("post", "/objects/quantity_units", json={"name": "piece", "name_plural": "pieces"})
            unit_id = r["created_object_id"]
        unit_note = "piece (inferred)"

    result = api("post", "/objects/products", json={
        "name": name,
        "qu_id_stock": unit_id,
        "qu_id_purchase": unit_id,
        "qu_id_consume": unit_id,
        "qu_id_price": unit_id,
        "location_id": loc_id,
    })
    loc_name = next((l["name"] for l in locations if l["id"] == loc_id), str(loc_id))
    note = f"[new product: unit={unit_note}, location={loc_name}]"
    return result["created_object_id"], note


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
    Amount must be in the product's stock unit (e.g. grams for 'Cottage Cheese Fit 150g').
    For packaged goods: amount = package_size × package_count (e.g. 2 × 150g = 300).

    Args:
        product_name: Name of the product
        amount: Quantity to add, in the product's stock unit
        use_by: Best-before date in YYYY-MM-DD format (optional; call suggest_use_by if unsure)
        location: Storage location e.g. 'Fridge', 'Freezer', 'Pantry' (used when creating a new product)
    """
    try:
        pid, creation_note = _get_or_create_product(product_name, location=location)
    except ValueError as e:
        return str(e)

    # Look up unit name for confirmation
    products = api("get", "/objects/products") or []
    p = next((x for x in products if x["id"] == pid), {})
    units = {u["id"]: u["name"] for u in (api("get", "/objects/quantity_units") or [])}
    unit_name = units.get(p.get("qu_id_stock"), "")

    payload: dict = {"amount": amount, "transaction_type": "purchase"}
    payload["best_before_date"] = use_by if use_by else "2999-12-31"
    api("post", f"/stock/products/{pid}/add", json=payload)

    parts = [f"Added {amount} {unit_name} × '{product_name}'"]
    if use_by:
        parts.append(f"use by {use_by}")
    if creation_note:
        parts.append(creation_note)
    return " — ".join(parts) + "."


@mcp.tool
def batch_purchase(items: list[dict]) -> str:
    """Add multiple products to stock in one call — ideal for logging a shopping trip.
    For packaged goods specify amount as total quantity in stock units, not package count.
    E.g. 2 × 150g packages → amount=300 (grams), not amount=2.

    Args:
        items: List of {product_name: str, amount: float, use_by: str (optional YYYY-MM-DD), location: str (optional)}
    """
    results = []
    units_cache: dict = {}

    for item in items:
        name = item.get("product_name", "")
        amount = float(item.get("amount", 1))
        use_by = item.get("use_by")
        location = item.get("location")
        try:
            pid, creation_note = _get_or_create_product(name, location=location)

            if not units_cache:
                units_cache = {u["id"]: u["name"] for u in (api("get", "/objects/quantity_units") or [])}
            products = api("get", "/objects/products") or []
            p = next((x for x in products if x["id"] == pid), {})
            unit_name = units_cache.get(p.get("qu_id_stock"), "")

            payload: dict = {"amount": amount, "transaction_type": "purchase"}
            payload["best_before_date"] = use_by if use_by else "2999-12-31"
            api("post", f"/stock/products/{pid}/add", json=payload)

            suffix = f" (use by {use_by})" if use_by else ""
            note = f" {creation_note}" if creation_note else ""
            results.append(f"✓ {amount} {unit_name} × {name}{suffix}{note}")
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
def create_product(name: str, default_unit: Optional[str] = None, location: Optional[str] = None) -> str:
    """Explicitly create a new product in the system.
    Unit and location are inferred from the product name if not provided.

    Args:
        name: Product name
        default_unit: Default quantity unit (e.g. 'gram', 'piece', 'liter'). Auto-inferred from name if omitted.
        location: Storage location e.g. 'Fridge', 'Freezer', 'Pantry'. Auto-inferred from name if omitted.
    """
    units = api("get", "/objects/quantity_units") or []
    locations = api("get", "/objects/locations") or []

    # Resolve unit
    if default_unit:
        uid = _unit_id(default_unit)
        if uid is None:
            valid = [u["name"] for u in units]
            return f"Unit '{default_unit}' not found. Available units: {valid}"
        unit_name = default_unit
    else:
        uid, unit_name, _ = _infer_unit(name, units)
        if uid is None:
            uid = _resolve_unit_id("piece", units) or (units[0]["id"] if units else None)
            unit_name = "piece (inferred)"

    # Resolve location
    if location:
        loc_id = _location_id(location)
        if loc_id is None:
            valid = [l["name"] for l in locations]
            return f"Location '{location}' not found. Available locations: {valid}"
        loc_name = location
    else:
        loc_id = _infer_location_id(name, locations)
        if loc_id is None:
            loc_id = next((l["id"] for l in locations if "pantry" in l["name"].lower()), None)
        if loc_id is None and locations:
            loc_id = locations[0]["id"]
        loc_name = next((l["name"] for l in locations if l["id"] == loc_id), "unknown") if loc_id else "none"

    payload: dict = {
        "name": name,
        "qu_id_stock": uid,
        "qu_id_purchase": uid,
        "qu_id_consume": uid,
        "qu_id_price": uid,
    }
    if loc_id:
        payload["location_id"] = loc_id
    api("post", "/objects/products", json=payload)
    return f"Created product '{name}' — unit: {unit_name}, location: {loc_name}."


@mcp.tool
def get_product_details(product_name: str) -> dict:
    """Get full details of a product: unit, location, current stock, open amount, min stock.

    Args:
        product_name: Name of the product
    """
    pid, matches = _find_product(product_name)
    if not pid:
        if matches:
            return {"error": f"Ambiguous: {[m['name'] for m in matches[:5]]}"}
        return {"error": f"Product '{product_name}' not found."}

    products = api("get", "/objects/products") or []
    p = next((x for x in products if x["id"] == pid), {})

    units = {u["id"]: u["name"] for u in (api("get", "/objects/quantity_units") or [])}
    locations = {l["id"]: l["name"] for l in (api("get", "/objects/locations") or [])}

    stock_entries = api("get", "/stock") or []
    entry = next((s for s in stock_entries if s["product_id"] == pid), None)

    return {
        "name": p.get("name"),
        "unit": units.get(p.get("qu_id_stock")),
        "location": locations.get(p.get("location_id")),
        "in_stock": float(entry["amount"]) if entry else 0.0,
        "open_amount": float(entry.get("open_amount", 0)) if entry else 0.0,
        "min_stock": float(p.get("min_stock_amount") or 0),
        "best_before": (entry or {}).get("best_before_date"),
    }


@mcp.tool
def suggest_use_by(product_name: str) -> dict:
    """Suggest a conservative best-before date for a product based on its name.
    Returns a suggestion only — does not update anything. Pass the date to purchase/set_nutrition as needed.

    Args:
        product_name: Name of the product
    """
    date, reason = _suggest_use_by(product_name)
    if date:
        return {"suggested_use_by": date, "reason": reason}
    return {"suggested_use_by": None, "reason": "No matching category — provide use_by explicitly."}


@mcp.tool
def update_product_unit(product_name: str, new_unit: str, new_total_amount: float) -> str:
    """Change the stock unit of an existing product.
    You must specify the correct total amount in the new unit (e.g. if changing from 'piece' to 'gram'
    and you have 2 × 150g packages, set new_total_amount=300).

    Args:
        product_name: Name of the product
        new_unit: New unit name (e.g. 'gram', 'kilogram', 'liter')
        new_total_amount: Correct total stock amount expressed in the new unit
    """
    pid, matches = _find_product(product_name)
    if not pid:
        if matches:
            return f"Ambiguous: {[m['name'] for m in matches[:5]]}"
        return f"Product '{product_name}' not found."

    uid = _unit_id(new_unit)
    if uid is None:
        units = api("get", "/objects/quantity_units") or []
        return f"Unit '{new_unit}' not found. Available: {[u['name'] for u in units]}"

    products = api("get", "/objects/products") or []
    product = next((x for x in products if x["id"] == pid), {})
    old_uid = product.get("qu_id_stock")

    if old_uid == uid:
        return f"'{product_name}' already uses unit '{new_unit}'."

    # Create temporary 1:1 conversion to satisfy Grocy's constraint
    conv = api("post", "/objects/quantity_unit_conversions", json={
        "from_qu_id": old_uid, "to_qu_id": uid, "factor": 1, "product_id": pid,
    })
    conv_id = conv["created_object_id"]

    try:
        # Capture existing stock entries (with best_before dates) before wiping
        existing_entries = api("get", f"/stock/products/{pid}/entries") or []

        # Zero out stock
        api("post", f"/stock/products/{pid}/inventory", json={"new_amount": 0, "best_before_date": "2999-12-31"})

        # Update unit on product
        patch = {**product, "qu_id_stock": uid, "qu_id_purchase": uid, "qu_id_consume": uid, "qu_id_price": uid}
        patch.pop("userfields", None)
        api("put", f"/objects/products/{pid}", json=patch)

        # Re-add stock, preserving best_before dates where possible.
        # If existing entries exist, re-add each with its original date scaled to new_total_amount.
        # Otherwise fall back to a single entry with no date.
        if existing_entries:
            original_total = sum(float(e.get("amount", 0)) for e in existing_entries)
            scale = new_total_amount / original_total if original_total else 1.0
            for entry in existing_entries:
                entry_amount = float(entry.get("amount", 0)) * scale
                if entry_amount <= 0:
                    continue
                bbd = entry.get("best_before_date") or "2999-12-31"
                api("post", f"/stock/products/{pid}/add", json={
                    "amount": entry_amount,
                    "best_before_date": bbd,
                })
        else:
            api("post", f"/stock/products/{pid}/add", json={"amount": new_total_amount})
    finally:
        api("delete", f"/objects/quantity_unit_conversions/{conv_id}")

    return f"'{product_name}' unit changed to '{new_unit}', stock set to {new_total_amount}."


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
# Tools — Nutrition
# ---------------------------------------------------------------------------

@mcp.tool
def get_nutrition(product_name: str) -> dict:
    """Get nutrition info (calories, protein, carbs, fat per 100g) for a product.

    Args:
        product_name: Name of the product
    """
    pid, matches = _find_product(product_name)
    if not pid:
        if matches:
            return {"error": f"Ambiguous: {[m['name'] for m in matches[:5]]}"}
        return {"error": f"Product '{product_name}' not found."}

    products = api("get", "/objects/products") or []
    product = next((p for p in products if p["id"] == pid), {})

    userfields = api("get", f"/userfields/products/{pid}") or {}

    return {
        "product": product.get("name", product_name),
        "calories_kcal": product.get("calories"),
        "protein_g": userfields.get("protein_g"),
        "carbs_g": userfields.get("carbs_g"),
        "fat_g": userfields.get("fat_g"),
        "per": "100g",
    }


@mcp.tool
def set_nutrition(
    product_name: str,
    calories_kcal: Optional[float] = None,
    protein_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    fat_g: Optional[float] = None,
) -> str:
    """Set nutrition values (per 100g) for a product.

    Args:
        product_name: Name of the product
        calories_kcal: Calories in kcal per 100g
        protein_g: Protein in grams per 100g
        carbs_g: Carbohydrates in grams per 100g
        fat_g: Fat in grams per 100g
    """
    pid, matches = _find_product(product_name)
    if not pid:
        if matches:
            return f"Ambiguous: {[m['name'] for m in matches[:5]]}"
        return f"Product '{product_name}' not found."

    products = api("get", "/objects/products") or []
    product = next((p for p in products if p["id"] == pid), {})

    if calories_kcal is not None:
        patch = {**product, "calories": calories_kcal}
        api("put", f"/objects/products/{pid}", json=patch)

    userfields: dict = {}
    if protein_g is not None:
        userfields["protein_g"] = protein_g
    if carbs_g is not None:
        userfields["carbs_g"] = carbs_g
    if fat_g is not None:
        userfields["fat_g"] = fat_g
    if userfields:
        api("put", f"/userfields/products/{pid}", json=userfields)

    parts = []
    if calories_kcal is not None:
        parts.append(f"{calories_kcal} kcal")
    if protein_g is not None:
        parts.append(f"{protein_g}g protein")
    if carbs_g is not None:
        parts.append(f"{carbs_g}g carbs")
    if fat_g is not None:
        parts.append(f"{fat_g}g fat")
    return f"Set nutrition for '{product_name}': {', '.join(parts)} per 100g."


@mcp.tool
def list_products_without_nutrition() -> list[str]:
    """Return product names that have no nutrition data set yet."""
    products = api("get", "/objects/products") or []
    missing = []
    for p in products:
        has_calories = p.get("calories") not in (None, 0, "")
        userfields = api("get", f"/userfields/products/{p['id']}") or {}
        has_macros = any(userfields.get(f) not in (None, "") for f in ("protein_g", "carbs_g", "fat_g"))
        if not has_calories and not has_macros:
            missing.append(p["name"])
    return sorted(missing)


@mcp.tool
def lookup_nutrition(product_name: str) -> dict:
    """Look up nutrition data from OpenFoodFacts by product name.
    On exact match, returns values ready to pass to set_nutrition.
    On no exact match, returns the closest result for confirmation — retry with a more specific name
    or call set_nutrition directly with the returned values.

    Args:
        product_name: Product name to search for
    """
    import urllib.request
    import urllib.parse

    query = urllib.parse.quote(product_name)
    url = (
        f"https://world.openfoodfacts.org/cgi/search.pl"
        f"?search_terms={query}&search_simple=1&action=process&json=1&page_size=5"
        f"&fields=product_name,nutriments,brands"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "grocy-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    products = data.get("products", [])
    if not products:
        return {"error": f"No results found for '{product_name}' on OpenFoodFacts."}

    def _extract(p: dict) -> dict:
        n = p.get("nutriments", {})
        return {
            "name": p.get("product_name", ""),
            "brand": p.get("brands", ""),
            "calories_kcal": n.get("energy-kcal_100g"),
            "protein_g": n.get("proteins_100g"),
            "carbs_g": n.get("carbohydrates_100g"),
            "fat_g": n.get("fat_100g"),
            "per": "100g",
        }

    # Exact match check
    name_lower = product_name.lower()
    for p in products:
        if p.get("product_name", "").lower() == name_lower:
            result = _extract(p)
            result["match"] = "exact"
            return result

    # Return best (first) result for confirmation
    result = _extract(products[0])
    result["match"] = "closest"
    result["message"] = (
        f"No exact match for '{product_name}'. "
        f"Closest: '{result['name']}' ({result['brand']}). "
        "If correct, call set_nutrition with these values. Otherwise retry with a more specific name."
    )
    return result


@mcp.tool
def get_day_nutrition(date: str) -> dict:
    """Calculate total nutrition for a given day based on the meal plan.

    Args:
        date: Date in YYYY-MM-DD format
    """
    entries = api("get", "/objects/meal_plan") or []
    recipes_map = {r["id"]: r for r in (api("get", "/objects/recipes") or [])}
    products_map = {p["id"]: p for p in (api("get", "/objects/products") or [])}

    totals = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    breakdown = []
    no_data = []

    day_entries = [e for e in entries if (e.get("day") or "")[:10] == date]
    if not day_entries:
        return {"date": date, "error": "No meal plan entries for this date."}

    for entry in day_entries:
        if not entry.get("recipe_id"):
            continue
        recipe = recipes_map.get(entry["recipe_id"])
        if not recipe:
            continue
        servings = float(entry.get("recipe_servings") or recipe.get("desired_servings") or 1)
        base_servings = float(recipe.get("base_servings") or recipe.get("desired_servings") or 1)
        scale = servings / base_servings if base_servings else 1.0

        positions = api("get", "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe['id']}"}) or []
        recipe_totals = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}

        for pos in positions:
            pid = pos.get("product_id")
            amount_g = float(pos.get("amount", 0)) * scale
            product = products_map.get(pid, {})
            userfields = api("get", f"/userfields/products/{pid}") or {}

            cal = product.get("calories")
            prot = userfields.get("protein_g")
            carb = userfields.get("carbs_g")
            fat = userfields.get("fat_g")

            if cal is None and prot is None:
                no_data.append(product.get("name", f"product_{pid}"))
                continue

            factor = amount_g / 100.0
            recipe_totals["calories_kcal"] += (float(cal or 0)) * factor
            recipe_totals["protein_g"] += (float(prot or 0)) * factor
            recipe_totals["carbs_g"] += (float(carb or 0)) * factor
            recipe_totals["fat_g"] += (float(fat or 0)) * factor

        for k in totals:
            totals[k] += recipe_totals[k]
        breakdown.append({"recipe": recipe["name"], **{k: round(v, 1) for k, v in recipe_totals.items()}})

    result: dict = {
        "date": date,
        "total": {k: round(v, 1) for k, v in totals.items()},
        "breakdown": breakdown,
    }
    if no_data:
        result["missing_nutrition_data"] = list(set(no_data))
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8080)
