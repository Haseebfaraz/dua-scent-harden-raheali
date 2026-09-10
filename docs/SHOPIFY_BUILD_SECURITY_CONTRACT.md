# Shopify Build Security Contract (theme / frontend integration)

Audience: the developer maintaining the Shopify theme (`custom-scent-product.liquid` and the
chat widget). This describes only what the storefront needs to send and expects back. It contains
no secrets and no internal implementation detail beyond what the browser already sees.

Status: **the live theme's slider request is no longer accepted.** Until the theme is updated as
described in section 4, the "adjust sliders on the product page and Save Build / Add to Cart"
path returns an error that tells the customer to reopen their preview from the chat. The preview
page itself (served by the backend through the App Proxy) already implements this contract.

---

## 1. Concepts

* **recommendationId**: opaque id of one fragrance recommendation. It identifies, it does not
  authorize.
* **buildToken** (the *build capability*): an opaque, server-minted, high-entropy string tied to
  exactly one recommendation. It is issued when the backend tells the chat widget the preview is
  ready, it expires after 7 days, and it can be revoked. Whoever presents it may read that one
  preview and change that one build. Treat it like a password: keep it out of logs, analytics,
  page titles, and shared URLs.

Neither a Shopify product GID, nor a conversation id, nor the browser's `Origin` header plays any
part in authorization any more.

## 2. Where the browser receives the token

1. The chat widget receives the `preview_ready` SSE event exactly as before. Its `previewUrl`
   now carries two query parameters:

   ```
   https://<store>/apps/scent-library/fragrance-preview?recommendationId=<id>&bt=<buildToken>
   ```

   The widget must navigate to the URL unchanged. Shopify's App Proxy forwards and signs both
   parameters.
2. The preview page reads both values from its own page data and includes `buildToken` in every
   POST it makes (this is already implemented in the backend-served page).
3. After a successful **Save Build**, the preview page redirects to the product page with the
   capability in the URL fragment (never sent to any server, never in `Referer`):

   ```
   https://<store>/products/<handle>#scentBuild=<recommendationId>.<buildToken>
   ```

   The theme must read `location.hash`, keep the two values in `sessionStorage` (not
   `localStorage`, not a cookie), and remove the fragment with `history.replaceState`.

## 3. Endpoint: `POST /api/save-build` (theme slider re-price)

Request headers: `Content-Type: application/json`. The browser sets `Origin` automatically; only
the store's own origins are accepted (`https://<store>.myshopify.com` and any origin configured
by the operator for a custom storefront domain).

Request body (all fields required except `name`; unknown fields are rejected):

```json
{
  "recommendationId": "<from the fragment>",
  "buildToken": "<from the fragment>",
  "ratios": { "top": 40, "middle": 30, "base": 30 },
  "name": "Optional new fragrance name"
}
```

Ratio rules (validated server-side, identical on every path):

* exactly the three keys `top`, `middle`, `base`;
* whole-number percentages (integers; `40.0` is accepted, `40.5` is not);
* each between 1 and 98 inclusive (the slider UI keeps layers at 5 or above);
* the three must total exactly 100.

Name rules: optional; trimmed; at most 80 characters; no control or invisible formatting
characters. Omit it, or send the current title, to leave the product title unchanged.

Success response `200`:

```json
{ "price": "136.00", "variantId": "gid://shopify/ProductVariant/123", "created": true }
```

Failure responses (JSON with `error` for display and a stable `code`):

| Status | `code` | Meaning / what the theme should do |
|---|---|---|
| 400 | `invalid_ratios`, `invalid_name`, `invalid_request`, `invalid_json`, `invalid_price` | Fix the input; show `error`. |
| 400 | `build_contract_upgraded` | The request used the retired `productId` shape. Update the theme (section 4). |
| 403 | `build_not_authorized` | Token missing, wrong, expired, or for a different recommendation. Ask the customer to reopen the preview from the chat. |
| 403 | `origin_not_allowed` | Not the store's origin. |
| 404 | `build_product_invalid` | The stored product is not a valid custom-scent build. Reopen the preview. |
| 409 | `build_not_saved` | No product exists yet for this recommendation; the customer must Save Build from the preview first. |
| 401 | (none) | Store not connected to Shopify; admin action required. |
| 502 / 500 / 503 | (none) | Transient; retry later. |

The price in the response is always computed by the server. Never display or submit a price
computed in the browser as authoritative.

## 4. Migration from the old slider request

Old (no longer accepted):

```json
{ "productId": "gid://shopify/Product/…", "ratios": {…}, "name": "…" }
```

New: see section 3. Required theme work:

1. On product-page load, parse `#scentBuild=<recommendationId>.<buildToken>` from
   `location.hash`, store both in `sessionStorage`, then strip the fragment.
2. Send `recommendationId` and `buildToken` from `sessionStorage` instead of the product GID.
3. On `403 build_not_authorized` or `400 build_contract_upgraded`, show the returned `error`
   and offer a link back to the chat.
4. Do not put the token into `localStorage`, cookies, analytics events, or the page URL.

Customers arriving on a product page without the fragment (for example from a shared link) can
view and buy existing variants normally; only the slider re-price needs the capability.

## 5. Preview page actions (already implemented server-side)

`POST /apps/scent-library/fragrance-preview` (through the App Proxy) accepts
`{ "intent": "recreate" | "save_build" | "add_to_cart", "recommendationId", "buildToken", "name", "ratios" }`
with the same ratio and name rules. An unauthorized request returns
`{"error": "...", "code": "build_not_authorized"}`. The GET that renders the page requires
`bt=<buildToken>` in its query string.
