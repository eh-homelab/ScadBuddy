-- An attachment made by opening an order link is bound to that link: revoking the link
-- removes it (`links.revoke_order_links`), and deleting the link cascades. A row with a
-- NULL link_id (a submit's own, or one from before this column) belongs to no link and
-- survives any revoke.
ALTER TABLE storefront_account_orders
  ADD COLUMN link_id text REFERENCES storefront_order_links (id) ON DELETE CASCADE;
CREATE INDEX storefront_account_orders_link ON storefront_account_orders (link_id)
  WHERE link_id IS NOT NULL;
