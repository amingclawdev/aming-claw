# L4 Smoke Fixture Artifact

This artifact is the source of truth for the external-governance-demo smoke project.
Add future dashboard E2E coverage by extending the governance hint and file blocks, then run `node scripts/materialize-fixture.mjs`.

<!-- governance-hint
{
  "schema_version": 1,
  "artifact_id": "external-governance-demo.l4-smoke-fixture",
  "layer": "L4",
  "kind": "fixture_artifact",
  "purpose": "Materialize a mixed Python/TypeScript/JavaScript project for dashboard graph/bootstrap/scope-reconcile smoke tests.",
  "generation": {
    "command": "node scripts/materialize-fixture.mjs --root examples/external-governance-demo --artifact docs/fixtures/external-governance-demo/l4-smoke-fixture.md",
    "script": "scripts/materialize-fixture.mjs",
    "target_root": "examples/external-governance-demo",
    "deterministic": true,
    "uses_ai": false,
    "mutates_governance_db": false
  },
  "scenarios": [
    {
      "id": "quote.v1",
      "layer": "L4",
      "kind": "domain_contract",
      "primary_files": [
        "src/demo_app/service.py",
        "web/widget.ts",
        "web/widget.js",
        "web/checkout.ts"
      ],
      "docs": [
        "docs/l4/quote-contract.md",
        "docs/l4/pricing-state.md",
        "docs/l4/test-coverage.md"
      ],
      "tests": [
        "tests/test_service.py",
        "tests/test_routes.py",
        "tests/smoke.test.mjs",
        "tests/widget.test.mjs"
      ],
      "artifacts": [
        "contracts/quote.schema.json",
        "state/pricing-rules.json"
      ],
      "relations": [
        {
          "type": "writes_artifact",
          "source": "src/demo_app/service.py",
          "target": "contracts/quote.schema.json"
        },
        {
          "type": "reads_state",
          "source": "src/demo_app/service.py",
          "target": "state/pricing-rules.json"
        },
        {
          "type": "depends_on",
          "source": "web/checkout.ts",
          "target": "web/widget.ts"
        }
      ],
      "expected_graph_shape": {
        "python_fan_out": [
          "quote_breakdown",
          "discount_for",
          "tax_for",
          "shipping_for",
          "compliance_flags"
        ],
        "python_fan_in": [
          "quote_order",
          "quote_breakdown_route",
          "quote_summary_route",
          "quote_export_route"
        ],
        "javascript_functions": [
          "renderQuote"
        ],
        "typescript_fan_out": [
          "buildQuoteView",
          "quoteSubtotal",
          "quoteDiscount",
          "quoteTax",
          "quoteBadges"
        ],
        "typescript_fan_in": [
          "createCheckoutPayload",
          "createCheckoutSummary",
          "renderCheckout"
        ]
      }
    }
  ],
  "materializes": [
    ".aming-claw.yaml",
    ".gitignore",
    "package.json",
    "pyproject.toml",
    "tsconfig.json",
    "README.md",
    "docs/usage.md",
    "docs/l4/quote-contract.md",
    "docs/l4/pricing-state.md",
    "docs/l4/test-coverage.md",
    "contracts/quote.schema.json",
    "state/pricing-rules.json",
    "src/demo_app/__init__.py",
    "src/demo_app/service.py",
    "src/demo_app/routes.py",
    "tests/test_service.py",
    "tests/test_routes.py",
    "tests/smoke.test.mjs",
    "tests/widget.test.mjs",
    "web/widget.js",
    "web/widget.ts",
    "web/checkout.ts"
  ]
}
-->

## Materialized Files

#### .aming-claw.yaml

````file path=".aming-claw.yaml"
version: 2
project_id: external-governance-demo
name: "External Governance Demo"
language: mixed

testing:
  unit_command: "python -m pytest tests -q"
  e2e_command: "npm test"
  allowed_commands:
    - executable: "python"
      args_prefixes: ["-m pytest"]
    - executable: "node"
      args_prefixes: ["scripts/materialize-fixture.mjs", "tests/smoke.test.mjs", "tests/widget.test.mjs"]
    - executable: "npm"
      args_prefixes: ["test", "run smoke", "run generate"]

governance:
  enabled: true
  test_tool_label: "pytest+node"

graph:
  exclude_paths:
    - "node_modules"
    - "dist"
    - "coverage"
    - ".pytest_cache"
    - "__pycache__"
    - ".aming-claw/e2e-artifacts"
  ignore_globs:
    - "**/node_modules/**"
    - "**/dist/**"
    - "**/coverage/**"
    - "**/.pytest_cache/**"
    - "**/__pycache__/**"
    - "**/.aming-claw/e2e-artifacts/**"
  nested_projects:
    mode: "exclude"
    roots: []

# Intentionally no ai.routing block: smoke fixtures should not silently enable
# live model calls. Configure AI from the dashboard when a live semantic smoke
# pass is desired.
````

#### .gitignore

````file path=".gitignore"
.pytest_cache/
__pycache__/
*.py[cod]
node_modules/
dist/
coverage/
.aming-claw/e2e-artifacts/
````

#### package.json

````file path="package.json"
{
  "name": "external-governance-demo-web",
  "private": true,
  "type": "module",
  "scripts": {
    "generate": "node scripts/materialize-fixture.mjs",
    "smoke": "npm run generate && node tests/smoke.test.mjs && node tests/widget.test.mjs",
    "test": "npm run smoke"
  }
}
````

#### pyproject.toml

````file path="pyproject.toml"
[project]
name = "external-governance-demo"
version = "0.1.0"
requires-python = ">=3.11"

````

#### tsconfig.json

````file path="tsconfig.json"
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ES2022",
    "moduleResolution": "Bundler",
    "strict": true,
    "noEmit": true
  },
  "include": ["web/**/*.ts"]
}
````

#### README.md

````file path="README.md"
# External Governance Demo

Small mixed-language fixture project used to verify that aming-claw can scan,
index, bootstrap, and inspect an external repository without relying on its own
governance files.

It intentionally includes:

- Python package code under `src/demo_app/`
- Python tests under `tests/`
- TypeScript dashboard/client code under `web/`
- JavaScript compatibility shim under `web/widget.js`
- Contract and state assets under `contracts/` and `state/`
- L4-style docs under `docs/l4/`
- A local `.aming-claw.yaml` project config

Smoke commands:

```bash
npm run generate
python -m pytest tests -q
npm test
```

The fixture is materialized from the aming-claw repo-level artifact
`docs/fixtures/external-governance-demo/l4-smoke-fixture.md`. Add future E2E
scenarios by extending the `governance-hint` block and the fenced file blocks in
that L4 artifact, then run `npm run generate`.

Graph smoke coverage:

- Python fan-out: `quote_breakdown` calls pricing, shipping, tax, and compliance
  helpers.
- Python fan-in: routes and UI summary helpers all converge on the same quote
  breakdown contract.
- TypeScript fan-out: `buildQuoteView` fans out to subtotal, discount, tax, and
  badge helpers.
- TypeScript fan-in: checkout helpers and renderers reuse the quote view.
- JavaScript support: `web/widget.js` gives the graph builder a plain JS file
  beside the TypeScript sources.
- L4 assets: quote schema, pricing state, and coverage docs are present so the
  dashboard can display code, docs, tests, config, and contract files together.
````

#### docs/usage.md

````file path="docs/usage.md"
# Usage

The quote route accepts a JSON payload with an `items` list and returns the
calculated total.

```json
{
  "customer_tier": "member",
  "region": "CA-ON",
  "items": [
    { "sku": "book", "price": 10, "quantity": 3 },
    { "sku": "battery", "price": 5, "quantity": 2, "hazmat": true }
  ]
}
```

Available routes:

- `quote_order(payload)` returns the total only.
- `quote_breakdown_route(payload)` returns subtotal, discount, tax, shipping,
  total, and compliance flags.
- `quote_summary_route(payload)` returns a compact string for UI cards.

Related L4 assets:

- `docs/fixtures/external-governance-demo/l4-smoke-fixture.md` is the
  deployable fixture artifact with governance hints and file blocks.
- `docs/l4/quote-contract.md` explains the request and response contract.
- `docs/l4/pricing-state.md` describes pricing state and compliance flags.
- `docs/l4/test-coverage.md` maps smoke tests to the code paths.
- `contracts/quote.schema.json` is the file-backed schema used by dashboard
  smoke checks.
````

#### docs/l4/quote-contract.md

````file path="docs/l4/quote-contract.md"
# Quote Contract

Layer: L4 domain contract

Contract id: `quote.v1`

Primary code:

- `src/demo_app/service.py::quote_breakdown`
- `src/demo_app/service.py::export_quote_payload`
- `web/checkout.ts::createCheckoutPayload`

Tests:

- `tests/test_service.py`
- `tests/test_routes.py`
- `tests/smoke.test.mjs`
- `tests/widget.test.mjs`

The quote contract accepts a list of item lines and returns subtotal, discount,
tax, shipping, total, and compliance flags. Python routes and TypeScript
checkout helpers both fan in to this same contract shape so the dashboard can
verify that docs, code, tests, and schema assets are attached to the same
feature area.

Schema asset: `contracts/quote.schema.json`
````

#### docs/l4/pricing-state.md

````file path="docs/l4/pricing-state.md"
# Pricing State

Layer: L4 state asset

State file: `state/pricing-rules.json`

Primary code:

- `src/demo_app/service.py::discount_for`
- `src/demo_app/service.py::tax_for`
- `src/demo_app/service.py::shipping_for`
- `src/demo_app/service.py::compliance_flags`

The smoke fixture keeps pricing rules in a JSON state asset and mirrors those
values in tiny pure functions. This gives scope-reconcile a useful mix of code,
state, docs, and tests without requiring an external service or database.

The fan-out path starts at `quote_breakdown` and reaches discount, tax,
shipping, and compliance helpers. The fan-in path starts at routes, summaries,
exports, and checkout helpers, then converges on the same quote breakdown.
````

#### docs/l4/test-coverage.md

````file path="docs/l4/test-coverage.md"
# Test Coverage

Layer: L4 test coverage contract

Coverage map:

- `tests/test_service.py` covers pricing helpers, quote breakdown, and export
  payload fan-in.
- `tests/test_routes.py` covers route functions that share the quote breakdown.
- `tests/smoke.test.mjs` checks that Python, TypeScript, docs, schema, and state
  assets are all present for graph indexing.
- `tests/widget.test.mjs` checks that TypeScript fan-in and fan-out functions
  remain visible to the graph adapter.

Expected smoke commands:

```bash
python -m pytest tests -q
npm test
```
````

#### contracts/quote.schema.json

````file path="contracts/quote.schema.json"
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "quote.v1",
  "title": "Quote Request",
  "type": "object",
  "required": ["items"],
  "properties": {
    "customer_tier": {
      "type": "string",
      "enum": ["standard", "member", "enterprise"],
      "default": "standard"
    },
    "region": {
      "type": "string",
      "default": "CA-ON"
    },
    "items": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["sku", "price"],
        "properties": {
          "sku": { "type": "string" },
          "price": { "type": "number", "minimum": 0 },
          "quantity": { "type": "integer", "minimum": 1, "default": 1 },
          "hazmat": { "type": "boolean", "default": false }
        }
      }
    }
  }
}
````

#### state/pricing-rules.json

````file path="state/pricing-rules.json"
{
  "contract": "quote.v1",
  "tax_rates": {
    "CA-ON": 0.13,
    "US-NY": 0.08875,
    "DEFAULT": 0.1
  },
  "discount_rates": {
    "standard": 0,
    "member": 0.05,
    "enterprise": 0.15
  },
  "shipping": {
    "free_quantity_threshold": 5,
    "base_fee": 7.5
  },
  "review_flags": ["requires_hazmat_review", "invalid_price"]
}
````

#### src/demo_app/__init__.py

````file path="src/demo_app/__init__.py"
"""Demo package for external governance scans."""

````

#### src/demo_app/service.py

````file path="src/demo_app/service.py"
"""Order pricing service with deliberate fan-in and fan-out shapes."""

TAX_RATES = {
    "CA-ON": 0.13,
    "US-NY": 0.08875,
    "DEFAULT": 0.10,
}


def normalize_item(item):
    return {
        "sku": str(item.get("sku") or "unknown"),
        "price": float(item.get("price", 0)),
        "quantity": int(item.get("quantity", 1)),
        "hazmat": bool(item.get("hazmat", False)),
    }


def normalize_items(items):
    return [normalize_item(item) for item in items]


def line_total(item):
    normalized = normalize_item(item)
    return normalized["price"] * normalized["quantity"]


def subtotal_for(items):
    return sum(line_total(item) for item in items)


def discount_for(subtotal, customer_tier="standard"):
    if customer_tier == "enterprise":
        return subtotal * 0.15
    if customer_tier == "member":
        return subtotal * 0.05
    return 0.0


def tax_for(amount, region="CA-ON"):
    rate = TAX_RATES.get(region, TAX_RATES["DEFAULT"])
    return amount * rate


def shipping_for(items):
    quantity = sum(item["quantity"] for item in normalize_items(items))
    return 0.0 if quantity >= 5 else 7.5


def compliance_flags(items):
    normalized = normalize_items(items)
    flags = []
    if any(item["hazmat"] for item in normalized):
        flags.append("requires_hazmat_review")
    if any(item["price"] <= 0 for item in normalized):
        flags.append("invalid_price")
    return flags


def quote_breakdown(payload):
    items = payload.get("items", [])
    subtotal = subtotal_for(items)
    discount = discount_for(subtotal, payload.get("customer_tier", "standard"))
    taxable = subtotal - discount
    tax = tax_for(taxable, payload.get("region", "CA-ON"))
    shipping = shipping_for(items)
    total = round(taxable + tax + shipping, 2)
    return {
        "subtotal": round(subtotal, 2),
        "discount": round(discount, 2),
        "tax": round(tax, 2),
        "shipping": round(shipping, 2),
        "total": total,
        "flags": compliance_flags(items),
    }


def quote_contract_summary(payload):
    quote = quote_breakdown(payload)
    return {
        "line_count": len(payload.get("items", [])),
        "total": quote["total"],
        "requires_review": bool(quote["flags"]),
    }


def calculate_total(items):
    return quote_breakdown({"items": items})["total"]


def summarize_quote(payload):
    quote = quote_breakdown(payload)
    return f"{quote['total']:.2f} total / {len(quote['flags'])} flags"


def export_quote_payload(payload):
    quote = quote_breakdown(payload)
    return {
        "contract": "quote.v1",
        "summary": quote_contract_summary(payload),
        "quote": quote,
    }
````

#### src/demo_app/routes.py

````file path="src/demo_app/routes.py"
"""Thin route handlers for the demo service."""

from demo_app.service import (
    calculate_total,
    export_quote_payload,
    quote_breakdown,
    summarize_quote,
)


def quote_order(payload):
    return {"total": calculate_total(payload.get("items", []))}


def quote_breakdown_route(payload):
    return quote_breakdown(payload)


def quote_summary_route(payload):
    return {"summary": summarize_quote(payload)}


def quote_export_route(payload):
    return export_quote_payload(payload)
````

#### tests/test_service.py

````file path="tests/test_service.py"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_app.service import (
    calculate_total,
    compliance_flags,
    export_quote_payload,
    quote_breakdown,
    quote_contract_summary,
    shipping_for,
)


def test_calculate_total_adds_tax():
    total = calculate_total([{"price": 10, "quantity": 2}])
    assert total == 30.1


def test_quote_breakdown_fans_out_pricing_steps():
    quote = quote_breakdown({
        "customer_tier": "member",
        "region": "CA-ON",
        "items": [
            {"sku": "book", "price": 10, "quantity": 3},
            {"sku": "battery", "price": 5, "quantity": 2, "hazmat": True},
        ],
    })
    assert quote["subtotal"] == 40
    assert quote["discount"] == 2
    assert quote["shipping"] == 0
    assert quote["total"] == 42.94
    assert quote["flags"] == ["requires_hazmat_review"]


def test_l4_state_helpers_are_independently_addressable():
    assert shipping_for([{"price": 1, "quantity": 6}]) == 0
    assert compliance_flags([{"price": 0, "quantity": 1}]) == ["invalid_price"]


def test_contract_summary_and_export_share_breakdown():
    payload = {"items": [{"sku": "fixture", "price": 20, "quantity": 1}]}
    assert quote_contract_summary(payload) == {
        "line_count": 1,
        "total": 30.1,
        "requires_review": False,
    }
    exported = export_quote_payload(payload)
    assert exported["contract"] == "quote.v1"
    assert exported["quote"]["total"] == exported["summary"]["total"]
````

#### tests/test_routes.py

````file path="tests/test_routes.py"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_app.routes import quote_breakdown_route, quote_export_route, quote_summary_route


def test_routes_fan_in_to_shared_quote_breakdown():
    payload = {"items": [{"sku": "fixture", "price": 20, "quantity": 1}]}
    assert quote_breakdown_route(payload)["total"] == 30.1
    assert quote_summary_route(payload)["summary"] == "30.10 total / 0 flags"
    assert quote_export_route(payload)["summary"]["total"] == 30.1


def test_route_flags_surface_review_state():
    payload = {"items": [{"sku": "acid", "price": 12, "quantity": 1, "hazmat": True}]}
    exported = quote_export_route(payload)
    assert exported["summary"]["requires_review"] is True
    assert exported["quote"]["flags"] == ["requires_hazmat_review"]
````

#### tests/smoke.test.mjs

````file path="tests/smoke.test.mjs"
import { readFileSync } from "node:fs";
import { join } from "node:path";

const service = readFileSync(join(process.cwd(), "src", "demo_app", "service.py"), "utf8");
const widget = readFileSync(join(process.cwd(), "web", "widget.ts"), "utf8");
const legacyWidget = readFileSync(join(process.cwd(), "web", "widget.js"), "utf8");
const checkout = readFileSync(join(process.cwd(), "web", "checkout.ts"), "utf8");
const contract = readFileSync(join(process.cwd(), "contracts", "quote.schema.json"), "utf8");
const pricing = readFileSync(join(process.cwd(), "state", "pricing-rules.json"), "utf8");
const l4Doc = readFileSync(join(process.cwd(), "docs", "l4", "quote-contract.md"), "utf8");
const manifest = JSON.parse(
  readFileSync(join(process.cwd(), ".aming-claw", "e2e-artifacts", "materialize-manifest.json"), "utf8"),
);

if (!service.includes("calculate_total")) {
  throw new Error("Python service fixture is missing calculate_total");
}

if (!service.includes("quote_breakdown") || !service.includes("compliance_flags")) {
  throw new Error("Python service fixture is missing fan-out pricing helpers");
}

if (!widget.includes("renderQuoteSummary")) {
  throw new Error("TypeScript widget fixture is missing renderQuoteSummary");
}

if (!widget.includes("buildQuoteView") || !widget.includes("quoteBadges")) {
  throw new Error("TypeScript widget fixture is missing fan-in/fan-out helpers");
}

if (!legacyWidget.includes("renderQuote")) {
  throw new Error("JavaScript compatibility widget is missing renderQuote");
}

if (!checkout.includes("createCheckoutPayload") || !checkout.includes("renderCheckout")) {
  throw new Error("TypeScript checkout fixture is missing cross-module fan-in helpers");
}

if (!contract.includes('"quote.v1"') || !pricing.includes('"CA-ON"')) {
  throw new Error("L4 contract/state fixture assets are missing expected markers");
}

if (!l4Doc.includes("quote.v1") || !l4Doc.includes("src/demo_app/service.py")) {
  throw new Error("L4 quote contract doc is missing code binding markers");
}

if (!manifest.hint_count || !manifest.files.includes("src/demo_app/service.py")) {
  throw new Error("L4 fixture materializer did not load governance hints or service materialization block");
}

console.log("external governance mixed smoke ok");
````

#### tests/widget.test.mjs

````file path="tests/widget.test.mjs"
import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(join(process.cwd(), "web", "widget.ts"), "utf8");
const checkout = readFileSync(join(process.cwd(), "web", "checkout.ts"), "utf8");

for (const symbol of [
  "normalizeLine",
  "lineTotal",
  "quoteSubtotal",
  "quoteDiscount",
  "quoteTax",
  "quoteBadges",
  "buildQuoteView",
  "renderQuoteCard",
  "renderAuditBadge",
]) {
  if (!source.includes(`function ${symbol}`)) {
    throw new Error(`widget.ts is missing ${symbol}`);
  }
}

for (const symbol of ["createCheckoutPayload", "createCheckoutSummary", "renderCheckout"]) {
  if (!checkout.includes(`function ${symbol}`)) {
    throw new Error(`checkout.ts is missing ${symbol}`);
  }
}

if (!checkout.includes('from "./widget"')) {
  throw new Error("checkout.ts should import widget helpers for graph relation smoke");
}

console.log("external governance widget smoke ok");
````

#### web/widget.js

````file path="web/widget.js"
export function renderQuote(total) {
  return `Total: ${total.toFixed(2)}`;
}

````

#### web/widget.ts

````file path="web/widget.ts"
export interface QuoteLine {
  sku: string;
  price: number;
  quantity?: number;
  hazmat?: boolean;
}

export interface QuoteView {
  subtotal: number;
  discount: number;
  tax: number;
  total: number;
  badges: string[];
}

export function normalizeLine(line: QuoteLine): Required<QuoteLine> {
  return {
    sku: line.sku || "unknown",
    price: line.price,
    quantity: line.quantity ?? 1,
    hazmat: line.hazmat ?? false,
  };
}

export function lineTotal(line: QuoteLine): number {
  const normalized = normalizeLine(line);
  return normalized.price * normalized.quantity;
}

export function quoteSubtotal(lines: QuoteLine[]): number {
  return lines.reduce((total, line) => total + lineTotal(line), 0);
}

export function quoteDiscount(subtotal: number, tier = "standard"): number {
  if (tier === "enterprise") return subtotal * 0.15;
  if (tier === "member") return subtotal * 0.05;
  return 0;
}

export function quoteTax(amount: number, taxRate = 0.13): number {
  return amount * taxRate;
}

export function quoteBadges(lines: QuoteLine[]): string[] {
  const normalized = lines.map(normalizeLine);
  return normalized.some((line) => line.hazmat) ? ["hazmat"] : [];
}

export function buildQuoteView(lines: QuoteLine[], tier = "standard", taxRate = 0.13): QuoteView {
  const subtotal = quoteSubtotal(lines);
  const discount = quoteDiscount(subtotal, tier);
  const taxable = subtotal - discount;
  const tax = quoteTax(taxable, taxRate);
  return {
    subtotal,
    discount,
    tax,
    total: taxable + tax,
    badges: quoteBadges(lines),
  };
}

export function renderQuoteSummary(lines: QuoteLine[], taxRate = 0.13): string {
  const subtotal = quoteSubtotal(lines);
  const total = subtotal + quoteTax(subtotal, taxRate);
  return `Total: ${total.toFixed(2)}`;
}

export function renderQuoteCard(lines: QuoteLine[], tier = "standard"): string {
  const view = buildQuoteView(lines, tier);
  return `${renderQuoteSummary(lines)} / ${view.badges.length} badges`;
}

export function renderAuditBadge(lines: QuoteLine[]): string {
  const badges = quoteBadges(lines);
  return badges.length ? `Review: ${badges.join(",")}` : "Review: clear";
}
````

#### web/checkout.ts

````file path="web/checkout.ts"
import {
  buildQuoteView,
  renderAuditBadge,
  renderQuoteCard,
  type QuoteLine,
  type QuoteView,
} from "./widget";

export interface CheckoutPayload {
  contract: "quote.v1";
  quote: QuoteView;
  reviewLabel: string;
}

export function createCheckoutPayload(lines: QuoteLine[], tier = "standard"): CheckoutPayload {
  return {
    contract: "quote.v1",
    quote: buildQuoteView(lines, tier),
    reviewLabel: renderAuditBadge(lines),
  };
}

export function createCheckoutSummary(lines: QuoteLine[], tier = "standard"): string {
  const payload = createCheckoutPayload(lines, tier);
  return `${payload.contract} ${payload.quote.total.toFixed(2)} ${payload.reviewLabel}`;
}

export function renderCheckout(lines: QuoteLine[], tier = "standard"): string {
  return `${renderQuoteCard(lines, tier)} | ${createCheckoutSummary(lines, tier)}`;
}
````
