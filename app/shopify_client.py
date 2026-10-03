import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


DEFAULT_API_VERSION = "2026-01"

_GONE_ERROR_MARKERS = ("does not exist", "not found", "invalid id", "no longer exists")


def is_gone_error(e):
    """True when a Shopify error means the resource is already gone.

    The rule callers encode with this: gone -> safe to proceed locally
    (delete the row / recreate the resource); anything else is transient and
    must surface, or a live resource silently becomes invisible in the app."""
    return any(t in str(e).lower() for t in _GONE_ERROR_MARKERS)

METAFIELDS_SET_MUTATION = """
mutation ($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key }
    userErrors { field message }
  }
}
"""


class ShopifyClient:
    def __init__(self, domain, access_token, api_version=DEFAULT_API_VERSION):
        self.domain = domain.rstrip("/")
        if not self.domain.endswith(".myshopify.com"):
            self.domain = f"{self.domain}.myshopify.com"
        self.access_token = access_token
        self.api_version = api_version
        self.graphql_url = (
            f"https://{self.domain}/admin/api/{self.api_version}/graphql.json"
        )
        self.headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    def _graphql(self, query, variables=None):
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        max_attempts = 5
        throttle_budget = 5
        attempt = 0
        while True:
            try:
                resp = requests.post(
                    self.graphql_url, json=payload, headers=self.headers, timeout=30
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                attempt += 1
                if attempt >= max_attempts:
                    raise
                wait = 2 ** attempt
                logger.warning("Shopify connection error (attempt %d/%d), retrying in %ds: %s", attempt, max_attempts, wait, e)
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                attempt += 1
                if attempt >= max_attempts:
                    raise Exception(
                        f"Shopify request failed after {attempt} attempts (HTTP {resp.status_code})"
                    )
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning("Shopify HTTP %d, retrying in %.1fs (attempt %d/%d)", resp.status_code, wait, attempt, max_attempts)
                time.sleep(min(wait, 30))
                continue

            resp.raise_for_status()
            data = resp.json()

            errors = data.get("errors")
            if errors:
                # GraphQL cost throttling arrives as HTTP 200 with code THROTTLED.
                throttled = any(
                    (e.get("extensions") or {}).get("code") == "THROTTLED"
                    for e in errors
                    if isinstance(e, dict)
                )
                if throttled:
                    if throttle_budget <= 0:
                        raise Exception("Shopify rate limited (THROTTLED) — retries exhausted")
                    throttle_budget -= 1
                    cost = (data.get("extensions") or {}).get("cost") or {}
                    status = cost.get("throttleStatus") or {}
                    requested = cost.get("requestedQueryCost") or 100
                    available = status.get("currentlyAvailable") or 0
                    restore = status.get("restoreRate") or 50
                    wait = max((requested - available) / max(restore, 1), 1.0)
                    logger.warning("Shopify GraphQL throttled, waiting %.1fs (budget %d left)", wait, throttle_budget)
                    time.sleep(min(wait, 30))
                    continue
                raise Exception(errors[0].get("message", str(errors)))
            return data.get("data", {})

    @staticmethod
    def _normalize_tags(tags):
        """Accept a list or comma-separated string; return a clean list of tags."""
        if isinstance(tags, str):
            return [t.strip() for t in tags.split(",") if t.strip()]
        if isinstance(tags, list):
            return [str(t).strip() for t in tags if str(t).strip()]
        return []

    def get_orders(self, query_filter=None, cursor=None, limit=50):
        """One page of orders with the customer journey summary used for
        revenue attribution. Requires the read_orders scope."""
        query = """
        query getOrders($limit: Int!, $cursor: String, $q: String) {
            orders(first: $limit, after: $cursor, query: $q, sortKey: CREATED_AT) {
                pageInfo { hasNextPage endCursor }
                nodes {
                    id
                    name
                    createdAt
                    test
                    currentTotalPriceSet { shopMoney { amount currencyCode } }
                    customerJourneySummary {
                        firstVisit { landingPage referrerUrl source sourceType utmParameters { source medium } occurredAt }
                        lastVisit { landingPage referrerUrl source sourceType utmParameters { source medium } occurredAt }
                    }
                }
            }
        }
        """
        data = self._graphql(query, {"limit": limit, "cursor": cursor, "q": query_filter})
        return data.get("orders", {})

    def test_connection(self):
        query = "{ shop { name id myshopifyDomain primaryDomain { host url } } }"
        data = self._graphql(query)
        shop = data.get("shop", {})
        primary = shop.get("primaryDomain") or {}
        return {
            "name": shop.get("name"),
            "domain": shop.get("myshopifyDomain"),
            "id": shop.get("id"),
            "primary_domain": primary.get("host"),
        }

    def get_shop_timezone(self):
        """Return the store's IANA timezone (e.g. 'America/Chicago'); 'UTC' on failure.

        Used so dates rendered server-side match Shopify's storefront, which
        displays timestamps in the store's local timezone.
        """
        try:
            data = self._graphql("{ shop { ianaTimezone } }")
            return (data.get("shop") or {}).get("ianaTimezone") or "UTC"
        except Exception:
            return "UTC"

    def product_exists(self, handle):
        """Return True if a product with this handle exists (any status, incl. drafts)."""
        if not handle:
            return False
        query = "query($q: String!) { products(first: 1, query: $q) { edges { node { id } } } }"
        data = self._graphql(query, {"q": f"handle:{handle}"})
        return bool(data.get("products", {}).get("edges"))

    def collection_exists(self, handle):
        """Return True if a collection with this handle exists (smart or manual)."""
        if not handle:
            return False
        query = "query($q: String!) { collections(first: 1, query: $q) { edges { node { id } } } }"
        data = self._graphql(query, {"q": f"handle:{handle}"})
        return bool(data.get("collections", {}).get("edges"))

    def get_page_by_handle(self, handle):
        """Return {id, title, handle} for the page with this handle, or None."""
        if not handle:
            return None
        query = "query($q: String!) { pages(first: 1, query: $q) { edges { node { id title handle } } } }"
        data = self._graphql(query, {"q": f"handle:{handle}"})
        edges = data.get("pages", {}).get("edges", [])
        return edges[0]["node"] if edges else None

    def page_exists(self, handle):
        """Return True if a page with this handle exists (published or not)."""
        if not handle:
            return False
        query = "query($q: String!) { pages(first: 1, query: $q) { edges { node { id } } } }"
        data = self._graphql(query, {"q": f"handle:{handle}"})
        return bool(data.get("pages", {}).get("edges"))

    def article_exists(self, blog_handle, handle):
        """Return True if an article with this handle exists, optionally within blog_handle."""
        if not handle:
            return False
        query = "query($q: String!) { articles(first: 10, query: $q) { edges { node { handle blog { handle } } } } }"
        data = self._graphql(query, {"q": f"handle:{handle}"})
        for edge in data.get("articles", {}).get("edges", []):
            node = edge.get("node", {})
            if node.get("handle") == handle:
                if not blog_handle or (node.get("blog") or {}).get("handle") == blog_handle:
                    return True
        return False

    def blog_exists(self, handle):
        """Return True if a blog with this handle exists."""
        if not handle:
            return False
        query = "{ blogs(first: 100) { edges { node { handle } } } }"
        data = self._graphql(query)
        return any(
            edge["node"].get("handle") == handle
            for edge in data.get("blogs", {}).get("edges", [])
        )

    def get_products(self, limit=50, cursor=None, query_filter=None):
        query = """
        query ($first: Int!, $after: String, $query: String) {
          products(first: $first, after: $after, query: $query) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                title
                handle
                descriptionHtml
                vendor
                productType
                tags
                status
                updatedAt
                seo { title description }
                media(first: 50) {
                  edges {
                    node {
                      ... on MediaImage {
                        id
                        alt
                        preview { image { url } }
                      }
                    }
                  }
                }
                variants(first: 50) {
                  edges {
                    node {
                      id title price sku availableForSale compareAtPrice barcode
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"first": limit, "after": cursor, "query": query_filter})
        products_data = data.get("products", {})
        page_info = products_data.get("pageInfo", {})
        edges = products_data.get("edges", [])

        products = []
        for edge in edges:
            node = edge["node"]
            products.append(
                {
                    "shopify_id": node["id"],
                    "title": node.get("title", ""),
                    "handle": node.get("handle", ""),
                    "description": node.get("descriptionHtml", ""),
                    "vendor": node.get("vendor", ""),
                    "product_type": node.get("productType", ""),
                    "tags": ", ".join(node.get("tags", [])),
                    "status": node.get("status", ""),
                    "shopify_updated_at": node.get("updatedAt"),
                    "meta_title": (node.get("seo") or {}).get("title", ""),
                    "meta_description": (node.get("seo") or {}).get("description", ""),
                    "images": [
                        {
                            "media_id": m["node"].get("id") or "",
                            "url": ((m["node"].get("preview") or {}).get("image") or {}).get("url", ""),
                            "alt": m["node"].get("alt") or "",
                        }
                        for m in (node.get("media", {}).get("edges", []))
                        if m["node"].get("id")
                    ],
                    "variants": [
                        {
                            "id": v["node"]["id"],
                            "title": v["node"].get("title", ""),
                            "price": v["node"].get("price", ""),
                            "sku": v["node"].get("sku", ""),
                            "available": v["node"].get("availableForSale", True),
                            "compare_at_price": v["node"].get("compareAtPrice", ""),
                            "barcode": v["node"].get("barcode", ""),
                        }
                        for v in (node.get("variants", {}).get("edges", []))
                    ],
                }
            )

        return {
            "products": products,
            "has_next": page_info.get("hasNextPage", False),
            "cursor": page_info.get("endCursor"),
        }

    def get_collections(self, limit=50, cursor=None):
        query = """
        query ($first: Int!, $after: String) {
          collections(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                title
                handle
                descriptionHtml
                updatedAt
                seo { title description }
                productsCount { count }
                image { url altText }
                ruleSet { appliedDisjunctively }
                resourcePublicationsV2(first: 20) { edges { node { isPublished } } }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"first": limit, "after": cursor})
        collections_data = data.get("collections", {})
        page_info = collections_data.get("pageInfo", {})
        edges = collections_data.get("edges", [])

        collections = []
        for edge in edges:
            node = edge["node"]
            image = node.get("image") or {}
            pub_edges = (node.get("resourcePublicationsV2") or {}).get("edges", [])
            is_published = any(
                (edge.get("node") or {}).get("isPublished", False)
                for edge in pub_edges
            )
            collections.append(
                {
                    "shopify_id": node["id"],
                    "title": node.get("title", ""),
                    "handle": node.get("handle", ""),
                    "description": node.get("descriptionHtml", ""),
                    "meta_title": (node.get("seo") or {}).get("title", ""),
                    "meta_description": (node.get("seo") or {}).get("description", ""),
                    "products_count": (node.get("productsCount") or {}).get("count", 0),
                    "image_url": image.get("url", ""),
                    "collection_type": "smart" if node.get("ruleSet") is not None else "manual",
                    "published": is_published,
                    "shopify_updated_at": node.get("updatedAt"),
                }
            )

        return {
            "collections": collections,
            "has_next": page_info.get("hasNextPage", False),
            "cursor": page_info.get("endCursor"),
        }

    def update_product(self, shopify_id, fields):
        product_input = {"id": shopify_id}
        if "title" in fields:
            product_input["title"] = fields["title"]
        if "description_html" in fields:
            product_input["descriptionHtml"] = fields["description_html"]
        if "tags" in fields:
            product_input["tags"] = self._normalize_tags(fields["tags"])
        if "product_type" in fields:
            product_input["productType"] = fields["product_type"]

        seo = {}
        if "meta_title" in fields:
            seo["title"] = fields["meta_title"]
        if "meta_description" in fields:
            seo["description"] = fields["meta_description"]
        if seo:
            product_input["seo"] = seo

        query = """
        mutation ($product: ProductUpdateInput!) {
          productUpdate(product: $product) {
            product { id title }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"product": product_input})
        result = data.get("productUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify productUpdate error: {errors[0].get('message', str(errors))}")
        return result.get("product", {})

    def update_product_media(self, shopify_id, media_updates):
        valid = [m for m in media_updates if m.get("media_id") and m.get("alt") is not None]
        if not valid:
            return []

        files_input = [{"id": m["media_id"], "alt": m["alt"]} for m in valid]
        query = """
        mutation fileUpdate($files: [FileUpdateInput!]!) {
          fileUpdate(files: $files) {
            files { ... on MediaImage { id alt } }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, variables={"files": files_input})
        result = data.get("fileUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify media update error: {errors[0].get('message', str(errors))}")

        updated_files = result.get("files", [])
        logger.info("Updated alt text for %d/%d images on product %s", len(updated_files), len(valid), shopify_id)
        return updated_files

    def iter_products(self, page_size=50, query_filter=None):
        cursor = None
        while True:
            result = self.get_products(limit=page_size, cursor=cursor, query_filter=query_filter)
            yield result["products"]
            if not result["has_next"]:
                break
            cursor = result["cursor"]

    def get_all_products(self, query_filter=None):
        all_products = []
        for page in self.iter_products(query_filter=query_filter):
            all_products.extend(page)
        return all_products

    def get_all_collection_products(self, shopify_id):
        all_products = []
        cursor = None
        query = """
        query ($id: ID!, $first: Int!, $after: String) {
          collection(id: $id) {
            products(first: $first, after: $after) {
              pageInfo { hasNextPage endCursor }
              edges {
                node { id title handle status }
              }
            }
          }
        }
        """
        while True:
            variables = {"id": shopify_id, "first": 50, "after": cursor}
            data = self._graphql(query, variables=variables)
            collection = data.get("collection") or {}
            products_data = collection.get("products") or {}
            page_info = products_data.get("pageInfo", {})
            edges = products_data.get("edges", [])

            for edge in edges:
                node = edge["node"]
                all_products.append({
                    "shopify_id": node["id"],
                    "title": node.get("title", ""),
                    "handle": node.get("handle", ""),
                    "status": node.get("status", ""),
                })

            if not page_info.get("hasNextPage", False):
                break
            cursor = page_info.get("endCursor")
        return all_products

    def update_collection(self, shopify_id, fields):
        collection_input = {"id": shopify_id}
        if "title" in fields:
            collection_input["title"] = fields["title"]
        if "description_html" in fields:
            collection_input["descriptionHtml"] = fields["description_html"]

        seo = {}
        if "meta_title" in fields:
            seo["title"] = fields["meta_title"]
        if "meta_description" in fields:
            seo["description"] = fields["meta_description"]
        if seo:
            collection_input["seo"] = seo

        query = """
        mutation ($input: CollectionInput!) {
          collectionUpdate(input: $input) {
            collection { id title }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"input": collection_input})
        result = data.get("collectionUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify collectionUpdate error: {errors[0].get('message', str(errors))}")
        return result.get("collection", {})

    def is_smart_collection(self, collection_shopify_id):
        query = """
        query ($id: ID!) {
          collection(id: $id) {
            ruleSet { appliedDisjunctively }
          }
        }
        """
        data = self._graphql(query, {"id": collection_shopify_id})
        collection = data.get("collection") or {}
        return collection.get("ruleSet") is not None

    def add_products_to_collection(self, collection_shopify_id, product_shopify_ids):
        if not product_shopify_ids:
            return []
        query = """
        mutation ($id: ID!, $productIds: [ID!]!) {
          collectionAddProducts(id: $id, productIds: $productIds) {
            collection { id title }
            userErrors { field message }
          }
        }
        """
        result = None
        for i in range(0, len(product_shopify_ids), 250):
            batch = product_shopify_ids[i:i + 250]
            data = self._graphql(query, {
                "id": collection_shopify_id,
                "productIds": batch,
            })
            result = data.get("collectionAddProducts", {})
            errors = result.get("userErrors", [])
            if errors:
                raise Exception(f"Shopify collectionAddProducts error: {errors[0].get('message', str(errors))}")
        return (result or {}).get("collection", {})

    def get_all_collections(self):
        all_collections = []
        cursor = None
        while True:
            result = self.get_collections(limit=50, cursor=cursor)
            all_collections.extend(result["collections"])
            if not result["has_next"]:
                break
            cursor = result["cursor"]
        return all_collections

    def set_product_metafield(self, product_shopify_id, namespace, key, value, value_type="json"):
        query = METAFIELDS_SET_MUTATION
        variables = {
            "metafields": [
                {
                    "ownerId": product_shopify_id,
                    "namespace": namespace,
                    "key": key,
                    "value": value,
                    "type": value_type,
                }
            ]
        }
        data = self._graphql(query, variables)
        result = data.get("metafieldsSet", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify metafieldsSet error: {errors[0].get('message', str(errors))}")
        return result.get("metafields", [])

    def delete_metafields(self, triples):
        """Bulk-delete metafields by (owner_gid, namespace, key) triples.
        Owner-agnostic — works for Product, Collection and Shop GIDs. Chunks of
        25 per mutation; raises on userErrors; returns deleted identifiers."""
        mutation = """
        mutation ($metafields: [MetafieldIdentifierInput!]!) {
          metafieldsDelete(metafields: $metafields) {
            deletedMetafields { ownerId namespace key }
            userErrors { field message }
          }
        }
        """
        deleted = []
        triples = list(triples)
        for i in range(0, len(triples), 25):
            chunk = [
                {"ownerId": owner, "namespace": ns, "key": key}
                for owner, ns, key in triples[i : i + 25]
            ]
            data = self._graphql(mutation, {"metafields": chunk})
            result = data.get("metafieldsDelete", {})
            errors = result.get("userErrors", [])
            if errors:
                raise Exception(f"Shopify metafieldsDelete error: {errors[0].get('message', str(errors))}")
            deleted.extend(result.get("deletedMetafields") or [])
        return deleted

    def iter_products_metafields(self, namespace, first=100, mf_first=10):
        """Yield {shopify_id, handle, title, metafields:[{id,namespace,key,value,type}]}
        for every product, reading only the given metafield namespace — one
        paginated query instead of per-product round-trips."""
        query = """
        query ($first: Int!, $cursor: String, $ns: String!, $mfFirst: Int!) {
          products(first: $first, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                handle
                title
                metafields(first: $mfFirst, namespace: $ns) {
                  edges { node { id namespace key value type } }
                }
              }
            }
          }
        }
        """
        cursor = None
        while True:
            data = self._graphql(
                query, {"first": first, "cursor": cursor, "ns": namespace, "mfFirst": mf_first}
            )
            products = data.get("products") or {}
            for edge in products.get("edges", []):
                node = edge.get("node") or {}
                yield {
                    "shopify_id": node.get("id"),
                    "handle": node.get("handle"),
                    "title": node.get("title"),
                    "metafields": [
                        m.get("node")
                        for m in (node.get("metafields") or {}).get("edges", [])
                        if m.get("node")
                    ],
                }
            page = products.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

    def get_shop_id(self):
        data = self._graphql("{ shop { id } }")
        return (data.get("shop") or {}).get("id")

    def get_publications(self):
        # catalog.title is only set for catalog-backed publications (markets/B2B);
        # app channels like Online Store still need the legacy name field.
        query = """
        {
          publications(first: 50) {
            edges {
              node { id name catalog { title } }
            }
          }
        }
        """
        data = self._graphql(query)
        edges = (data.get("publications") or {}).get("edges", [])
        return [
            {
                "id": edge["node"]["id"],
                "name": (
                    ((edge["node"].get("catalog") or {}).get("title"))
                    or edge["node"].get("name")
                    or ""
                ),
            }
            for edge in edges
        ]

    def publish_to_all_channels(self, resource_shopify_id):
        """Publish the resource to the Online Store channel.

        (Name kept for caller compatibility.) Publishing to every app channel
        (Google, Faire, POS, ...) caused partial-success errors and unwanted
        third-party listings; if the Online Store publication can't be
        identified, falls back to the previous publish-everywhere behavior.
        """
        publications = self.get_publications()
        if not publications:
            return []

        online_store = [
            p for p in publications
            if (p.get("name") or "").strip().lower() == "online store"
        ]
        if online_store:
            publications = online_store
        else:
            logger.warning(
                "Online Store publication not found among %d channels — publishing to all",
                len(publications),
            )

        publication_inputs = [{"publicationId": p["id"]} for p in publications]
        query = """
        mutation ($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) {
            publishable { availablePublicationsCount { count } }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"id": resource_shopify_id, "input": publication_inputs})
        result = data.get("publishablePublish", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify publishablePublish error: {errors[0].get('message', str(errors))}")
        return publications

    def delete_collection(self, shopify_id):
        query = """
        mutation ($input: CollectionDeleteInput!) {
          collectionDelete(input: $input) {
            deletedCollectionId
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"input": {"id": shopify_id}})
        result = data.get("collectionDelete", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify collectionDelete error: {errors[0].get('message', str(errors))}")
        return result.get("deletedCollectionId")

    def create_collection(self, title, description_html=""):
        collection_input = {"title": title}
        if description_html:
            collection_input["descriptionHtml"] = description_html

        query = """
        mutation ($input: CollectionInput!) {
          collectionCreate(input: $input) {
            collection { id title handle }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"input": collection_input})
        result = data.get("collectionCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify collectionCreate error: {errors[0].get('message', str(errors))}")
        collection = result.get("collection", {})
        return {
            "shopify_id": collection.get("id", ""),
            "title": collection.get("title", ""),
            "handle": collection.get("handle", ""),
        }

    def create_smart_collection(self, title, vendor=None, description_html="", seo_title="",
                                seo_description="", rules=None, applied_disjunctively=False,
                                handle=None):
        """Create a rule-based (smart) collection. Pass `rules` as
        [{column, relation, condition}] for arbitrary conditions, or `vendor`
        alone for the single VENDOR EQUALS rule brand pages use.

        productsCount comes back from the mutation because a smart collection's
        real membership (which includes draft products and honors publication)
        is not something a local preview can know exactly."""
        if not rules:
            if not vendor:
                raise ValueError("create_smart_collection needs either rules or vendor")
            rules = [{"column": "VENDOR", "relation": "EQUALS", "condition": vendor}]

        query = """
        mutation ($input: CollectionInput!) {
          collectionCreate(input: $input) {
            collection {
              id title handle
              productsCount { count }
              ruleSet { appliedDisjunctively rules { column relation condition } }
            }
            userErrors { field message }
          }
        }
        """
        input_vars = {
            "title": title,
            "ruleSet": {
                "appliedDisjunctively": bool(applied_disjunctively),
                "rules": rules,
            },
        }
        if handle:
            input_vars["handle"] = handle
        if description_html:
            input_vars["descriptionHtml"] = description_html
        if seo_title or seo_description:
            input_vars["seo"] = {}
            if seo_title:
                input_vars["seo"]["title"] = seo_title
            if seo_description:
                input_vars["seo"]["description"] = seo_description

        data = self._graphql(query, {"input": input_vars})
        result = data.get("collectionCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify collectionCreate error: {errors[0].get('message', str(errors))}")
        collection = result.get("collection", {})
        return {
            "shopify_id": collection.get("id", ""),
            "title": collection.get("title", ""),
            "handle": collection.get("handle", ""),
            "products_count": (collection.get("productsCount") or {}).get("count"),
            "rule_set": collection.get("ruleSet"),
        }

    def get_collection_rule_conditions(self):
        """Which relations each rule column actually accepts. The GraphQL enums
        are permissive — {TAG, CONTAINS} validates fine and then fails at
        runtime as a userError — so proposed rules are checked against this."""
        query = """
        query { collectionRulesConditions { ruleType allowedRelations defaultRelation } }
        """
        data = self._graphql(query)
        return data.get("collectionRulesConditions", []) or []

    def get_collection_products_count(self, collection_shopify_id):
        query = """
        query ($id: ID!) {
          collection(id: $id) { productsCount { count } }
        }
        """
        data = self._graphql(query, {"id": collection_shopify_id})
        collection = data.get("collection") or {}
        return (collection.get("productsCount") or {}).get("count")

    def staged_upload_create(self, filename, mime_type, file_size, resource="PRODUCT_IMAGE"):
        query = """
        mutation ($input: [StagedUploadInput!]!) {
          stagedUploadsCreate(input: $input) {
            stagedTargets {
              url
              resourceUrl
              parameters { name value }
            }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": [
                {
                    "resource": resource,
                    "filename": filename,
                    "mimeType": mime_type,
                    "fileSize": str(file_size),
                    "httpMethod": "POST",
                }
            ]
        }
        data = self._graphql(query, variables)
        result = data.get("stagedUploadsCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify stagedUploadsCreate error: {errors[0].get('message', str(errors))}")
        targets = result.get("stagedTargets", [])
        if not targets:
            raise Exception("No staged upload target returned")
        return targets[0]

    def upload_staged_file(self, staged_target, file_bytes, content_type="image/png", filename="image.png"):
        form_data = {p["name"]: p["value"] for p in staged_target["parameters"]}
        import requests as req
        files = {"file": (filename, file_bytes, content_type)}
        resp = req.post(staged_target["url"], data=form_data, files=files, timeout=60)
        resp.raise_for_status()
        return staged_target["resourceUrl"]

    def file_create(self, resource_url, filename=None, alt_text=None):
        query = """
        mutation ($files: [FileCreateInput!]!) {
          fileCreate(files: $files) {
            files {
              ... on MediaImage {
                id
                fileStatus
                alt
                image { url }
              }
              ... on GenericFile {
                id
                fileStatus
                url
              }
            }
            userErrors { field message }
          }
        }
        """
        file_input = {"originalSource": resource_url, "contentType": "IMAGE"}
        if filename:
            file_input["filename"] = filename
        if alt_text:
            file_input["alt"] = alt_text
        data = self._graphql(query, {"files": [file_input]})
        result = data.get("fileCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify fileCreate error: {errors[0].get('message', str(errors))}")
        files = result.get("files", [])
        return files[0] if files else {}

    def file_get_url(self, file_id, max_attempts=10):
        query = """
        query ($id: ID!) {
          node(id: $id) {
            ... on MediaImage {
              fileStatus
              image { url }
            }
            ... on GenericFile {
              fileStatus
              url
            }
          }
        }
        """
        for _ in range(max_attempts):
            data = self._graphql(query, {"id": file_id})
            node = data.get("node")
            if node is None:
                raise Exception(f"Shopify file not found: {file_id}")
            status = node.get("fileStatus")
            if status == "READY":
                url = ((node.get("image") or {}).get("url")) or node.get("url")
                if url:
                    return url
            elif status == "FAILED":
                raise Exception("Shopify file processing failed")
            time.sleep(2)
        raise Exception("Shopify file processing timed out")

    def delete_files(self, file_ids):
        query = """
        mutation ($fileIds: [ID!]!) {
          fileDelete(fileIds: $fileIds) {
            deletedFileIds
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"fileIds": file_ids})
        result = data.get("fileDelete", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify fileDelete error: {errors[0].get('message', str(errors))}")
        return result.get("deletedFileIds", [])

    def create_product_media(self, product_shopify_id, resource_url, alt_text):
        query = """
        mutation ($productId: ID!, $media: [CreateMediaInput!]!) {
          productCreateMedia(productId: $productId, media: $media) {
            media {
              ... on MediaImage {
                id
                alt
                preview { image { url } }
              }
            }
            mediaUserErrors { field message }
          }
        }
        """
        variables = {
            "productId": product_shopify_id,
            "media": [
                {
                    "originalSource": resource_url,
                    "alt": alt_text,
                    "mediaContentType": "IMAGE",
                }
            ],
        }
        data = self._graphql(query, variables)
        result = data.get("productCreateMedia", {})
        errors = result.get("mediaUserErrors", [])
        if errors:
            raise Exception(f"Shopify productCreateMedia error: {errors[0].get('message', str(errors))}")
        media_list = result.get("media", [])
        return media_list[0] if media_list else {}

    def get_product_metafield(self, product_shopify_id, namespace, key):
        query = """
        query ($id: ID!, $namespace: String!, $key: String!) {
          product(id: $id) {
            metafield(namespace: $namespace, key: $key) {
              id namespace key value type
            }
          }
        }
        """
        data = self._graphql(query, {
            "id": product_shopify_id,
            "namespace": namespace,
            "key": key,
        })
        product = data.get("product") or {}
        return product.get("metafield")

    def get_product_metafields(self, product_shopify_id, first=40):
        """List a product's metafields (namespace/key/value/type) for spec extraction."""
        query = """
        query ($id: ID!, $first: Int!) {
          product(id: $id) {
            metafields(first: $first) {
              edges { node { namespace key value type } }
            }
          }
        }
        """
        data = self._graphql(query, {"id": product_shopify_id, "first": first})
        product = data.get("product") or {}
        edges = (product.get("metafields") or {}).get("edges", [])
        return [e["node"] for e in edges if e.get("node")]

    def get_collection_products_with_images(self, collection_shopify_id, limit=4):
        query = """
        query ($id: ID!, $first: Int!) {
          collection(id: $id) {
            products(first: $first) {
              edges {
                node {
                  id
                  title
                  media(first: 1) {
                    edges {
                      node {
                        ... on MediaImage {
                          preview { image { url } }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"id": collection_shopify_id, "first": limit})
        collection = data.get("collection") or {}
        edges = (collection.get("products") or {}).get("edges", [])

        products = []
        for edge in edges:
            node = edge["node"]
            media_edges = (node.get("media") or {}).get("edges", [])
            image_url = ""
            for m in media_edges:
                image_url = ((m["node"].get("preview") or {}).get("image") or {}).get("url", "")
                if image_url:
                    break
            if image_url:
                products.append({
                    "shopify_id": node["id"],
                    "title": node.get("title", ""),
                    "image_url": image_url,
                })
        return products

    def get_blogs(self, limit=50, cursor=None):
        query = """
        query ($first: Int!, $after: String) {
          blogs(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                title
                handle
                commentPolicy
                articlesCount { count }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"first": limit, "after": cursor})
        blogs_data = data.get("blogs", {})
        page_info = blogs_data.get("pageInfo", {})
        edges = blogs_data.get("edges", [])
        blogs = []
        for edge in edges:
            node = edge["node"]
            blogs.append({
                "shopify_id": node["id"],
                "title": node.get("title", ""),
                "handle": node.get("handle", ""),
                "comment_policy": node.get("commentPolicy", ""),
                "articles_count": (node.get("articlesCount") or {}).get("count", 0),
            })
        return {
            "blogs": blogs,
            "has_next": page_info.get("hasNextPage", False),
            "cursor": page_info.get("endCursor"),
        }

    def get_all_blogs(self):
        all_blogs = []
        cursor = None
        while True:
            result = self.get_blogs(limit=50, cursor=cursor)
            all_blogs.extend(result["blogs"])
            if not result["has_next"]:
                break
            cursor = result["cursor"]
        return all_blogs

    def create_blog(self, title):
        query = """
        mutation ($blog: BlogCreateInput!) {
          blogCreate(blog: $blog) {
            blog { id title handle }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"blog": {"title": title}})
        result = data.get("blogCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify blogCreate error: {errors[0].get('message', str(errors))}")
        blog = result.get("blog", {})
        return {
            "shopify_id": blog.get("id", ""),
            "title": blog.get("title", ""),
            "handle": blog.get("handle", ""),
        }

    def delete_blog(self, shopify_id):
        query = """
        mutation ($id: ID!) {
          blogDelete(id: $id) {
            deletedBlogId
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"id": shopify_id})
        result = data.get("blogDelete", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify blogDelete error: {errors[0].get('message', str(errors))}")
        return result.get("deletedBlogId")

    def update_blog(self, shopify_id, fields):
        query = """
        mutation ($id: ID!, $blog: BlogUpdateInput!) {
          blogUpdate(id: $id, blog: $blog) {
            blog { id title handle }
            userErrors { field message }
          }
        }
        """
        blog_input = {}
        if "title" in fields:
            blog_input["title"] = fields["title"]
        data = self._graphql(query, {"id": shopify_id, "blog": blog_input})
        result = data.get("blogUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify blogUpdate error: {errors[0].get('message', str(errors))}")
        blog = result.get("blog", {})
        return {
            "shopify_id": blog.get("id", ""),
            "title": blog.get("title", ""),
            "handle": blog.get("handle", ""),
        }

    def get_articles(self, limit=50, cursor=None, blog_id=None):
        # The blog_id search filter expects the numeric ID, not the GID.
        blog_filter = None
        if blog_id:
            blog_filter = f"blog_id:{str(blog_id).rsplit('/', 1)[-1]}"
        query = """
        query ($first: Int!, $after: String, $query: String) {
          articles(first: $first, after: $after, query: $query) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                title
                handle
                body
                summary
                tags
                blog { id title handle }
                author { name }
                image { url altText }
                publishedAt
                createdAt
                templateSuffix
                metafields(first: 10, namespace: "global") {
                  edges {
                    node { key value }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"first": limit, "after": cursor, "query": blog_filter})
        articles_data = data.get("articles", {})
        page_info = articles_data.get("pageInfo", {})
        edges = articles_data.get("edges", [])
        articles = []
        for edge in edges:
            node = edge["node"]
            image = node.get("image") or {}
            blog = node.get("blog") or {}
            author = node.get("author") or {}
            meta_title = ""
            meta_description = ""
            news_category = ""
            news_date_modified = ""
            for mf_edge in (node.get("metafields") or {}).get("edges", []):
                mf = mf_edge.get("node", {})
                if mf.get("key") == "title_tag":
                    meta_title = mf.get("value", "")
                elif mf.get("key") == "description_tag":
                    meta_description = mf.get("value", "")
                elif mf.get("key") == "news_category":
                    news_category = mf.get("value", "")
                elif mf.get("key") == "news_date_modified":
                    news_date_modified = mf.get("value", "")
            articles.append({
                "shopify_id": node["id"],
                "title": node.get("title", ""),
                "handle": node.get("handle", ""),
                "body": node.get("body", ""),
                "summary": node.get("summary", ""),
                "tags": ", ".join(node.get("tags", [])),
                "blog_shopify_id": blog.get("id", ""),
                "blog_title": blog.get("title", ""),
                "blog_handle": blog.get("handle", ""),
                "author": author.get("name", ""),
                "meta_title": meta_title,
                "meta_description": meta_description,
                "image_url": image.get("url", ""),
                "image_alt": image.get("altText", ""),
                "published_at": node.get("publishedAt"),
                "created_at": node.get("createdAt"),
                "is_published": node.get("publishedAt") is not None,
                "template_suffix": node.get("templateSuffix") or "",
                "news_category": news_category,
                "news_date_modified": news_date_modified,
            })
        return {
            "articles": articles,
            "has_next": page_info.get("hasNextPage", False),
            "cursor": page_info.get("endCursor"),
        }

    def get_all_articles(self, blog_id=None):
        all_articles = []
        cursor = None
        while True:
            result = self.get_articles(limit=50, cursor=cursor, blog_id=blog_id)
            all_articles.extend(result["articles"])
            if not result["has_next"]:
                break
            cursor = result["cursor"]
        return all_articles

    def get_article(self, shopify_id):
        """Live {id, handle, body} of one article, or None if it no longer exists."""
        query = """
        query ($id: ID!) {
          article(id: $id) { id handle body }
        }
        """
        data = self._graphql(query, {"id": shopify_id})
        return data.get("article")

    _ITEM_CONTENT_QUERIES = {
        "product": "product(id: $id) { title body: descriptionHtml updatedAt seo { title description } }",
        "collection": "collection(id: $id) { title body: descriptionHtml updatedAt seo { title description } }",
        "article": (
            "article(id: $id) { title body summary updatedAt "
            'metafields(first: 10, namespace: "global") { edges { node { key value } } } }'
        ),
        "page": (
            "page(id: $id) { title body updatedAt "
            'metafields(first: 10, namespace: "global") { edges { node { key value } } } }'
        ),
    }

    def get_item_content(self, item_type, shopify_id):
        """Live content of one item under canonical keys (title, body,
        meta_title, meta_description, summary for articles, updated_at), or
        None if it no longer exists. Read before every write so an edit made
        in Shopify admin is never overwritten unseen."""
        query = "query ($id: ID!) { " + self._ITEM_CONTENT_QUERIES[item_type] + " }"
        node = self._graphql(query, {"id": shopify_id}).get(item_type)
        if not node:
            return None
        content = {
            "title": node.get("title") or "",
            "body": node.get("body") or "",
            "updated_at": node.get("updatedAt"),
        }
        if "seo" in node:
            seo = node.get("seo") or {}
            content["meta_title"] = seo.get("title") or ""
            content["meta_description"] = seo.get("description") or ""
        else:
            metas = {
                e["node"]["key"]: e["node"].get("value") or ""
                for e in (node.get("metafields") or {}).get("edges", [])
            }
            content["meta_title"] = metas.get("title_tag", "")
            content["meta_description"] = metas.get("description_tag", "")
        if item_type == "article":
            content["summary"] = node.get("summary") or ""
        return content

    def create_article(self, blog_shopify_id, fields):
        query = """
        mutation ($article: ArticleCreateInput!) {
          articleCreate(article: $article) {
            article {
              id title handle
              blog { id }
              image { url altText }
              publishedAt
            }
            userErrors { field message }
          }
        }
        """
        article_input = {
            "blogId": blog_shopify_id,
            "author": {"name": fields.get("author", "A2Z")},
        }
        if "title" in fields:
            article_input["title"] = fields["title"]
        if fields.get("handle"):
            article_input["handle"] = fields["handle"]
        if "body" in fields:
            article_input["body"] = fields["body"]
        if "summary" in fields:
            article_input["summary"] = fields["summary"]
        if "tags" in fields:
            article_input["tags"] = fields["tags"] if isinstance(fields["tags"], list) else [t.strip() for t in fields["tags"].split(",") if t.strip()]
        if "is_published" in fields:
            article_input["isPublished"] = fields["is_published"]
        if "publish_date" in fields:
            article_input["publishDate"] = fields["publish_date"]
        if "image_url" in fields and fields["image_url"]:
            article_input["image"] = {"url": fields["image_url"]}
            if "image_alt" in fields:
                article_input["image"]["altText"] = fields["image_alt"]
        if "template_suffix" in fields:
            article_input["templateSuffix"] = fields["template_suffix"]

        data = self._graphql(query, {"article": article_input})
        result = data.get("articleCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify articleCreate error: {errors[0].get('message', str(errors))}")
        article = result.get("article", {})
        image = article.get("image") or {}
        return {
            "shopify_id": article.get("id", ""),
            "title": article.get("title", ""),
            "handle": article.get("handle", ""),
            "image_url": image.get("url", ""),
            "image_alt": image.get("altText", ""),
        }

    def update_article(self, shopify_id, fields):
        query = """
        mutation ($article: ArticleUpdateInput!, $id: ID!) {
          articleUpdate(article: $article, id: $id) {
            article { id title handle }
            userErrors { field message }
          }
        }
        """
        # Only touch the author when the caller explicitly provides one —
        # otherwise every unrelated update would overwrite the live byline.
        article_input = {}
        if fields.get("author"):
            article_input["author"] = {"name": fields["author"]}
        if "blog_id" in fields:
            article_input["blogId"] = fields["blog_id"]
        if "title" in fields:
            article_input["title"] = fields["title"]
        if "body" in fields:
            article_input["body"] = fields["body"]
        if "summary" in fields:
            article_input["summary"] = fields["summary"]
        if "tags" in fields:
            article_input["tags"] = fields["tags"] if isinstance(fields["tags"], list) else [t.strip() for t in fields["tags"].split(",") if t.strip()]
        if "is_published" in fields:
            article_input["isPublished"] = fields["is_published"]
        if "publish_date" in fields:
            article_input["publishDate"] = fields["publish_date"]
        if "image_url" in fields:
            if fields["image_url"]:
                article_input["image"] = {"url": fields["image_url"]}
                if "image_alt" in fields:
                    article_input["image"]["altText"] = fields["image_alt"]
            elif fields.get("clear_image"):
                article_input["image"] = None
        if "template_suffix" in fields:
            article_input["templateSuffix"] = fields["template_suffix"]

        data = self._graphql(query, {"article": article_input, "id": shopify_id})
        result = data.get("articleUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify articleUpdate error: {errors[0].get('message', str(errors))}")

        if "meta_title" in fields or "meta_description" in fields:
            self.set_article_seo(shopify_id, fields.get("meta_title"), fields.get("meta_description"))

        return result.get("article", {})

    def set_article_seo(self, article_shopify_id, meta_title=None, meta_description=None):
        # metafieldsSet rejects blank values, so a blank means "clear": delete
        # the metafield and let Shopify fall back to the article title/excerpt.
        metafields = []
        blanks = []
        for key, value in (("title_tag", meta_title), ("description_tag", meta_description)):
            if value is None:
                continue
            if not str(value).strip():
                blanks.append((article_shopify_id, "global", key))
                continue
            metafields.append({
                "ownerId": article_shopify_id,
                "namespace": "global",
                "key": key,
                "value": value,
                "type": "single_line_text_field",
            })
        if blanks:
            self.delete_metafields(blanks)
        if not metafields:
            return []

        query = METAFIELDS_SET_MUTATION
        data = self._graphql(query, {"metafields": metafields})
        result = data.get("metafieldsSet", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify article SEO metafieldsSet error: {errors[0].get('message', str(errors))}")
        logger.info("Set article SEO metafields for %s", article_shopify_id)
        return result.get("metafields", [])

    def set_article_metafields(self, article_shopify_id, pairs):
        """Set arbitrary metafields on an article.

        pairs: list of (namespace, key, value, type) tuples. Values that are
        None are skipped. Uses the same metafieldsSet pattern as set_article_seo.
        The 'global' namespace is readable from Liquid without a definition.
        """
        metafields = []
        for namespace, key, value, mtype in pairs:
            if value is None:
                continue
            metafields.append({
                "ownerId": article_shopify_id,
                "namespace": namespace,
                "key": key,
                "value": value,
                "type": mtype,
            })
        if not metafields:
            return []

        query = METAFIELDS_SET_MUTATION
        data = self._graphql(query, {"metafields": metafields})
        result = data.get("metafieldsSet", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify article metafieldsSet error: {errors[0].get('message', str(errors))}")
        logger.info("Set %d article metafields for %s", len(metafields), article_shopify_id)
        return result.get("metafields", [])

    def set_blog_seo(self, blog_shopify_id, meta_title=None, meta_description=None):
        metafields = []
        if meta_title is not None:
            metafields.append({
                "ownerId": blog_shopify_id,
                "namespace": "global",
                "key": "title_tag",
                "value": meta_title,
                "type": "single_line_text_field",
            })
        if meta_description is not None:
            metafields.append({
                "ownerId": blog_shopify_id,
                "namespace": "global",
                "key": "description_tag",
                "value": meta_description,
                "type": "single_line_text_field",
            })
        if not metafields:
            return []

        query = METAFIELDS_SET_MUTATION
        data = self._graphql(query, {"metafields": metafields})
        result = data.get("metafieldsSet", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify blog SEO metafieldsSet error: {errors[0].get('message', str(errors))}")
        logger.info("Set blog SEO metafields for %s", blog_shopify_id)
        return result.get("metafields", [])

    def delete_article(self, shopify_id):
        query = """
        mutation ($id: ID!) {
          articleDelete(id: $id) {
            deletedArticleId
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"id": shopify_id})
        result = data.get("articleDelete", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify articleDelete error: {errors[0].get('message', str(errors))}")
        return result.get("deletedArticleId")

    def create_url_redirect(self, path, target):
        query = """
        mutation ($urlRedirect: UrlRedirectInput!) {
          urlRedirectCreate(urlRedirect: $urlRedirect) {
            urlRedirect { id path target }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"urlRedirect": {"path": path, "target": target}})
        result = data.get("urlRedirectCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify urlRedirectCreate error: {errors[0].get('message', str(errors))}")
        return result.get("urlRedirect", {})

    def get_pages(self, limit=50, cursor=None):
        query = """
        query ($first: Int!, $after: String) {
          pages(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                title
                handle
                body
                isPublished
                publishedAt
                templateSuffix
                metafields(first: 5, namespace: "global") {
                  edges {
                    node { key value }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"first": limit, "after": cursor})
        pages_data = data.get("pages", {})
        page_info = pages_data.get("pageInfo", {})
        edges = pages_data.get("edges", [])

        pages = []
        for edge in edges:
            node = edge["node"]
            metafield_edges = (node.get("metafields") or {}).get("edges", [])
            seo_title = ""
            seo_description = ""
            for mf in metafield_edges:
                mf_node = mf.get("node", {})
                if mf_node.get("key") == "title_tag":
                    seo_title = mf_node.get("value", "")
                elif mf_node.get("key") == "description_tag":
                    seo_description = mf_node.get("value", "")
            pages.append({
                "shopify_id": node["id"],
                "title": node.get("title", ""),
                "handle": node.get("handle", ""),
                "body_html": node.get("body", ""),
                "is_published": node.get("isPublished", False),
                "published_at": node.get("publishedAt"),
                "template_suffix": node.get("templateSuffix") or "",
                "seo_title": seo_title,
                "seo_description": seo_description,
            })

        return {
            "pages": pages,
            "has_next": page_info.get("hasNextPage", False),
            "cursor": page_info.get("endCursor"),
        }

    def get_all_pages(self):
        all_pages = []
        cursor = None
        while True:
            result = self.get_pages(limit=50, cursor=cursor)
            all_pages.extend(result["pages"])
            if not result["has_next"]:
                break
            cursor = result["cursor"]
        return all_pages

    def get_homepage_data(self):
        """Fetch the shop's homepage SEO metadata (title_tag, description_tag) and storefront URL."""
        query = """
        {
          shop {
            id
            name
            description
            primaryDomain { host url }
            myshopifyDomain
            titleTag: metafield(namespace: "global", key: "title_tag") { value }
            descriptionTag: metafield(namespace: "global", key: "description_tag") { value }
          }
        }
        """
        data = self._graphql(query)
        shop = data.get("shop", {})
        primary = shop.get("primaryDomain") or {}
        storefront_url = primary.get("url") or f"https://{shop.get('myshopifyDomain', self.domain)}"

        theme_sections = []
        menus = {}
        try:
            theme_sections = self.get_homepage_theme_sections()
        except Exception as e:
            logger.warning("Could not fetch homepage theme sections: %s", e)
        try:
            menus = self.get_menus(handles=["main-menu", "footer"])
        except Exception as e:
            logger.warning("Could not fetch menus: %s", e)

        body_html = self._theme_sections_to_html(theme_sections)

        return {
            "shop_id": shop.get("id", ""),
            "shop_name": shop.get("name", ""),
            "storefront_url": storefront_url,
            "seo_title": (shop.get("titleTag") or {}).get("value", ""),
            "seo_description": (shop.get("descriptionTag") or {}).get("value", ""),
            "body_html": body_html,
            "theme_sections": theme_sections,
            "menus": menus,
        }

    def _theme_sections_to_html(self, sections):
        """Generate synthetic HTML from structured theme sections for SEO scoring."""
        parts = []
        for s in sections:
            settings = s.get("settings", {})
            stype = s.get("type", "")
            blocks = s.get("blocks", {})

            if stype == "custom-liquid":
                parts.append(settings.get("custom_liquid", ""))
            elif stype == "featured-collection":
                title = settings.get("title", "")
                coll = settings.get("collection", "")
                if title:
                    parts.append(f"<h2>{title}</h2>")
                if coll:
                    parts.append(f'<a href="/collections/{coll}">{title or coll}</a>')
            elif stype == "featured-collections":
                heading = settings.get("heading", "")
                if heading:
                    parts.append(f"<h2>{heading}</h2>")
                for block in blocks.values():
                    bs = block.get("settings", {})
                    coll = bs.get("collection", "")
                    if coll:
                        parts.append(f'<a href="/collections/{coll}">{coll}</a>')
            elif stype == "logo-list":
                title = settings.get("title", "")
                if title:
                    parts.append(f"<h2>{title}</h2>")
                for block in blocks.values():
                    bs = block.get("settings", {})
                    link = bs.get("link", "")
                    name = bs.get("title", "")
                    if link and name:
                        parts.append(f'<a href="{link}">{name}</a>')
                    if bs.get("image"):
                        parts.append(f'<img src="{bs["image"]}" alt="{name}">')
            elif stype == "text-with-icons":
                for block in blocks.values():
                    bs = block.get("settings", {})
                    title = bs.get("title", "")
                    text = bs.get("text", "")
                    if title:
                        parts.append(f"<p><strong>{title}</strong></p>")
                    if text:
                        parts.append(f"<p>{text}</p>")
            elif stype == "newsletter":
                heading = settings.get("heading", "")
                subtext = settings.get("subtext", "")
                if heading:
                    parts.append(f"<h2>{heading}</h2>")
                if subtext:
                    parts.append(subtext)
            elif stype in ("rich-text", "rich_text"):
                heading = settings.get("heading", "")
                if heading:
                    parts.append(f"<h2>{heading}</h2>")
                for block in blocks.values():
                    bs = block.get("settings", {})
                    if bs.get("text"):
                        parts.append(bs["text"])
            elif stype in ("image-banner", "slideshow"):
                heading = settings.get("heading", "")
                subheading = settings.get("subheading", "")
                if heading:
                    parts.append(f"<h1>{heading}</h1>")
                if subheading:
                    parts.append(f"<p>{subheading}</p>")
                if settings.get("image"):
                    parts.append(f'<img src="{settings["image"]}" alt="{heading}">')

        return "\n".join(parts)

    def get_homepage_theme_sections(self):
        """Fetch structured homepage sections from the theme's templates/index.json."""
        theme_id = self.get_main_theme_id()
        if not theme_id:
            return []
        raw = self.get_theme_asset(theme_id, "templates/index.json")
        if not raw:
            return []
        template = json.loads(raw)
        sections_map = template.get("sections", {})
        order = template.get("order", list(sections_map.keys()))
        result = []
        for key in order:
            section = sections_map.get(key)
            if section:
                result.append({
                    "key": key,
                    "type": section.get("type", ""),
                    "settings": section.get("settings", {}),
                    "blocks": section.get("blocks", {}),
                    "block_order": section.get("block_order", []),
                })
        return result

    def get_menus(self, handles=None):
        """Fetch navigation menus via GraphQL."""
        query = """
        {
          menus(first: 10) {
            nodes {
              id title handle isDefault
              items(limit: 30) {
                id title type url resourceId
                items {
                  id title type url resourceId
                }
              }
            }
          }
        }
        """
        data = self._graphql(query)
        menus = data.get("menus", {}).get("nodes", [])
        if handles:
            menus = [m for m in menus if m.get("handle") in handles]
        result = {}
        for menu in menus:
            result[menu["handle"]] = {
                "title": menu["title"],
                "is_default": menu.get("isDefault", False),
                "items": self._flatten_menu_items(menu.get("items", [])),
            }
        return result

    def _flatten_menu_items(self, items):
        result = []
        for item in items:
            entry = {
                "title": item.get("title", ""),
                "type": item.get("type", ""),
                "url": item.get("url", ""),
                "resource_id": item.get("resourceId"),
                "children": self._flatten_menu_items(item.get("items", [])),
            }
            result.append(entry)
        return result

    def update_homepage_seo(self, seo_title=None, seo_description=None):
        """Update the shop-level SEO title and/or meta description via metafieldsSet."""
        query = """
        {
          shop { id }
        }
        """
        data = self._graphql(query)
        shop_id = data.get("shop", {}).get("id")
        if not shop_id:
            raise Exception("Could not retrieve shop ID")

        metafields = []
        if seo_title is not None:
            metafields.append({
                "ownerId": shop_id,
                "namespace": "global",
                "key": "title_tag",
                "value": seo_title,
                "type": "single_line_text_field",
            })
        if seo_description is not None:
            metafields.append({
                "ownerId": shop_id,
                "namespace": "global",
                "key": "description_tag",
                "value": seo_description,
                "type": "single_line_text_field",
            })
        if not metafields:
            return

        mutation = """
        mutation ($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { key value }
            userErrors { code field message }
          }
        }
        """
        data = self._graphql(mutation, {"metafields": metafields})
        result = data.get("metafieldsSet", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify metafieldsSet error: {errors[0].get('message', str(errors))}")
        return result.get("metafields", [])

    def delete_page(self, shopify_id):
        query = """
        mutation ($id: ID!) {
          pageDelete(id: $id) {
            deletedPageId
            userErrors { code field message }
          }
        }
        """
        data = self._graphql(query, {"id": shopify_id})
        result = data.get("pageDelete", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify pageDelete error: {errors[0].get('message', str(errors))}")
        return result.get("deletedPageId")

    def create_page(self, title, body_html, handle=None, is_published=True, seo_title=None, seo_description=None, template_suffix=None):
        query = """
        mutation ($page: PageCreateInput!) {
          pageCreate(page: $page) {
            page { id title handle }
            userErrors { code field message }
          }
        }
        """
        page_input = {
            "title": title,
            "body": body_html,
            "isPublished": is_published,
        }
        if handle:
            page_input["handle"] = handle
        if template_suffix:
            page_input["templateSuffix"] = template_suffix
        metafields = []
        if seo_title:
            metafields.append({"namespace": "global", "key": "title_tag", "value": seo_title, "type": "single_line_text_field"})
        if seo_description:
            metafields.append({"namespace": "global", "key": "description_tag", "value": seo_description, "type": "single_line_text_field"})
        if metafields:
            page_input["metafields"] = metafields
        data = self._graphql(query, {"page": page_input})
        result = data.get("pageCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify pageCreate error: {errors[0].get('message', str(errors))}")
        page = result.get("page", {})
        return {
            "shopify_id": page.get("id", ""),
            "title": page.get("title", ""),
            "handle": page.get("handle", ""),
        }

    def update_page(self, shopify_id, fields):
        query = """
        mutation ($id: ID!, $page: PageUpdateInput!) {
          pageUpdate(id: $id, page: $page) {
            page { id title handle }
            userErrors { code field message }
          }
        }
        """
        page_input = {}
        if "title" in fields:
            page_input["title"] = fields["title"]
        if "body" in fields:
            page_input["body"] = fields["body"]
        if "handle" in fields:
            page_input["handle"] = fields["handle"]
        if "is_published" in fields:
            page_input["isPublished"] = fields["is_published"]
        if "template_suffix" in fields:
            page_input["templateSuffix"] = fields["template_suffix"]
        metafields = []
        if "seo_title" in fields:
            metafields.append({"namespace": "global", "key": "title_tag", "value": fields["seo_title"], "type": "single_line_text_field"})
        if "seo_description" in fields:
            metafields.append({"namespace": "global", "key": "description_tag", "value": fields["seo_description"], "type": "single_line_text_field"})
        if metafields:
            page_input["metafields"] = metafields
        data = self._graphql(query, {"id": shopify_id, "page": page_input})
        result = data.get("pageUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify pageUpdate error: {errors[0].get('message', str(errors))}")
        page = result.get("page", {})
        return {
            "shopify_id": page.get("id", ""),
            "title": page.get("title", ""),
            "handle": page.get("handle", ""),
        }

    def create_url_redirect(self, path, target):
        """Create a storefront URL redirect (idempotent — an existing identical
        path is treated as success). Returns the redirect id or None."""
        query = """
        mutation ($redirect: UrlRedirectInput!) {
          urlRedirectCreate(urlRedirect: $redirect) {
            urlRedirect { id path target }
            userErrors { field message }
          }
        }
        """
        data = self._graphql(query, {"redirect": {"path": path, "target": target}})
        result = data.get("urlRedirectCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            msg = errors[0].get("message", "")
            if "taken" in msg.lower() or "exists" in msg.lower():
                return None
            raise Exception(f"Shopify urlRedirectCreate error: {msg}")
        return (result.get("urlRedirect") or {}).get("id")

    def delete_url_redirect_by_path(self, path):
        """Best-effort: find and delete a URL redirect matching a path."""
        query = """
        query ($q: String!) {
          urlRedirects(first: 5, query: $q) { edges { node { id path } } }
        }
        """
        data = self._graphql(query, {"q": f"path:{path}"})
        for edge in (data.get("urlRedirects") or {}).get("edges", []):
            if edge["node"].get("path") == path:
                self._graphql(
                    """
                    mutation ($id: ID!) {
                      urlRedirectDelete(id: $id) { deletedUrlRedirectId userErrors { message } }
                    }
                    """,
                    {"id": edge["node"]["id"]},
                )
                return True
        return False

    def get_collection_image(self, collection_shopify_id):
        """Live image url for one collection, "" if it has none.

        collectionUpdate attaches images asynchronously, so this is the only way
        to know an upload actually landed."""
        query = """
        query ($id: ID!) {
          collection(id: $id) { id image { url } }
        }
        """
        data = self._graphql(query, {"id": collection_shopify_id})
        collection = data.get("collection") or {}
        return ((collection.get("image") or {}).get("url")) or ""

    def update_collection_image(self, collection_shopify_id, image_src_url, alt_text):
        query = """
        mutation ($input: CollectionInput!) {
          collectionUpdate(input: $input) {
            collection { id title image { url altText } }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "id": collection_shopify_id,
                "image": {
                    "src": image_src_url,
                    "altText": alt_text,
                },
            }
        }
        data = self._graphql(query, variables)
        result = data.get("collectionUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify collectionUpdate image error: {errors[0].get('message', str(errors))}")
        return result.get("collection", {})

    # ── Theme REST API ─────────────────────────────────────────────

    def _rest_url(self, path):
        return f"https://{self.domain}/admin/api/{self.api_version}/{path}"

    def get_main_theme_id(self):
        resp = requests.get(self._rest_url("themes.json"), headers=self.headers, timeout=15)
        resp.raise_for_status()
        for theme in resp.json().get("themes", []):
            if theme.get("role") == "main":
                return theme["id"]
        return None

    def list_theme_assets(self, theme_id):
        resp = requests.get(
            self._rest_url(f"themes/{theme_id}/assets.json"),
            headers=self.headers,
            timeout=15,
        )
        resp.raise_for_status()
        return [a["key"] for a in resp.json().get("assets", [])]

    def get_theme_asset(self, theme_id, asset_key):
        resp = requests.get(
            self._rest_url(f"themes/{theme_id}/assets.json"),
            headers=self.headers,
            params={"asset[key]": asset_key},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("asset", {}).get("value", "")

    def upsert_theme_asset(self, theme_id, asset_key, value):
        resp = requests.put(
            self._rest_url(f"themes/{theme_id}/assets.json"),
            headers=self.headers,
            json={"asset": {"key": asset_key, "value": value}},
            timeout=15,
        )
        if resp.status_code == 422:
            body = resp.text
            logger.error("Shopify theme asset 422 for %s: %s", asset_key, body)
            raise Exception(f"Shopify rejected theme asset '{asset_key}': {body}")
        resp.raise_for_status()
        return resp.json().get("asset", {})

    def delete_theme_asset(self, theme_id, asset_key):
        """Delete a theme file. Returns True if deleted, False if it didn't exist."""
        resp = requests.delete(
            self._rest_url(f"themes/{theme_id}/assets.json"),
            headers=self.headers,
            params={"asset[key]": asset_key},
            timeout=15,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True
