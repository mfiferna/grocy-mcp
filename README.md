# grocy-mcp

A minimal, AI-friendly MCP server for [Grocy](https://grocy.info/). Built with [FastMCP](https://github.com/prefecthq/fastmcp).

## Tools

| Tool | Description |
|---|---|
| `get_stock` | All inventory with amounts, units, best-before dates |
| `get_expiring_soon` | Items expiring within N days + already expired |
| `find_product` | Search products by name (for disambiguation) |
| `purchase` | Add stock, creates product if new |
| `consume` | Use up stock |
| `adjust_stock` | Set absolute quantity (correction) |
| `create_product` | Explicitly create a new product |
| `get_recipes` | All recipes + can-make-now status |
| `get_recipe` | Full recipe with ingredients |
| `create_recipe` | Create a recipe with ingredients |
| `update_recipe` | Update name/servings/ingredients/description |
| `cook_recipe` | Mark cooked — auto-consumes stock |
| `add_missing_to_shopping_list` | Add missing recipe ingredients to shopping list |
| `get_meal_plan` | Meal plan around today, with optional lookback plus per-entry/day energy values |
| `add_to_meal_plan` | Schedule a recipe |
| `add_product_to_meal_plan` | Schedule a standalone product such as a snack or drink |
| `delete_meal_plan_entry` | Remove a recipe or product from the meal plan |
| `get_shopping_list` | Current shopping list |
| `add_to_shopping_list` | Add item |
| `remove_from_shopping_list` | Remove item |

Product names are used throughout — IDs are handled internally.

## Running

```bash
docker run \
  -e GROCY_BASE_URL=https://your-grocy.example.com \
  -e GROCY_API_KEY=your_key \
  -p 8080:8080 \
  ghcr.io/mfiferna/grocy-mcp:latest
```

MCP endpoint: `http://localhost:8080/mcp`

## Config (env vars)

| Variable | Description |
|---|---|
| `GROCY_BASE_URL` | URL of your Grocy instance |
| `GROCY_API_KEY` | API key from Grocy → User Settings → API Keys |
