import requests


class ShopifyClient:
    def __init__(self, domain, access_token, api_version="2024-10"):
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
        resp = requests.post(
            self.graphql_url, json=payload, headers=self.headers, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise Exception(data["errors"][0].get("message", str(data["errors"])))
        return data.get("data", {})

    def test_connection(self):
        query = "{ shop { name id myshopifyDomain } }"
        data = self._graphql(query)
        shop = data.get("shop", {})
        return {
            "name": shop.get("name"),
            "domain": shop.get("myshopifyDomain"),
            "id": shop.get("id"),
        }

    def get_products(self, limit=50, cursor=None):
        after = f', after: "{cursor}"' if cursor else ""
        query = f"""
        {{
          products(first: {limit}{after}) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{
              node {{
                id
                title
                handle
                descriptionHtml
                vendor
                productType
                tags
                status
                seo {{ title description }}
                media(first: 10) {{
                  edges {{
                    node {{
                      ... on MediaImage {{
                        id
                        alt
                        preview {{ image {{ url }} }}
                      }}
                    }}
                  }}
                }}
                variants(first: 10) {{
                  edges {{
                    node {{ id title price sku availableForSale compareAtPrice barcode }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        data = self._graphql(query)
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
        after = f', after: "{cursor}"' if cursor else ""
        query = f"""
        {{
          collections(first: {limit}{after}) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{
              node {{
                id
                title
                handle
                descriptionHtml
                seo {{ title description }}
                productsCount {{ count }}
                image {{ url altText }}
                ruleSet {{ appliedDisjunctively }}
                resourcePublicationsV2(first: 1) {{ edges {{ node {{ isPublished }} }} }}
              }}
            }}
          }}
        }}
        """
        data = self._graphql(query)
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
                }
            )

        return {
            "collections": collections,
            "has_next": page_info.get("hasNextPage", False),
            "cursor": page_info.get("endCursor"),
        }

    def update_product(self, shopify_id, fields):
        input_parts = [f'id: "{shopify_id}"']
        if "title" in fields:
            title = fields["title"].replace('"', '\\"')
            input_parts.append(f'title: "{title}"')
        if "description_html" in fields:
            desc = fields["description_html"].replace('"', '\\"').replace("\n", "\\n")
            input_parts.append(f'descriptionHtml: "{desc}"')
        if "tags" in fields and isinstance(fields["tags"], list):
            tags_str = ", ".join(f'"{t}"' for t in fields["tags"])
            input_parts.append(f"tags: [{tags_str}]")

        seo_parts = []
        if "meta_title" in fields:
            mt = fields["meta_title"].replace('"', '\\"')
            seo_parts.append(f'title: "{mt}"')
        if "meta_description" in fields:
            md = fields["meta_description"].replace('"', '\\"')
            seo_parts.append(f'description: "{md}"')
        if seo_parts:
            input_parts.append(f"seo: {{ {', '.join(seo_parts)} }}")

        input_str = ", ".join(input_parts)
        query = f"""
        mutation {{
          productUpdate(input: {{ {input_str} }}) {{
            product {{ id title }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = self._graphql(query)
        result = data.get("productUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify productUpdate error: {errors[0].get('message', str(errors))}")
        return result.get("product", {})

    def update_product_media(self, shopify_id, media_updates):
        valid = [m for m in media_updates if m.get("media_id") and m.get("alt") is not None]
        if not valid:
            return []

        media_items = []
        for m in valid:
            mid = m["media_id"]
            alt = m["alt"].replace('"', '\\"')
            media_items.append(f'{{ id: "{mid}", alt: "{alt}" }}')

        media_str = ", ".join(media_items)
        query = f"""
        mutation {{
          fileUpdate(files: [{media_str}]) {{
            files {{ ... on MediaImage {{ id alt }} }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = self._graphql(query)
        result = data.get("fileUpdate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise Exception(f"Shopify media update error: {errors[0].get('message', str(errors))}")
        return result.get("files", [])

    def get_all_products(self):
        all_products = []
        cursor = None
        while True:
            result = self.get_products(limit=50, cursor=cursor)
            all_products.extend(result["products"])
            if not result["has_next"]:
                break
            cursor = result["cursor"]
        return all_products

    def get_collection_products(self, shopify_id):
        query = """
        query ($id: ID!) {
          collection(id: $id) {
            products(first: 50) {
              edges {
                node { id title handle status }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"id": shopify_id})
        collection = data.get("collection") or {}
        edges = (collection.get("products") or {}).get("edges", [])

        products = []
        for edge in edges:
            node = edge["node"]
            if node.get("status") != "ACTIVE":
                continue
            products.append(
                {
                    "shopify_id": node["id"],
                    "title": node.get("title", ""),
                    "handle": node.get("handle", ""),
                    "status": node.get("status", ""),
                }
            )
        return products

    def get_all_collection_products(self, shopify_id):
        all_products = []
        cursor = None
        while True:
            after = f', after: "{cursor}"' if cursor else ""
            query = f"""
            {{
              collection(id: "{shopify_id}") {{
                products(first: 50{after}) {{
                  pageInfo {{ hasNextPage endCursor }}
                  edges {{
                    node {{ id title handle status }}
                  }}
                }}
              }}
            }}
            """
            data = self._graphql(query)
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
        input_parts = [f'id: "{shopify_id}"']
        if "title" in fields:
            title = fields["title"].replace('"', '\\"')
            input_parts.append(f'title: "{title}"')
        if "description_html" in fields:
            desc = fields["description_html"].replace('"', '\\"').replace("\n", "\\n")
            input_parts.append(f'descriptionHtml: "{desc}"')

        seo_parts = []
        if "meta_title" in fields:
            mt = fields["meta_title"].replace('"', '\\"')
            seo_parts.append(f'title: "{mt}"')
        if "meta_description" in fields:
            md = fields["meta_description"].replace('"', '\\"')
            seo_parts.append(f'description: "{md}"')
        if seo_parts:
            input_parts.append(f"seo: {{ {', '.join(seo_parts)} }}")

        input_str = ", ".join(input_parts)
        query = f"""
        mutation {{
          collectionUpdate(input: {{ {input_str} }}) {{
            collection {{ id title }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = self._graphql(query)
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
        query = """
        mutation ($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { id namespace key }
            userErrors { field message }
          }
        }
        """
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

    def get_publications(self):
        query = """
        {
          publications(first: 50) {
            edges {
              node { id name }
            }
          }
        }
        """
        data = self._graphql(query)
        edges = (data.get("publications") or {}).get("edges", [])
        return [{"id": edge["node"]["id"], "name": edge["node"].get("name", "")} for edge in edges]

    def publish_to_all_channels(self, resource_shopify_id):
        publications = self.get_publications()
        if not publications:
            return []

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
        escaped_title = title.replace('"', '\\"')
        input_parts = [f'title: "{escaped_title}"']
        if description_html:
            escaped_desc = description_html.replace('"', '\\"').replace("\n", "\\n")
            input_parts.append(f'descriptionHtml: "{escaped_desc}"')

        input_str = ", ".join(input_parts)
        query = f"""
        mutation {{
          collectionCreate(input: {{ {input_str} }}) {{
            collection {{ id title handle }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = self._graphql(query)
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
