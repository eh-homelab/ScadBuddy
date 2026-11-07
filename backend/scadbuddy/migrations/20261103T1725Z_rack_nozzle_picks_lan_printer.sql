-- #5138, #5142: a LAN start's picks by their registry printer. The watch settles a
-- start by printer and `subtask_name` (`rack/usage.py` `settle_lan_ended`), and a run's
-- settle stops each pick's time at the printer's next LAN pick, one lookup per open pick.
CREATE INDEX rack_nozzle_picks_lan_printer ON rack_nozzle_picks (printer, picked_at)
    WHERE lan_run_id IS NOT NULL;
