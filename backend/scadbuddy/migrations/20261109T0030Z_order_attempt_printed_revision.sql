-- #5704: a production attempt names the revision it printed, the item's current one when
-- it was recorded (`OrderStore.insert_fulfilment`), so `stale_attempt` orders an attempt
-- and the accepted quote's revision by revision number, not by two wall-clock times from
-- two transactions. An attempt made before this names the item's newest revision written
-- no later than the attempt; one made before the item had a revision names none.
UPDATE order_item_fulfilments AS f
   SET revision_id = (
     SELECT r.id FROM order_item_revisions AS r
      WHERE r.item_id = f.order_item_id AND r.created_at <= f.created_at
      ORDER BY r.number DESC LIMIT 1)
 WHERE f.purpose = 'production' AND f.revision_id IS NULL;
