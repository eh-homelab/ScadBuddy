-- #6949 K1 Task 16 (design §6.1a): an order item's parts leave stock and come back through a
-- workflow (`workflows/order_stock.py` `FulfilOrderItem`). Its first step commits the order
-- change and names itself here; its last step records Go's answers and sets it back to NULL.
-- So a value here is the open fulfil or unfulfil workflow of the item: the item's
-- `parts_settling`, and the workflow that a later fulfil or unfulfil of the item waits for.
ALTER TABLE order_items ADD COLUMN parts_workflow_id text;
