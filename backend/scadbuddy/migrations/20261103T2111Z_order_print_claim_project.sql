-- #5263: the Bambuddy project an order item's print claim keyed its run with, so a
-- retry keys it the same even when the item's project was deleted in Bambuddy and made
-- again. A claim from before this file has print_project_keyed false: its run was keyed
-- with no project (before #5231) or with the item's project as it was then.
ALTER TABLE order_item_fulfilments
    ADD COLUMN print_project_id bigint,
    ADD COLUMN print_project_keyed boolean NOT NULL DEFAULT false;
